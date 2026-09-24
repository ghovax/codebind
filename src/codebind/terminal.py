"""Terminal presentation using IPython's prompts, history, and MIME renderers."""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from IPython.terminal.interactiveshell import IPythonPTLexer, TerminalInteractiveShell
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, PygmentsTokens
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import DynamicStyle, Style, merge_styles
from rich.console import Console
from rich.markdown import Markdown


_MISSING = object()
_IMAGE_FORMATS = ("image/png", "image/jpeg", "image/svg+xml")


def _question_text(transformed: str) -> str | None:
    """Recover a Codebind question from IPython's own transformed history."""
    try:
        statements = ast.parse(transformed).body
    except SyntaxError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], ast.Expr):
        return None
    call = statements[0].value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in {"run_cell_magic", "run_line_magic"}
        and isinstance(call.func.value, ast.Call)
        and isinstance(call.func.value.func, ast.Name)
        and call.func.value.func.id == "get_ipython"
        and bool(call.args)
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "question"
    ):
        return None
    text_index = 2 if call.func.attr == "run_cell_magic" else 1
    if len(call.args) <= text_index or not isinstance(call.args[text_index], ast.Constant):
        return None
    text = call.args[text_index].value
    return text.strip() if isinstance(text, str) else None


def _history_cell(
    shell: TerminalInteractiveShell,
    number: int,
    source: str,
    transformed: str,
) -> dict[str, object] | None:
    """Capture one finished IPython cell before later displays can alter its history."""
    if not source.strip():
        return None
    history = shell.history_manager
    identifier = f"terminal-{number}"
    question = _question_text(transformed)
    if question is not None:
        return {"id": identifier, "type": "markdown", "source": question}

    outputs: list[dict[str, object]] = []
    for record in history.outputs.get(number, ()):
        if record.output_type in {"out_stream", "err_stream"}:
            stream = record.bundle.get("stream", [])
            outputs.append(
                {
                    "output_type": "stream",
                    "name": "stdout" if record.output_type == "out_stream" else "stderr",
                    "text": "".join(stream),
                }
            )
        elif record.output_type in {"display_data", "execute_result"}:
            output: dict[str, object] = {
                "output_type": record.output_type,
                "data": deepcopy(record.bundle),
                "metadata": {},
            }
            if record.output_type == "execute_result":
                output["execution_count"] = number
            outputs.append(output)
    error = history.exceptions.get(number)
    if error is not None:
        outputs.append({"output_type": "error", **deepcopy(error)})
    return {
        "id": identifier,
        "type": "code",
        "source": source,
        "execution_count": number,
        "outputs": outputs,
    }


class TerminalCellHistory:
    """Track completed IPython cells, without reinterpreting mutable output history."""

    def __init__(
        self,
        shell: TerminalInteractiveShell,
        previous: TerminalCellHistory | None = None,
    ) -> None:
        self.shell = shell
        self._cells = deepcopy(previous._cells) if previous is not None else {}
        history = shell.history_manager
        for number in range(1, shell.execution_count):
            if number in self._cells or number >= len(history.input_hist_raw):
                continue
            cell = _history_cell(
                shell,
                number,
                history.input_hist_raw[number],
                history.input_hist_parsed[number],
            )
            if cell is not None:
                self._cells[number] = cell
        self._callback = self._record
        shell.events.register("post_run_cell", self._callback)

    def _record(self, result) -> None:
        if result is None or result.execution_count is None or not result.info.store_history:
            return
        cell = _history_cell(
            self.shell,
            result.execution_count,
            result.info.raw_cell,
            result.info.transformed_cell,
        )
        if cell is not None:
            self._cells[result.execution_count] = cell

    def snapshot(self) -> list[dict[str, object]]:
        return [deepcopy(self._cells[number]) for number in sorted(self._cells)]

    def close(self) -> None:
        self.shell.events.unregister("post_run_cell", self._callback)


def install_question_mode(shell: TerminalInteractiveShell) -> Callable[[], None]:
    """Let Shift+Tab switch the terminal prompt between Python and questions."""
    prompt_session = shell.pt_app
    if prompt_session is None:
        return lambda: None

    original_style = prompt_session.style
    original_prompt_for_code = shell.prompt_for_code
    had_prompt_override = "prompt_for_code" in vars(shell)
    bindings = prompt_session.key_bindings
    transforms = shell.input_transformer_manager.cleanup_transforms
    question_mode = False
    active_prompt = False
    pending_question = False

    def prompt_for_code() -> str:
        nonlocal active_prompt, pending_question
        active_prompt = True
        try:
            text = original_prompt_for_code()
        finally:
            active_prompt = False
        pending_question = question_mode and bool(text.strip())
        return text

    def transform_question(lines: list[str]) -> list[str]:
        nonlocal pending_question
        if not pending_question:
            return lines
        pending_question = False
        return ["%%question\n", *lines]

    def toggle_question(event) -> None:
        nonlocal question_mode
        question_mode = not question_mode
        event.app.invalidate()

    def submit_question(event) -> None:
        event.current_buffer.validate_and_handle()

    question_active = Condition(lambda: active_prompt and question_mode)
    bindings.add("s-tab", filter=has_focus(DEFAULT_BUFFER))(toggle_question)
    bindings.add("enter", filter=has_focus(DEFAULT_BUFFER) & question_active)(submit_question)
    shell.prompt_for_code = prompt_for_code
    question_style = merge_styles(
        [
            original_style,
            Style.from_dict(
                {
                    key: "#a855f7"
                    for key in (
                        "pygments.prompt",
                        "pygments.promptnum",
                        "pygments.prompt.mode",
                        "pygments.prompt.linenumber",
                        "pygments.prompt.continuation",
                        "pygments.prompt.padding",
                        "pygments.prompt.wrap",
                    )
                }
            ),
        ]
    )
    prompt_session.style = DynamicStyle(
        lambda: question_style if active_prompt and question_mode else original_style
    )
    transforms.insert(0, transform_question)

    def restore() -> None:
        bindings.remove(toggle_question)
        bindings.remove(submit_question)
        transforms.remove(transform_question)
        if had_prompt_override:
            shell.prompt_for_code = original_prompt_for_code
        else:
            del shell.prompt_for_code
        prompt_session.style = original_style

    return restore


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


__all__ = [
    "TerminalCellHistory",
    "install_markdown_renderer",
    "install_question_mode",
    "record_tool_output",
    "show_input",
]
