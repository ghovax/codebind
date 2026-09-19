"""Frontend-neutral output through IPython's MIME display protocol."""

from __future__ import annotations

from IPython.display import Code, display


def display_cell(cell: str) -> None:
    """Display model-authored cell source in the active frontend."""
    display(Code(cell, language="python"))


def display_assistant(text: str) -> None:
    """Publish assistant text as plain text and Markdown MIME representations."""
    display({"text/plain": text, "text/markdown": text}, raw=True)
