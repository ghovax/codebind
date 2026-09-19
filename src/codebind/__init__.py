"""A minimal model loop over the current IPython session."""

from importlib.metadata import version

from .execution import ExecutionReport, IPythonExecutor
from .session import Session


__version__ = version("codebind")

__all__ = [
    "ExecutionReport",
    "IPythonExecutor",
    "Session",
    "__version__",
]
