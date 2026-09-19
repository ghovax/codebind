"""Execution of model-authored cells in a shared IPython namespace."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, cast

from IPython.core.displaypub import DisplayPublisher
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output

from .display import display_cell
from .jupyter import JupyterLabBridge


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """The model-facing result of one IPython cell."""

    ok: bool
    stdout: str
    stderr: str
    result: str | None
    displays: tuple[str, ...]
    error: dict[str, str] | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


class _ExecutionDisplayHook:
    """Capture an expression result while preserving IPython output history."""

    def __init__(
        self,
        shell: InteractiveShell,
        outputs: list[dict[str, Any]],
        on_output: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.shell = shell
        self.outputs = outputs
        self.on_output = on_output

    def __call__(self, value: Any = None) -> None:
        if value is None:
            return
        displayhook = self.shell.displayhook
        displayhook.check_for_underscore()
        if displayhook.quiet():
            return
        data, metadata = displayhook.compute_format_data(value)
        displayhook.update_user_ns(value)
        displayhook.fill_exec_result(value)
        if data:
            displayhook.log_output(data)
            self.outputs.append({"data": data, "metadata": metadata})
            if self.on_output is not None:
                self.on_output(
                    {
                        "output_type": "execute_result",
                        "execution_count": displayhook.prompt_count,
                        "data": data,
                        "metadata": metadata,
                    }
                )


class _StreamingTextIO:
    """Write to IPython's capture buffer while forwarding live stream output."""

    def __init__(
        self,
        stream: Any,
        name: str,
        on_output: Callable[[dict[str, Any]], None],
    ) -> None:
        self.stream = stream
        self.name = name
        self.on_output = on_output

    def write(self, text: str) -> int:
        written = self.stream.write(text)
        if text:
            self.on_output({"output_type": "stream", "name": self.name, "text": text})
        return written

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _StreamingDisplayPublisher:
    """Capture rich displays while forwarding them to the notebook cell."""

    def __init__(
        self,
        publisher: Any,
        on_output: Callable[[dict[str, Any]], None],
        on_clear: Callable[[bool], None],
    ) -> None:
        self.publisher = publisher
        self.on_output = on_output
        self.on_clear = on_clear

    def publish(
        self,
        data: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        source: str | None = None,
        *,
        transient: dict[str, Any] | None = None,
        update: bool = False,
    ) -> None:
        self.publisher.publish(
            data,
            metadata=metadata,
            source=source,
            transient=transient,
            update=update,
        )
        self.on_output(
            {
                "output_type": "display_data",
                "data": data,
                "metadata": metadata or {},
            }
        )

    def clear_output(self, wait: bool = False) -> None:
        self.publisher.clear_output(wait)
        self.on_clear(wait)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.publisher, name)


@contextmanager
def _captured_execution(
    shell: InteractiveShell,
    expression_outputs: list[dict[str, Any]],
    *,
    bridged: bool,
    on_output: Callable[[dict[str, Any]], None] | None = None,
    on_clear: Callable[[bool], None] | None = None,
) -> Iterator[Any]:
    """Capture one nested execution without leaking its output to the parent cell."""
    with capture_output() as captured:
        if not bridged:
            yield captured
            return

        previous_displayhook = sys.displayhook
        previous_showtraceback = shell.showtraceback
        previous_showsyntaxerror = shell.showsyntaxerror
        if on_output is not None:
            sys.stdout = _StreamingTextIO(sys.stdout, "stdout", on_output)
            sys.stderr = _StreamingTextIO(sys.stderr, "stderr", on_output)
            shell.display_pub = cast(
                DisplayPublisher,
                _StreamingDisplayPublisher(
                    shell.display_pub,
                    on_output,
                    on_clear or (lambda wait: None),
                ),
            )
        sys.displayhook = _ExecutionDisplayHook(shell, expression_outputs, on_output)
        shell.showtraceback = lambda *args, **kwargs: None
        shell.showsyntaxerror = lambda *args, **kwargs: None
        try:
            yield captured
        finally:
            sys.displayhook = previous_displayhook
            shell.showtraceback = previous_showtraceback
            shell.showsyntaxerror = previous_showsyntaxerror


class IPythonExecutor:
    """Run cells through one existing IPython shell."""

    def __init__(
        self,
        shell: InteractiveShell,
        bridge: JupyterLabBridge | None = None,
    ) -> None:
        self.shell = shell
        self.bridge = bridge

    def execute(self, cell: str) -> ExecutionReport:
        """Execute a cell through IPython and replay its native rich output."""
        if not isinstance(cell, str) or not cell.strip():
            raise ValueError("cell must be a non-empty string")

        bridge = self.bridge
        cell_id = bridge.start_code_cell(cell) if bridge is not None else None
        bridged = cell_id is not None
        on_output, on_clear = self._stream_callbacks(cell_id)
        if not bridged:
            display_cell(cell)
        expression_outputs: list[dict[str, Any]] = []
        with _captured_execution(
            self.shell,
            expression_outputs,
            bridged=bridged,
            on_output=on_output,
            on_clear=on_clear,
        ) as captured:
            result = self.shell.run_cell(cell, store_history=True)
        if bridged:
            assert bridge is not None and cell_id is not None
            bridge.finish_code_cell(
                cell_id,
                result.execution_count,
                self._notebook_outputs(result, captured, expression_outputs),
            )
        else:
            captured.show()

        return self._report(result, captured)

    async def aexecute(self, cell: str) -> ExecutionReport:
        """Execute an async-capable cell and replay its native rich output."""
        if not isinstance(cell, str) or not cell.strip():
            raise ValueError("cell must be a non-empty string")

        bridge = self.bridge
        cell_id = bridge.start_code_cell(cell) if bridge is not None else None
        bridged = cell_id is not None
        on_output, on_clear = self._stream_callbacks(cell_id)
        if not bridged:
            display_cell(cell)
        transformed = self.shell.transform_cell(cell)
        expression_outputs: list[dict[str, Any]] = []
        with _captured_execution(
            self.shell,
            expression_outputs,
            bridged=bridged,
            on_output=on_output,
            on_clear=on_clear,
        ) as captured:
            result = await self.shell.run_cell_async(
                cell,
                store_history=True,
                transformed_cell=transformed,
            )
        if bridged:
            assert bridge is not None and cell_id is not None
            bridge.finish_code_cell(
                cell_id,
                result.execution_count,
                self._notebook_outputs(result, captured, expression_outputs),
            )
        else:
            captured.show()

        return self._report(result, captured)

    def _stream_callbacks(
        self,
        cell_id: str | None,
    ) -> tuple[
        Callable[[dict[str, Any]], None] | None,
        Callable[[bool], None] | None,
    ]:
        bridge = self.bridge
        if bridge is None or cell_id is None:
            return None, None

        def on_output(output: dict[str, Any]) -> None:
            bridge.append_code_output(cell_id, output)

        def on_clear(wait: bool) -> None:
            bridge.clear_code_output(cell_id, wait=wait)

        return on_output, on_clear

    @staticmethod
    def _report(result: Any, captured: Any) -> ExecutionReport:
        """Build the model-facing text projection of an IPython execution."""

        displays: list[str] = []
        for output in captured.outputs:
            data = getattr(output, "data", None)
            if isinstance(data, dict) and "text/plain" in data:
                displays.append(str(data["text/plain"]))

        exception = result.error_before_exec or result.error_in_exec
        error = (
            {"type": type(exception).__name__, "message": str(exception)}
            if exception is not None
            else None
        )
        return ExecutionReport(
            ok=result.success,
            stdout=captured.stdout,
            stderr=captured.stderr,
            result=repr(result.result) if result.result is not None else None,
            displays=tuple(displays),
            error=error,
        )

    def _notebook_outputs(
        self,
        result: Any,
        captured: Any,
        expression_outputs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Build standard nbformat outputs for a JupyterLab code cell."""
        outputs: list[dict[str, Any]] = []
        if captured.stdout:
            outputs.append({"output_type": "stream", "name": "stdout", "text": captured.stdout})
        if captured.stderr:
            outputs.append({"output_type": "stream", "name": "stderr", "text": captured.stderr})

        for output in captured.outputs:
            data = getattr(output, "data", None)
            if not isinstance(data, dict):
                continue
            outputs.append(
                {
                    "output_type": "display_data",
                    "data": data,
                    "metadata": getattr(output, "metadata", {}) or {},
                }
            )

        for expression in expression_outputs:
            outputs.append(
                {
                    "output_type": "execute_result",
                    "execution_count": result.execution_count,
                    "data": expression["data"],
                    "metadata": expression["metadata"],
                }
            )

        exception = result.error_before_exec or result.error_in_exec
        if exception is not None:
            if isinstance(exception, SyntaxError):
                traceback = self.shell.SyntaxTB.structured_traceback(
                    type(exception),
                    exception,
                )
            else:
                traceback = self.shell.InteractiveTB.structured_traceback(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            outputs.append(
                {
                    "output_type": "error",
                    "ename": type(exception).__name__,
                    "evalue": str(exception),
                    "traceback": traceback,
                }
            )
        return outputs
