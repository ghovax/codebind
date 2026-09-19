"""A minimal model loop over the current IPython session."""

from importlib.metadata import version

from .execution import ExecutionReport, PythonExecutor
from .session import Session


__version__ = version("codebind")

__all__ = [
    "ExecutionReport",
    "PythonExecutor",
    "Session",
    "__version__",
]
