"""Terminal presentation using IPython's prompts, history, and MIME renderers."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

from IPython.terminal.interactiveshell import IPythonPTLexer, TerminalInteractiveShell
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, PygmentsTokens
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console
from rich.markdown import Markdown


_MISSING = object()
_IMAGE_FORMATS = ("image/png", "image/jpeg", "image/svg+xml")


def show_input(shell: TerminalInteractiveShell, cell: str) -> None:
    """Show a model cell with the shell's own input and continuation prompts."""
    sys.stdout.write(shell.separate_in)
    lines = cell.splitlines()
    lexer = IPythonPTLexer().lex_document(Document(cell))
    first_prompt = shell.prompts.in_prompt_tokens()
    prompt_width = sum(len(text) for _, text in first_prompt)
    style = shell.pt_app.app.style if shell.pt_app is not None else shell._style
    for index, _ in enumerate(lines):
        prompt = (
            first_prompt
            if index == 0
            else shell.prompts.continuation_prompt_tokens(prompt_width, lineno=index - 1)
        )
        print_formatted_text(
            PygmentsTokens(prompt),
            FormattedText(lexer(index)),
            sep="",
            style=style,
            color_depth=shell.color_depth,
        )


def install_markdown_renderer(shell: TerminalInteractiveShell) -> Callable[[], None]:
    """Render Codebind's Markdown MIME in the terminal, preserving shell settings."""
    mime = "text/markdown"
    formatter = shell.display_formatter.formatters[mime]
    was_enabled = formatter.enabled
    was_active = mime in shell.display_formatter.active_types
    previous_renderer = shell.mime_renderers.get(mime, _MISSING)

    def render(text: str, _metadata: object) -> None:
        Console(file=sys.stdout, soft_wrap=True, no_color=shell.colors.lower() == "nocolor").print(
            Markdown(text)
        )

    formatter.enabled = True
    if not was_active:
        shell.display_formatter.active_types.append(mime)
    shell.mime_renderers[mime] = render

    def restore() -> None:
        if previous_renderer is _MISSING:
            shell.mime_renderers.pop(mime, None)
        else:
            shell.mime_renderers[mime] = previous_renderer
        if not was_active and mime in shell.display_formatter.active_types:
            shell.display_formatter.active_types.remove(mime)
        formatter.enabled = was_enabled

    return restore


@contextmanager
def record_tool_output(shell: TerminalInteractiveShell) -> Iterator[None]:
    """Keep image and shell-command output in IPython's native history."""
    active = shell.display_formatter.active_types
    previous_png_renderer = shell.mime_renderers.pop("image/png", _MISSING)
    settings: list[tuple[str, bool, bool]] = []
    for mime in _IMAGE_FORMATS:
        formatter = shell.display_formatter.formatters[mime]
        settings.append((mime, formatter.enabled, mime in active))
        formatter.enabled = True
        if mime not in active:
            active.append(mime)
    try:
        with patch.object(shell, "system", shell.system_piped):
            yield
    finally:
        for mime, was_enabled, was_active in settings:
            if not was_active and mime in active:
                active.remove(mime)
            shell.display_formatter.formatters[mime].enabled = was_enabled
        if previous_png_renderer is not _MISSING:
            shell.mime_renderers["image/png"] = previous_png_renderer


__all__ = ["install_markdown_renderer", "record_tool_output", "show_input"]
