"""Execution of model-authored cells in a shared IPython namespace."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output


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


class IPythonExecutor:
    """Run cells through one existing IPython shell."""

    def __init__(self, shell: InteractiveShell) -> None:
        self.shell = shell

    def execute(self, cell: str) -> ExecutionReport:
        """Execute a cell, replay its visible output, and capture a structured result."""
        if not isinstance(cell, str) or not cell.strip():
            raise ValueError("cell must be a non-empty string")

        displayhook = self.shell.displayhook
        original_prompt = displayhook.write_output_prompt
        displayhook.write_output_prompt = lambda: None
        try:
            with capture_output() as captured:
                result = self.shell.run_cell(cell, store_history=False)
        finally:
            displayhook.write_output_prompt = original_prompt

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
