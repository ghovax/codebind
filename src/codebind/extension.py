"""IPython extension entry points."""

from __future__ import annotations

from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from models_provider import Models

from .session import Session


_NAMESPACE_ATTRIBUTE = "_codebind_extension_namespace"


def load_ipython_extension(ipython: InteractiveShell) -> None:
    """Load Codebind into the active IPython user namespace."""
    previous = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(previous, dict):
        ipython.drop_by_id(previous)
    namespace: dict[str, Any] = {
        "chat": Session(shell=ipython),
        "Models": Models,
    }
    ipython.push(namespace)
    setattr(ipython, _NAMESPACE_ATTRIBUTE, namespace)


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    """Remove names added by Codebind without touching user replacements."""
    namespace = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(namespace, dict):
        ipython.drop_by_id(namespace)
        delattr(ipython, _NAMESPACE_ATTRIBUTE)
