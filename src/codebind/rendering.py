"""Terminal rendering for model responses and IPython tool output."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from .execution import ExecutionReport
from .prompts import CellPrompts, render_output_prompt


def _visible_output(report: ExecutionReport) -> str:
    parts = [part for part in (report.stdout, report.stderr) if part]
    if report.displays:
        parts.extend(report.displays)
    elif not parts and report.result is not None:
        parts.append(report.result)
    if not parts and report.error is not None:
        parts.append(f"{report.error['type']}: {report.error['message']}")
    return "\n".join(part.rstrip("\n") for part in parts if part).strip("\n")


class TerminalRenderer:
    """Render tool output and Markdown answers to the active terminal."""

    def __init__(self) -> None:
        self.console = Console()

    def tool_output(self, report: ExecutionReport, shell: object, prompts: CellPrompts) -> None:
        """Show the complete tool output."""
        visible = _visible_output(report)
        if not visible:
            return
        output = Text.from_ansi(visible)
        if report.ok:
            render_output_prompt(shell, prompts)
        self.console.print(output)

    def assistant(self, text: str) -> None:
        """Render the final model response as terminal Markdown."""
        self.console.print()
        self.console.print(Markdown(text))
