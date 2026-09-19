"""Project-local IPython launcher with Codebind conveniences preloaded."""

from __future__ import annotations

from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from IPython.terminal.ipapp import TerminalIPythonApp
from models_provider import Models
from rich.console import Console
from rich.markdown import Markdown

from .session import Session


BANNER = """\
- `models = Models({...})`
- `chat.ask("...", models.chat("provider/model"))`
"""


def namespace(shell: InteractiveShell) -> dict[str, Any]:
    """Build the small namespace exposed by the Codebind shell."""
    return {
        "chat": Session(shell=shell),
        "Models": Models,
    }


def main() -> None:
    """Start IPython in the current directory with Codebind preloaded."""
    Console().print(Markdown(BANNER))
    application = TerminalIPythonApp.instance()
    application.display_banner = False
    application.initialize([])
    shell = application.shell
    shell.enable_tip = False
    shell.push(namespace(shell))
    application.start()
