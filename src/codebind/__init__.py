"""A minimal model loop over the current IPython session."""

from importlib.metadata import version

from .execution import ExecutionReport, IPythonExecutor
from .extension import load_ipython_extension, unload_ipython_extension
from .invocation import Invocation, Outcome, current_invocation_id
from .jupyter import JupyterLabBridge
from .session import Session


__version__ = version("codebind")

__all__ = [
    "ExecutionReport",
    "IPythonExecutor",
    "Invocation",
    "JupyterLabBridge",
    "Outcome",
    "Session",
    "__version__",
    "current_invocation_id",
    "load_ipython_extension",
    "unload_ipython_extension",
]
