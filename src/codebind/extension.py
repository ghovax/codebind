"""IPython extension entry points."""

from __future__ import annotations

from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from models_provider import Models

from .invocation import current_invocation_id
from .jupyter import JupyterLabBridge
from .session import Session


_NAMESPACE_ATTRIBUTE = "_codebind_extension_namespace"
_HOOKS_ATTRIBUTE = "_codebind_extension_hooks"


def _remove_hooks(ipython: InteractiveShell) -> None:
    hook = getattr(ipython, _HOOKS_ATTRIBUTE, None)
    if not callable(hook):
        return
    ipython.events.unregister("pre_run_cell", hook)
    delattr(ipython, _HOOKS_ATTRIBUTE)


def _install_hooks(ipython: InteractiveShell) -> None:
    def pre_run_cell(info: Any) -> None:
        metadata = getattr(info, "cell_meta", None)
        invocation_id = (
            metadata.get("codebind_invocation_id") if isinstance(metadata, dict) else None
        )
        current_invocation_id.set(invocation_id if isinstance(invocation_id, str) else None)

    ipython.events.register("pre_run_cell", pre_run_cell)
    setattr(ipython, _HOOKS_ATTRIBUTE, pre_run_cell)


def load_ipython_extension(ipython: InteractiveShell) -> None:
    """Load Codebind into the active IPython user namespace."""
    _remove_hooks(ipython)
    previous = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(previous, dict):
        previous_chat = previous.get("chat")
        if isinstance(previous_chat, Session) and previous_chat.bridge is not None:
            previous_chat.bridge.close()
        ipython.drop_by_id(previous)
    bridge = JupyterLabBridge.connect(ipython)
    namespace: dict[str, Any] = {
        "chat": Session(shell=ipython, bridge=bridge),
        "Models": Models,
    }
    ipython.push(namespace)
    setattr(ipython, _NAMESPACE_ATTRIBUTE, namespace)
    _install_hooks(ipython)


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    """Remove names added by Codebind without touching user replacements."""
    _remove_hooks(ipython)
    namespace = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(namespace, dict):
        chat = namespace.get("chat")
        if isinstance(chat, Session) and chat.bridge is not None:
            chat.bridge.close()
        ipython.drop_by_id(namespace)
        delattr(ipython, _NAMESPACE_ATTRIBUTE)
