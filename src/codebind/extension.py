"""IPython extension entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from langchain_core.language_models import BaseChatModel
from models_provider import Models

from .jupyter import JupyterLabBridge
from .session import Session


_NAMESPACE_ATTRIBUTE = "_codebind_extension_namespace"


def _models_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured:
        config_home = Path(configured).expanduser()
        if config_home.is_absolute():
            return config_home / "codebind" / "models.json"
    return Path.home() / ".config" / "codebind" / "models.json"


def _load_models() -> Models:
    path = _models_path()
    if not path.exists():
        return Models()
    values = json.loads(path.read_text())
    if not isinstance(values, dict):
        raise ValueError(f"Codebind model configuration must be a JSON object: {path}")
    return Models(values)


def load_ipython_extension(ipython: InteractiveShell) -> None:
    """Load Codebind into the active IPython user namespace."""
    models = _load_models()
    previous = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(previous, dict):
        previous_chat = previous.get("chat")
        if isinstance(previous_chat, Session) and previous_chat.bridge is not None:
            previous_chat.bridge.close()
        ipython.drop_by_id(previous)
    bridge = JupyterLabBridge.connect(ipython)
    chat = Session(shell=ipython, bridge=bridge)
    if bridge is not None:

        async def answer_question(question: str, model_name: str) -> None:
            model = ipython.user_ns.get(model_name)
            if not isinstance(model, BaseChatModel):
                raise NameError(f"{model_name!r} is not a chat model in the IPython namespace")
            await chat.asend(question, model)

        bridge.handle_questions(answer_question)
    namespace: dict[str, Any] = {
        "chat": chat,
        "Models": Models,
        "models": models,
    }
    ipython.push(namespace)
    setattr(ipython, _NAMESPACE_ATTRIBUTE, namespace)


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    """Remove names added by Codebind without touching user replacements."""
    namespace = getattr(ipython, _NAMESPACE_ATTRIBUTE, None)
    if isinstance(namespace, dict):
        chat = namespace.get("chat")
        if isinstance(chat, Session) and chat.bridge is not None:
            chat.bridge.close()
        ipython.drop_by_id(namespace)
        delattr(ipython, _NAMESPACE_ATTRIBUTE)
