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
from .invocation import (
    Invocation,
    InvocationRegistry,
    Outcome,
    current_invocation_id,
    message_text,
)
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
        _visible: bool = True,
        _registry: InvocationRegistry | None = None,
        _invocation_id: str | None = None,
    ) -> None:
        resolved_shell = shell or get_ipython()
        if resolved_shell is None:
            raise RuntimeError("Session must be created inside IPython or given an IPython shell.")

        self.shell = resolved_shell
        self.bridge = bridge
        self._visible = _visible
        self._registry = _registry or InvocationRegistry()
        self.executor = IPythonExecutor(
            resolved_shell,
            bridge,
            visible=_visible,
            registry=self._registry,
            invocation_id=_invocation_id,
        )
        self._invocation_id = _invocation_id
        self.instructions = instructions.strip() if instructions else None
        self.messages: list[BaseMessage] = []
        self.last_response: AIMessage | None = None
        if self.instructions:
            self.messages.append(SystemMessage(self.instructions))

    @property
    def invocations(self) -> tuple[Invocation, ...]:
        """Return every invocation created by this runtime in creation order."""
        return self._registry.invocations

    def clear(self) -> None:
        """Clear conversation history without clearing the shared Python namespace."""
        self.messages.clear()
        self.last_response = None
        if self.instructions:
            self.messages.append(SystemMessage(self.instructions))

    def invoke(
        self,
        prompt: str,
        model: BaseChatModel,
        *,
        history: Sequence[BaseMessage] = (),
    ) -> Invocation:
        """Start an independent agent invocation from an immutable history snapshot."""
        text = self._validate_prompt(prompt)
        starting_history = tuple(history)
        if not all(isinstance(message, BaseMessage) for message in starting_history):
            raise TypeError("history must contain only BaseMessage instances")

        bridge = self.bridge
        notebook_invocation = (
            current_invocation_id.get() is None
            and bridge is not None
            and bridge.ready
        )
        child: Session

        async def run(new_history: list[BaseMessage]) -> AIMessage:
            try:
                return await child._arun(text, model, new_history)
            except BaseException:
                if notebook_invocation and bridge is not None:
                    bridge.complete_invocation(invocation.id, "")
                raise

        invocation = Invocation(text, starting_history, run, self._registry)
        child = Session(
            shell=self.shell,
            bridge=bridge if notebook_invocation else None,
            _visible=notebook_invocation,
            _registry=self._registry,
            _invocation_id=invocation.id if notebook_invocation else None,
        )
        child.messages.extend(starting_history)
        if notebook_invocation and bridge is not None:
            def start() -> None:
                invocation.start()

            def start_inline() -> None:
                child._invocation_id = None
                child.executor.invocation_id = None
                invocation.start()

            invocation._when_waited(start_inline)
            bridge.start_invocation(invocation.id, start)
            return invocation
        return invocation.start()

    def send(self, prompt: str, model: BaseChatModel) -> None:
        """Run one visible agent invocation synchronously."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("send() cannot run inside an active event loop; use await asend().")

        text = self._validate_prompt(prompt)
        invocation = Invocation(text, tuple(self.messages), None, self._registry)
        with invocation._inline() as new_history:
            try:
                response = self._run(text, model, new_history)
            except BaseException as error:
                invocation._finish(Outcome("failed", error=error))
                raise
            invocation._finish(Outcome("completed", value=response))

    async def asend(self, prompt: str, model: BaseChatModel) -> None:
        """Run one visible agent invocation asynchronously."""
        text = self._validate_prompt(prompt)
        invocation = Invocation(text, tuple(self.messages), None, self._registry)
        with invocation._inline() as new_history:
            try:
                response = await self._arun(text, model, new_history)
            except asyncio.CancelledError:
                invocation._finish(Outcome("cancelled"))
                raise
            except BaseException as error:
                invocation._finish(Outcome("failed", error=error))
                raise
            invocation._finish(Outcome("completed", value=response))

    def _run(
        self,
        prompt: str,
        model: BaseChatModel,
        new_history: list[BaseMessage],
    ) -> AIMessage:
        bound_model = self._bind(model)
        self._append(HumanMessage(prompt), new_history)

        while True:
            response = bound_model.invoke(tuple(self.messages))
            if not isinstance(response, AIMessage):
                raise TypeError("model must return an AIMessage")
            self._append(response, new_history)
            self.last_response = response

            if not response.tool_calls:
                self._finish_response(response)
                return response

            for call in response.tool_calls:
                report = self._execute_call(call)
                self._append(self._tool_message(call, report), new_history)

    async def _arun(
        self,
        prompt: str,
        model: BaseChatModel,
        new_history: list[BaseMessage],
    ) -> AIMessage:
        bound_model = self._bind(model)
        self._append(HumanMessage(prompt), new_history)

        while True:
            response = await bound_model.ainvoke(tuple(self.messages))
            if not isinstance(response, AIMessage):
                raise TypeError("model must return an AIMessage")
            self._append(response, new_history)
            self.last_response = response

            if not response.tool_calls:
                self._finish_response(response)
                return response

            for call in response.tool_calls:
                report = await self._aexecute_call(call)
                self._append(self._tool_message(call, report), new_history)

    @staticmethod
    def _validate_prompt(prompt: str) -> str:
        text = prompt.strip()
        if not text:
            raise ValueError("prompt cannot be empty")
        return text

    @staticmethod
    def _bind(model: BaseChatModel) -> Runnable[Any, BaseMessage]:
        try:
            return model.bind_tools([IPYTHON_TOOL], parallel_tool_calls=False)
        except NotImplementedError:
            return model.bind(tools=[IPYTHON_TOOL], parallel_tool_calls=False)

    def _append(self, message: BaseMessage, new_history: list[BaseMessage]) -> None:
        self.messages.append(message)
        new_history.append(message)

    def _finish_response(self, response: AIMessage) -> None:
        answer = message_text(response)
        if self._invocation_id is not None and self.bridge is not None:
            self.bridge.complete_invocation(self._invocation_id, answer)
        elif answer and self._visible:
            self._display_assistant(answer)

    @staticmethod
    def _tool_message(call: Mapping[str, Any], report: ExecutionReport) -> ToolMessage:
        identifier = str(call.get("id") or "ipython")
        return ToolMessage(
            json.dumps(report.as_dict(), ensure_ascii=False),
            tool_call_id=identifier,
        )

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
            return self.executor.execute(arguments["cell"])
        except Exception as error:
            return _tool_error(type(error).__name__, str(error))

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
