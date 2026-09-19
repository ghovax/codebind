"""Terminal rendering for model responses and IPython tool output."""

from __future__ import annotations

from itertools import chain

from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segments
from rich.text import Text

from .execution import ExecutionReport
from .prompts import CellPrompts, render_output_prompt


_MAXIMUM_OUTPUT_LINES = 10


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
    """Render concise tool previews and Markdown answers to the active terminal."""

    def __init__(self) -> None:
        self.console = Console()

    def tool_output(self, report: ExecutionReport, shell: object, prompts: CellPrompts) -> None:
        """Show a bounded preview while leaving the report itself intact for the model."""
        visible = _visible_output(report)
        if not visible:
            return
        output = Text.from_ansi(visible)
        options = self.console.options
        complete = self.console.render_lines(output, options, pad=False, new_lines=True)
        preview = complete[:_MAXIMUM_OUTPUT_LINES]
        if report.ok:
            render_output_prompt(shell, prompts)
        self.console.print(Segments(chain.from_iterable(preview)), end="")
        if len(complete) > len(preview):
            self.console.print(
                f"[dim]{len(preview)} of {len(complete)} lines shown; "
                "the complete result was returned to the model.[/dim]"
            )

    def assistant(self, text: str) -> None:
        """Render the final model response as terminal Markdown."""
        self.console.print()
        self.console.print(Markdown(text))
