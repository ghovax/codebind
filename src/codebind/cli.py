"""Standard IPython launcher with the Codebind extension loaded."""

from __future__ import annotations

import sys

from IPython import start_ipython

from . import __version__


def main() -> None:
    """Start ordinary IPython with Codebind loaded as an extension."""
    if sys.argv[1:] == ["--version"]:
        print(f"codebind {__version__}")
        return
    start_ipython(argv=["--ext=codebind", *sys.argv[1:]])
