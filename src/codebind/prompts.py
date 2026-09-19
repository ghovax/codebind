"""Native IPython prompt rendering for model-authored cells."""

from __future__ import annotations

import sys

from IPython.terminal.ptutils import IPythonPTLexer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, PygmentsTokens
from prompt_toolkit.shortcuts import print_formatted_text


def render_cell(shell: object, cell: str) -> None:
    """Render a cell with IPython's lexer, prompt tokens, and terminal style."""
    lexer = IPythonPTLexer().lex_document(Document(cell))
    pt_app = getattr(shell, "pt_app", None)
    style = pt_app.app.style if pt_app is not None else None
    sys.stdout.write(getattr(shell, "separate_in", "\n"))
    prompts = shell.prompts

    for index, _line in enumerate(cell.split("\n")):
        prompt_tokens = (
            prompts.in_prompt_tokens()
            if index == 0
            else prompts.continuation_prompt_tokens(lineno=index - 1)
        )
        print_formatted_text(PygmentsTokens(prompt_tokens), style=style, end="")
        print_formatted_text(FormattedText(lexer(index)), style=style)


def render_output_prompt(shell: object) -> None:
    """Render an output prompt with IPython's terminal style."""
    pt_app = getattr(shell, "pt_app", None)
    style = pt_app.app.style if pt_app is not None else None
    sys.stdout.write(getattr(shell, "separate_out", "") or "\n")
    print_formatted_text(PygmentsTokens(shell.prompts.out_prompt_tokens()), style=style, end="")
