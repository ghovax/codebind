"""Execution of model-authored cells in a shared IPython namespace."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output

from .display import display_cell
from .invocation import InvocationRegistry
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

    @classmethod
    def from_notebook_outputs(cls, outputs: list[dict[str, Any]]) -> ExecutionReport:
        """Build a model-facing report from standard notebook outputs."""
        stdout: list[str] = []
        stderr: list[str] = []
        displays: list[str] = []
        result: str | None = None
        error: dict[str, str] | None = None

        for output in outputs:
            output_type = output.get("output_type")
            if output_type == "stream":
                stream = stdout if output.get("name") == "stdout" else stderr
                stream.append(_notebook_text(output.get("text")))
            elif output_type == "execute_result":
                data = output.get("data")
                if isinstance(data, dict):
                    result = _notebook_text(data.get("text/plain"))
            elif output_type == "display_data":
                data = output.get("data")
                if isinstance(data, dict) and "text/plain" in data:
                    displays.append(_notebook_text(data["text/plain"]))
            elif output_type == "error":
                error = {
                    "type": str(output.get("ename", "Error")),
                    "message": str(output.get("evalue", "")),
                }

        return cls(
            ok=error is None,
            stdout="".join(stdout),
            stderr="".join(stderr),
            result=result,
            displays=tuple(displays),
            error=error,
        )


def _notebook_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


class _ExecutionDisplayHook:
    """Capture an expression result while preserving IPython output history."""

    def __init__(
        self,
        shell: InteractiveShell,
        outputs: list[dict[str, Any]],
        *,
        record_history: bool,
    ) -> None:
        self.shell = shell
        self.outputs = outputs
        self.record_history = record_history

    def __call__(self, value: Any = None) -> None:
        if value is None:
            return
        displayhook = self.shell.displayhook
        displayhook.check_for_underscore()
        if displayhook.quiet():
            return
        data, metadata = displayhook.compute_format_data(value)
        displayhook.fill_exec_result(value)
        if data:
            if self.record_history:
                displayhook.update_user_ns(value)
                displayhook.log_output(data)
            self.outputs.append({"data": data, "metadata": metadata})


@contextmanager
def _captured_execution(
    shell: InteractiveShell,
    expression_outputs: list[dict[str, Any]],
    *,
    suppress: bool,
    record_history: bool,
) -> Iterator[Any]:
    """Capture one nested execution without leaking its output to the parent cell."""
    with capture_output() as captured:
        if not suppress:
            yield captured
            return

        previous_displayhook = sys.displayhook
        previous_showtraceback = shell.showtraceback
        previous_showsyntaxerror = shell.showsyntaxerror
        sys.displayhook = _ExecutionDisplayHook(
            shell,
            expression_outputs,
            record_history=record_history,
        )
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
        *,
        visible: bool = True,
        registry: InvocationRegistry | None = None,
        invocation_id: str | None = None,
    ) -> None:
        self.shell = shell
        self.bridge = bridge
        self.visible = visible
        self.registry = registry or InvocationRegistry()
        self.invocation_id = invocation_id

    def execute(self, cell: str) -> ExecutionReport:
        """Execute a cell through IPython and replay its native rich output."""
        if not isinstance(cell, str) or not cell.strip():
            raise ValueError("cell must be a non-empty string")

        bridge = self.bridge
        bridged = self.visible and bridge is not None and bridge.ready
        if self.visible and not bridged:
            display_cell(cell)
        expression_outputs: list[dict[str, Any]] = []
        with _captured_execution(
            self.shell,
            expression_outputs,
            suppress=bridged or not self.visible,
            record_history=self.visible,
        ) as captured:
            result = self.shell.run_cell(cell, store_history=self.visible)
        if bridged:
            assert bridge is not None
            bridge.insert_code_cell(
                cell,
                result.execution_count,
                self._notebook_outputs(result, captured, expression_outputs),
            )
        elif self.visible:
            captured.show()

        return self._report(result, captured)

    async def aexecute(self, cell: str) -> ExecutionReport:
        """Execute an async-capable cell and replay its native rich output."""
        if not isinstance(cell, str) or not cell.strip():
            raise ValueError("cell must be a non-empty string")

        bridge = self.bridge
        if self.invocation_id is not None and bridge is not None and bridge.ready:
            async with self.registry.execution():
                payload = await bridge.execute_cell(self.invocation_id, cell)
            outputs = payload.get("outputs")
            if not isinstance(outputs, list):
                raise TypeError("JupyterLab returned invalid cell outputs")
            return ExecutionReport.from_notebook_outputs(outputs)

        bridged = self.visible and bridge is not None and bridge.ready
        if self.visible and not bridged:
            display_cell(cell)
        transformed = self.shell.transform_cell(cell)
        expression_outputs: list[dict[str, Any]] = []
        async with self.registry.execution():
            with _captured_execution(
                self.shell,
                expression_outputs,
                suppress=bridged or not self.visible,
                record_history=self.visible,
            ) as captured:
                result = await self.shell.run_cell_async(
                    cell,
                    store_history=self.visible,
                    transformed_cell=transformed,
                )
        if bridged:
            assert bridge is not None
            bridge.insert_code_cell(
                cell,
                result.execution_count,
                self._notebook_outputs(result, captured, expression_outputs),
            )
        elif self.visible:
            captured.show()

        return self._report(result, captured)

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
