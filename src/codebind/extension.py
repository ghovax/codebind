"""IPython extension entry points."""

from __future__ import annotations

import asyncio
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from langchain_core.language_models import BaseChatModel

from .configuration import load_configuration, load_models
from .conversation import MemoryConversationStore, NotebookConversationStore
from .jupyter import JupyterLabBridge
from .session import Session


_STATE_ATTRIBUTE = "_codebind_extension_state"


def load_ipython_extension(ipython: InteractiveShell) -> None:
    """Load Codebind into the active IPython session."""
    previous = getattr(ipython, _STATE_ATTRIBUTE, None)
    if isinstance(previous, dict):
        previous_load = previous.get("load_task")
        if isinstance(previous_load, asyncio.Task):
            previous_load.cancel()
        previous_session = previous.get("session")
        if isinstance(previous_session, Session) and previous_session.bridge is not None:
            previous_session.bridge.close()

    configuration = load_configuration()
    models = load_models()
    model: BaseChatModel | None = None

    def get_model() -> BaseChatModel:
        nonlocal model
        if model is None:
            model = models.chat(configuration.model, **configuration.parameters)
        return model

    def discard_model(selected: BaseChatModel) -> None:
        nonlocal model
        if model is selected:
            model = None

    bridge = JupyterLabBridge.connect(ipython)
    store = NotebookConversationStore(bridge) if bridge is not None else MemoryConversationStore()
    session = Session(shell=ipython, bridge=bridge, store=store)

    async def answer_question(question: str, notebook: list[dict[str, Any]]) -> None:
        selected = get_model()
        try:
            await session.asend(question, selected, notebook=notebook)
        except BaseException:
            discard_model(selected)
            raise

    def question_magic(line: str, cell: str | None = None) -> None:
        selected = get_model()
        try:
            session.send(cell if cell is not None else line, selected)
        except BaseException:
            discard_model(selected)
            raise

    load_task: asyncio.Task[None] | None = None
    if bridge is not None:
        bridge.handle_questions(answer_question)

        async def prepare_session() -> None:
            try:
                await session.aload()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                bridge.report_session_ready(error)
            else:
                bridge.report_session_ready()

        try:
            load_task = asyncio.get_running_loop().create_task(prepare_session())
        except RuntimeError:
            pass
    ipython.register_magic_function(question_magic, "line_cell", "question")
    setattr(
        ipython,
        _STATE_ATTRIBUTE,
        {
            "session": session,
            "bridge": bridge,
            "load_task": load_task,
            "get_model": get_model,
        },
    )


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    """Unload Codebind without touching the user namespace."""
    state: Any = getattr(ipython, _STATE_ATTRIBUTE, None)
    if isinstance(state, dict):
        load_task = state.get("load_task")
        if isinstance(load_task, asyncio.Task):
            load_task.cancel()
        session = state.get("session")
        if isinstance(session, Session) and session.bridge is not None:
            session.bridge.close()
        delattr(ipython, _STATE_ATTRIBUTE)
    ipython.magics_manager.magics["line"].pop("question", None)
    ipython.magics_manager.magics["cell"].pop("question", None)
