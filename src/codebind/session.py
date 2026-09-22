"""Conversation state and the single IPython-tool model loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Any
from uuid import uuid4

from IPython import get_ipython
from IPython.core.interactiveshell import InteractiveShell
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
    messages_from_dict,
)
from langchain_core.messages.ai import add_ai_message_chunks
from langchain_core.runnables import Runnable

from .display import display_assistant
from .conversation import Conversation, ConversationStore, MemoryConversationStore
from .execution import ExecutionReport, IPythonExecutor
from .jupyter import JupyterLabBridge


def _default_instructions() -> str:
    return files("codebind").joinpath("instructions.md").read_text().strip()


IPYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "ipython",
        "description": (
            "Execute one complete IPython cell in the state-persistent session shared with "
            "the user. Use it proactively whenever local inspection or action can help, and "
            "batch related work into one cell when practical."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cell": {
                    "type": "string",
                    "description": "A complete IPython cell to execute.",
                }
            },
            "required": ["cell"],
            "additionalProperties": False,
        },
    },
}


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray, str)):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _tool_error(error_type: str, message: str) -> ExecutionReport:
    return ExecutionReport(False, "", "", None, (), {"type": error_type, "message": message})


class Session:
    """Keep conversation history while choosing the model independently for every turn."""

    def __init__(
        self,
        *,
        shell: InteractiveShell | None = None,
        instructions: str | None = None,
        bridge: JupyterLabBridge | None = None,
        store: ConversationStore | None = None,
    ) -> None:
        resolved_shell = shell or get_ipython()
        if resolved_shell is None:
            raise RuntimeError("Session must be created inside IPython or given an IPython shell.")

        self.shell = resolved_shell
        self.bridge = bridge
        self.executor = IPythonExecutor(resolved_shell, bridge)
        self.store = store or MemoryConversationStore()
        self.instructions = (instructions or _default_instructions()).strip()
        self.conversation: Conversation | None = None
        self._messages: list[BaseMessage] = []
        self.last_response: AIMessage | None = None
        self._load_lock = asyncio.Lock()

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        return tuple(self._messages)

    async def _ensure_loaded(self) -> None:
        if self.conversation is not None:
            return
        async with self._load_lock:
            if self.conversation is not None:
                return
            value = await self.store.load()
            self.conversation = (
                Conversation.from_dict(value)
                if value is not None
                else Conversation(id=getattr(self.store, "conversation_id", str(uuid4())))
            )
            self._messages = self.conversation.messages()
            self.last_response = next(
                (message for message in reversed(self._messages) if isinstance(message, AIMessage)),
                None,
            )
            unfinished = self.conversation.unfinished_turns()
            for identifier, turn_id in self.conversation.pending_tool_calls(set(unfinished)):
                message = self._interrupted_tool_message(identifier)
                self.conversation.append_message(message, turn_id)
                self._messages.append(message)
            for turn_id in unfinished:
                self.conversation.append("turn_finished", turn_id=turn_id, status="interrupted")
            if unfinished:
                await self._save()
            if not self.conversation.events:
                if self.bridge is not None:
                    self.bridge.ensure_instructions_cell(self.instructions)
                await self._append_message(SystemMessage(self.instructions), "system")
            elif self.bridge is not None:
                system = next(
                    (message for message in self._messages if isinstance(message, SystemMessage)),
                    None,
                )
                if system is not None and isinstance(system.content, str):
                    self.bridge.ensure_instructions_cell(system.content)

    async def aload(self) -> None:
        """Load persisted state and prepare the frontend without starting a turn."""
        await self._ensure_loaded()

    def _ensure_loaded_sync(self) -> None:
        asyncio.run(self._ensure_loaded())

    async def _save(self) -> None:
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        await self.store.save(self.conversation.as_dict())

    async def _append_event(self, event_type: str, **data: Any) -> None:
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        event = self.conversation.append(event_type, **data)
        try:
            await self._save()
        except Exception:
            self.conversation.rollback(event)
            raise

    async def _append_message(self, message: BaseMessage, turn_id: str) -> None:
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        event = self.conversation.append_message(message, turn_id)
        try:
            await self._save()
        except Exception:
            self.conversation.rollback(event)
            raise
        self._messages.append(message)

    async def _append_notebook(
        self,
        notebook: list[dict[str, Any]] | None,
        turn_id: str,
    ) -> None:
        if notebook is None:
            return
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        event = self.conversation.append_notebook(notebook, turn_id)
        if event is None:
            return
        try:
            await self._save()
        except Exception:
            self.conversation.rollback(event)
            raise
        self._messages.extend(messages_from_dict([event["message"]]))

    def _append_event_sync(self, event_type: str, **data: Any) -> None:
        asyncio.run(self._append_event(event_type, **data))

    def _append_message_sync(self, message: BaseMessage, turn_id: str) -> None:
        asyncio.run(self._append_message(message, turn_id))

    def _append_notebook_sync(
        self,
        notebook: list[dict[str, Any]] | None,
        turn_id: str,
    ) -> None:
        asyncio.run(self._append_notebook(notebook, turn_id))

    def _model_kwargs(self, model: BaseChatModel) -> dict[str, Any]:
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        if hasattr(model, "session_id"):
            return {"prompt_cache_key": self.conversation.id}
        return {}

    @staticmethod
    def _ensure_tool_call_ids(response: AIMessage) -> list[str]:
        identifiers: list[str] = []
        for call in response.tool_calls:
            identifier = call.get("id")
            if not isinstance(identifier, str) or not identifier:
                identifier = f"ipython-{uuid4()}"
                call["id"] = identifier
            identifiers.append(identifier)
        return identifiers

    @staticmethod
    def _interrupted_tool_message(identifier: str) -> ToolMessage:
        report = _tool_error("Interrupted", "The tool call was interrupted before completion.")
        return ToolMessage(
            json.dumps(report.as_dict(), ensure_ascii=False),
            tool_call_id=identifier,
            status="error",
        )

    async def _seal_interrupted_calls(self, identifiers: list[str], turn_id: str) -> None:
        for identifier in identifiers:
            await self._append_message(self._interrupted_tool_message(identifier), turn_id)

    def _seal_interrupted_calls_sync(self, identifiers: list[str], turn_id: str) -> None:
        for identifier in identifiers:
            self._append_message_sync(self._interrupted_tool_message(identifier), turn_id)

    def _update_assistant_stream(self, cell_id: str | None, text: str) -> str | None:
        if self.bridge is None or not text:
            return cell_id
        if cell_id is None:
            return self.bridge.start_markdown_cell(text)
        self.bridge.update_markdown_cell(cell_id, text)
        return cell_id

    def _finish_assistant_stream(self, cell_id: str | None, response: AIMessage) -> None:
        answer = _message_text(response)
        if cell_id is not None and self.bridge is not None:
            self.bridge.finish_markdown_cell(cell_id, answer)
        elif answer:
            self._display_assistant(answer)

    def _cancel_assistant_stream(self, cell_id: str | None) -> None:
        if cell_id is not None and self.bridge is not None:
            self.bridge.cancel_markdown_cell(cell_id)

    @staticmethod
    def _completed_stream_message(
        chunk: AIMessageChunk | None,
        response: AIMessage | None,
    ) -> AIMessage:
        if response is not None:
            return response
        if chunk is None:
            raise RuntimeError("model stream returned no messages")
        message = message_chunk_to_message(chunk)
        if not isinstance(message, AIMessage):
            raise TypeError("model stream must return an AIMessage")
        return message

    def _stream_model(
        self,
        bound_model: Runnable[Any, BaseMessage],
        model: BaseChatModel,
    ) -> tuple[AIMessage, str | None]:
        aggregate: AIMessageChunk | None = None
        response: AIMessage | None = None
        cell_id: str | None = None
        shown = ""
        try:
            for message in bound_model.stream(self.messages, **self._model_kwargs(model)):
                tool_call_started = bool(
                    message.tool_call_chunks if isinstance(message, AIMessageChunk) else False
                )
                if isinstance(message, AIMessageChunk):
                    aggregate = (
                        message
                        if aggregate is None
                        else add_ai_message_chunks(aggregate, message)
                    )
                    current = message_chunk_to_message(aggregate)
                elif isinstance(message, AIMessage) and aggregate is None and response is None:
                    response = message
                    current = message
                else:
                    raise TypeError("model stream must return AIMessage chunks")
                text = _message_text(current)
                if tool_call_started and text != shown:
                    cell_id = self._update_assistant_stream(cell_id, text)
                    shown = text
            completed = self._completed_stream_message(aggregate, response)
            answer = _message_text(completed)
            if answer != shown:
                cell_id = self._update_assistant_stream(cell_id, answer)
            return completed, cell_id
        except BaseException:
            self._cancel_assistant_stream(cell_id)
            raise

    async def _astream_model(
        self,
        bound_model: Runnable[Any, BaseMessage],
        model: BaseChatModel,
    ) -> tuple[AIMessage, str | None]:
        aggregate: AIMessageChunk | None = None
        response: AIMessage | None = None
        cell_id: str | None = None
        shown = ""
        try:
            async for message in bound_model.astream(self.messages, **self._model_kwargs(model)):
                tool_call_started = bool(
                    message.tool_call_chunks if isinstance(message, AIMessageChunk) else False
                )
                if isinstance(message, AIMessageChunk):
                    aggregate = (
                        message
                        if aggregate is None
                        else add_ai_message_chunks(aggregate, message)
                    )
                    current = message_chunk_to_message(aggregate)
                elif isinstance(message, AIMessage) and aggregate is None and response is None:
                    response = message
                    current = message
                else:
                    raise TypeError("model stream must return AIMessage chunks")
                text = _message_text(current)
                if tool_call_started and text != shown:
                    cell_id = self._update_assistant_stream(cell_id, text)
                    shown = text
            completed = self._completed_stream_message(aggregate, response)
            answer = _message_text(completed)
            if answer != shown:
                cell_id = self._update_assistant_stream(cell_id, answer)
            return completed, cell_id
        except BaseException:
            self._cancel_assistant_stream(cell_id)
            raise

    def clear(self) -> None:
        """Clear conversation history without clearing the shared Python namespace."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclear())
            return
        raise RuntimeError("clear() cannot run inside an active event loop; use await aclear().")

    async def aclear(self) -> None:
        self.conversation = Conversation()
        self._messages.clear()
        self.last_response = None
        await self._save()
        if self.instructions:
            await self._append_message(SystemMessage(self.instructions), "system")

    def send(
        self,
        prompt: str,
        model: BaseChatModel,
        *,
        notebook: list[dict[str, Any]] | None = None,
    ) -> None:
        """Send one user message synchronously."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("send() cannot run inside an active event loop; use await asend().")

        text = prompt.strip()
        if not text:
            raise ValueError("prompt cannot be empty")

        self._ensure_loaded_sync()
        turn_id = str(uuid4())
        self._append_event_sync("turn_started", turn_id=turn_id)
        bound_model = self._bind(model)
        pending_calls: list[str] = []
        assistant_cell_id: str | None = None
        try:
            self._append_notebook_sync(notebook, turn_id)
            self._append_message_sync(HumanMessage(text), turn_id)
            while True:
                response, assistant_cell_id = self._stream_model(bound_model, model)
                pending_calls = self._ensure_tool_call_ids(response)
                self._append_message_sync(response, turn_id)
                self.last_response = response
                self._finish_assistant_stream(assistant_cell_id, response)
                assistant_cell_id = None
                if not response.tool_calls:
                    self._append_event_sync("turn_finished", turn_id=turn_id, status="completed")
                    return

                for call, identifier in zip(response.tool_calls, tuple(pending_calls), strict=True):
                    report = self._execute_call(call)
                    self._append_message_sync(
                        ToolMessage(
                            json.dumps(report.as_dict(), ensure_ascii=False),
                            tool_call_id=identifier,
                        ),
                        turn_id,
                    )
                    pending_calls.remove(identifier)
        except KeyboardInterrupt:
            self._cancel_assistant_stream(assistant_cell_id)
            self._seal_interrupted_calls_sync(pending_calls, turn_id)
            self._append_event_sync("turn_finished", turn_id=turn_id, status="cancelled")
            raise
        except Exception:
            self._cancel_assistant_stream(assistant_cell_id)
            self._seal_interrupted_calls_sync(pending_calls, turn_id)
            self._append_event_sync("turn_finished", turn_id=turn_id, status="failed")
            raise

    async def asend(
        self,
        prompt: str,
        model: BaseChatModel,
        *,
        notebook: list[dict[str, Any]] | None = None,
    ) -> None:
        """Send one user message asynchronously."""
        text = prompt.strip()
        if not text:
            raise ValueError("prompt cannot be empty")

        await self._ensure_loaded()
        turn_id = str(uuid4())
        await self._append_event("turn_started", turn_id=turn_id)
        bound_model = self._bind(model)
        pending_calls: list[str] = []
        assistant_cell_id: str | None = None
        try:
            await self._append_notebook(notebook, turn_id)
            await self._append_message(HumanMessage(text), turn_id)
            while True:
                response, assistant_cell_id = await self._astream_model(bound_model, model)
                pending_calls = self._ensure_tool_call_ids(response)
                await self._append_message(response, turn_id)
                self.last_response = response
                self._finish_assistant_stream(assistant_cell_id, response)
                assistant_cell_id = None
                if not response.tool_calls:
                    await self._append_event("turn_finished", turn_id=turn_id, status="completed")
                    return

                for call, identifier in zip(response.tool_calls, tuple(pending_calls), strict=True):
                    report = await self._aexecute_call(call)
                    await self._append_message(
                        ToolMessage(
                            json.dumps(report.as_dict(), ensure_ascii=False),
                            tool_call_id=identifier,
                        ),
                        turn_id,
                    )
                    pending_calls.remove(identifier)
        except asyncio.CancelledError:
            self._cancel_assistant_stream(assistant_cell_id)
            await asyncio.shield(self._seal_interrupted_calls(pending_calls, turn_id))
            await asyncio.shield(
                self._append_event("turn_finished", turn_id=turn_id, status="cancelled")
            )
            raise
        except Exception:
            self._cancel_assistant_stream(assistant_cell_id)
            await self._seal_interrupted_calls(pending_calls, turn_id)
            await self._append_event("turn_finished", turn_id=turn_id, status="failed")
            raise

    def _bind(self, model: BaseChatModel) -> Runnable[Any, BaseMessage]:
        if self.conversation is None:
            raise RuntimeError("conversation is not loaded")
        try:
            return model.bind_tools([IPYTHON_TOOL], parallel_tool_calls=False)
        except NotImplementedError:
            return model.bind(tools=[IPYTHON_TOOL], parallel_tool_calls=False)

    def _display_assistant(self, answer: str) -> None:
        if self.bridge is None or not self.bridge.insert_markdown_cell(answer):
            display_assistant(answer)

    def _execute_call(self, call: Mapping[str, Any]) -> ExecutionReport:
        if call.get("name") != "ipython":
            return _tool_error("UnknownTool", f"unknown tool: {call.get('name')!r}")
        arguments = call.get("args")
        if not isinstance(arguments, Mapping) or not isinstance(arguments.get("cell"), str):
            return _tool_error("InvalidArguments", "ipython requires a string cell argument")

        # Execution failures are tool results; they do not end the model loop.
        try:
            report = self.executor.execute(arguments["cell"])
        except Exception as error:
            report = _tool_error(type(error).__name__, str(error))
        return report

    async def _aexecute_call(self, call: Mapping[str, Any]) -> ExecutionReport:
        if call.get("name") != "ipython":
            return _tool_error("UnknownTool", f"unknown tool: {call.get('name')!r}")
        arguments = call.get("args")
        if not isinstance(arguments, Mapping) or not isinstance(arguments.get("cell"), str):
            return _tool_error("InvalidArguments", "ipython requires a string cell argument")

        try:
            return await self.executor.aexecute(arguments["cell"])
        except Exception as error:
            return _tool_error(type(error).__name__, str(error))
