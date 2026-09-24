"""IPython extension entry points."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from IPython.core.interactiveshell import InteractiveShell
from IPython.core.page import page
from IPython.terminal.interactiveshell import TerminalInteractiveShell
from langchain_core.language_models import BaseChatModel

from .configuration import load_configuration, load_models
from .conversation import MemoryConversationStore, NotebookConversationStore
from .jupyter import JupyterLabBridge, TARGET_NAME
from .session import Session
from .terminal import TerminalCellHistory, install_markdown_renderer, install_question_mode


_STATE_ATTRIBUTE = "_codebind_extension_state"


def _close_state(state: dict[str, Any]) -> None:
    restore_output_magic = state.get("restore_output_magic")
    if callable(restore_output_magic):
        restore_output_magic()
    terminal_history = state.get("terminal_history")
    if isinstance(terminal_history, TerminalCellHistory):
        terminal_history.close()
    restore_terminal_question_mode = state.get("restore_terminal_question_mode")
    if callable(restore_terminal_question_mode):
        restore_terminal_question_mode()
    restore_terminal_display = state.get("restore_terminal_display")
    if callable(restore_terminal_display):
        restore_terminal_display()
    manager = state.get("comm_manager")
    target = state.get("comm_target")
    if manager is not None and getattr(manager, "targets", {}).get(TARGET_NAME) is target:
        manager.unregister_target(TARGET_NAME, target)
    load_task = state.get("load_task")
    if isinstance(load_task, asyncio.Task):
        load_task.cancel()
    session = state.get("session")
    if isinstance(session, Session) and session.bridge is not None:
        session.bridge.close()
    discard_model = state.get("discard_model")
    if callable(discard_model):
        try:
            asyncio.get_running_loop().create_task(discard_model())
        except RuntimeError:
            asyncio.run(discard_model())


def load_ipython_extension(ipython: InteractiveShell) -> None:
    """Load Codebind into the active IPython session."""
    previous = getattr(ipython, _STATE_ATTRIBUTE, None)
    previous_terminal_history = (
        previous.get("terminal_history") if isinstance(previous, dict) else None
    )
    if isinstance(previous, dict):
        _close_state(previous)

    configuration = load_configuration()
    models = load_models()
    model: BaseChatModel | None = None

    def get_model() -> BaseChatModel:
        nonlocal model
        if model is None:
            model = models.chat(configuration.model, **configuration.parameters)
        return model

    async def discard_model() -> None:
        nonlocal model
        selected = model
        model = None
        close = getattr(selected, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    manager = getattr(getattr(ipython, "kernel", None), "comm_manager", None)
    state: dict[str, Any] = {
        "session": None,
        "bridge": None,
        "load_task": None,
        "get_model": get_model,
        "discard_model": discard_model,
        "comm_manager": manager,
        "comm_target": None,
        "restore_terminal_display": None,
        "restore_terminal_question_mode": None,
        "terminal_history": None,
        "restore_output_magic": None,
    }

    if manager is None:
        state["session"] = Session(shell=ipython, store=MemoryConversationStore())
        if isinstance(ipython, TerminalInteractiveShell):
            state["terminal_history"] = TerminalCellHistory(
                ipython,
                previous=(
                    previous_terminal_history
                    if isinstance(previous_terminal_history, TerminalCellHistory)
                    else None
                ),
            )
            state["restore_terminal_display"] = install_markdown_renderer(ipython)
            state["restore_terminal_question_mode"] = install_question_mode(ipython)

            line_magics = ipython.magics_manager.magics["line"]
            previous_output_magic = line_magics.get("output")

            def output_magic(line: str) -> None:
                number_text = line.strip()
                if not number_text.isdecimal() or int(number_text) < 1:
                    raise ValueError("Usage: %output <cell number>")
                terminal_history = state["terminal_history"]
                if not isinstance(terminal_history, TerminalCellHistory):
                    raise RuntimeError("Terminal history is unavailable")
                number = int(number_text)
                try:
                    full_output = terminal_history.output_text(number)
                except KeyError as error:
                    raise ValueError(f"No recorded cell In [{number}]") from error
                if full_output:
                    page(f"\x1b[90m{full_output}\x1b[0m" if sys.stdout.isatty() else full_output)
                else:
                    print(f"In [{number}] has no text output.")

            ipython.register_magic_function(output_magic, "line", "output")

            def restore_output_magic() -> None:
                if line_magics.get("output") is output_magic:
                    if previous_output_magic is None:
                        line_magics.pop("output", None)
                    else:
                        line_magics["output"] = previous_output_magic

            state["restore_output_magic"] = restore_output_magic
    else:

        def accept_comm(comm: Any, message: dict[str, Any]) -> None:
            data = message.get("content", {}).get("data", {})
            if not isinstance(data, dict):
                raise ValueError("Invalid Codebind notebook connection")
            conversation = data.get("conversation")
            if conversation is not None and not isinstance(conversation, dict):
                raise ValueError("Invalid Codebind conversation")

            previous_task = state.get("load_task")
            if isinstance(previous_task, asyncio.Task):
                previous_task.cancel()
            previous_bridge = state.get("bridge")
            if isinstance(previous_bridge, JupyterLabBridge):
                previous_bridge.close()

            bridge = JupyterLabBridge(comm, conversation)
            session = Session(
                shell=ipython,
                bridge=bridge,
                store=NotebookConversationStore(bridge),
            )

            async def answer_question(question: str, notebook: list[dict[str, Any]]) -> None:
                selected = get_model()
                try:
                    await session.asend(question, selected, notebook=notebook)
                except BaseException:
                    await discard_model()
                    raise

            async def prepare_session() -> None:
                try:
                    await session.aload()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if bridge.ready:
                        bridge.report_session_ready(error)
                else:
                    if bridge.ready:
                        bridge.report_session_ready()

            bridge.handle_questions(answer_question)
            state["bridge"] = bridge
            state["session"] = session
            state["load_task"] = asyncio.get_running_loop().create_task(prepare_session())

        manager.register_target(TARGET_NAME, accept_comm)
        state["comm_target"] = accept_comm

    def question_magic(line: str, cell: str | None = None) -> None:
        session = state.get("session")
        if not isinstance(session, Session):
            session = Session(shell=ipython, store=MemoryConversationStore())
            state["session"] = session
        selected = get_model()
        terminal_history = state.get("terminal_history")
        history = (
            terminal_history.snapshot()
            if isinstance(terminal_history, TerminalCellHistory)
            else None
        )
        try:
            session.send(cell if cell is not None else line, selected, notebook=history)
        except BaseException:
            asyncio.run(discard_model())
            raise

    ipython.register_magic_function(question_magic, "line_cell", "question")
    setattr(ipython, _STATE_ATTRIBUTE, state)


def unload_ipython_extension(ipython: InteractiveShell) -> None:
    """Unload Codebind without touching the user namespace."""
    state: Any = getattr(ipython, _STATE_ATTRIBUTE, None)
    if isinstance(state, dict):
        _close_state(state)
        delattr(ipython, _STATE_ATTRIBUTE)
    ipython.magics_manager.magics["line"].pop("question", None)
    ipython.magics_manager.magics["cell"].pop("question", None)
