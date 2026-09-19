"""Conversation state and the single IPython-tool model loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from IPython import get_ipython
from IPython.core.interactiveshell import InteractiveShell
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable

from .display import display_assistant
from .execution import ExecutionReport, IPythonExecutor
from .jupyter import JupyterLabBridge


IPYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "ipython",
        "description": (
            "Execute an IPython cell in the state-persistent session shared with the user."
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


def _message_text(message: AIMessage) -> str:
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
    ) -> None:
        resolved_shell = shell or get_ipython()
        if resolved_shell is None:
            raise RuntimeError("Session must be created inside IPython or given an IPython shell.")

        self.shell = resolved_shell
        self.bridge = bridge
        self.executor = IPythonExecutor(resolved_shell, bridge)
        self.instructions = instructions.strip() if instructions else None
        self.messages: list[BaseMessage] = []
        self.last_response: AIMessage | None = None
        if self.instructions:
            self.messages.append(SystemMessage(self.instructions))

    def clear(self) -> None:
        """Clear conversation history without clearing the shared Python namespace."""
        self.messages.clear()
        self.last_response = None
        if self.instructions:
            self.messages.append(SystemMessage(self.instructions))

    def send(self, prompt: str, model: BaseChatModel) -> None:
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

        bound_model = self._bind(model)
        self.messages.append(HumanMessage(text))

        while True:
            response = bound_model.invoke(tuple(self.messages))
            if not isinstance(response, AIMessage):
                raise TypeError("model must return an AIMessage")
            self.messages.append(response)
            self.last_response = response

            if not response.tool_calls:
                answer = _message_text(response)
                if answer:
                    self._display_assistant(answer)
                return

            for call in response.tool_calls:
                report = self._execute_call(call)
                identifier = str(call.get("id") or f"ipython-{len(self.messages)}")
                self.messages.append(
                    ToolMessage(
                        json.dumps(report.as_dict(), ensure_ascii=False),
                        tool_call_id=identifier,
                    )
                )

    async def asend(self, prompt: str, model: BaseChatModel) -> None:
        """Send one user message asynchronously."""
        text = prompt.strip()
        if not text:
            raise ValueError("prompt cannot be empty")

        bound_model = self._bind(model)
        self.messages.append(HumanMessage(text))

        while True:
            response = await bound_model.ainvoke(tuple(self.messages))
            if not isinstance(response, AIMessage):
                raise TypeError("model must return an AIMessage")
            self.messages.append(response)
            self.last_response = response

            if not response.tool_calls:
                answer = _message_text(response)
                if answer:
                    self._display_assistant(answer)
                return

            for call in response.tool_calls:
                report = await self._aexecute_call(call)
                identifier = str(call.get("id") or f"ipython-{len(self.messages)}")
                self.messages.append(
                    ToolMessage(
                        json.dumps(report.as_dict(), ensure_ascii=False),
                        tool_call_id=identifier,
                    )
                )

    @staticmethod
    def _bind(model: BaseChatModel) -> Runnable[Any, BaseMessage]:
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

        try:
            report = self.executor.execute(arguments["cell"])
        except Exception as error:  # The failure must be returned to the model, not end the session.
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
