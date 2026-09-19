"""Native IPython prompt rendering for model-authored cells."""

from __future__ import annotations

from IPython.terminal.prompts import Prompts
from IPython.terminal.ptutils import IPythonPTLexer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, PygmentsTokens
from prompt_toolkit.shortcuts import print_formatted_text
from pygments.token import Token


class CellPrompts(Prompts):
    """IPython prompts for one numbered model-authored cell."""

    def __init__(self, shell: object, number: int) -> None:
        super().__init__(shell)
        self.number = number

    def in_prompt_tokens(self):
        return [
            (Token.Prompt, "Python ["),
            (Token.PromptNum, str(self.number)),
            (Token.Prompt, "]: "),
        ]

    def continuation_prompt_tokens(self, width: int | None = None, **_: object):
        width = self._width() if width is None else width
        return [
            (Token.Prompt.Continuation, (" " * (width - 5)) + "...:"),
            (Token.Prompt.Padding, " "),
        ]

    def out_prompt_tokens(self):
        return [
            (Token.OutPrompt, "Shell ["),
            (Token.OutPromptNum, str(self.number)),
            (Token.OutPrompt, "]: "),
        ]


def render_cell(shell: object, cell: str, prompts: CellPrompts) -> None:
    """Render a cell with IPython's lexer, prompt tokens, and terminal style."""
    lexer = IPythonPTLexer().lex_document(Document(cell))
    pt_app = getattr(shell, "pt_app", None)
    style = pt_app.app.style if pt_app is not None else None

    for index, _line in enumerate(cell.split("\n")):
        prompt_tokens = (
            prompts.in_prompt_tokens()
            if index == 0
            else prompts.continuation_prompt_tokens(lineno=index - 1)
        )
        print_formatted_text(PygmentsTokens(prompt_tokens), style=style, end="")
        print_formatted_text(FormattedText(lexer(index)), style=style)
