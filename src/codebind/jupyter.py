"""Optional bridge from an IPython kernel to the Codebind JupyterLab extension."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from IPython.core.interactiveshell import InteractiveShell


_TARGET_NAME = "codebind"
_HANDSHAKE_TIMEOUT_SECONDS = 30
_Notebook = list[dict[str, Any]]
_QuestionHandler = Callable[[str, _Notebook], Awaitable[None]]


class JupyterLabBridge:
    """Send native notebook cells to a connected JupyterLab frontend."""

    def __init__(self, comm: Any) -> None:
        self._comm = comm
        self._question_handler: _QuestionHandler | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._conversation: dict[str, Any] | None = None
        self._conversation_received = False
        self._conversation_waiters: set[asyncio.Future[dict[str, Any] | None]] = set()
        self._save_waiters: dict[str, asyncio.Future[None]] = {}
        self.ready = False
        comm.on_msg(self._on_message)

    @classmethod
    def connect(cls, shell: InteractiveShell) -> JupyterLabBridge | None:
        """Open a frontend comm when running inside an IPython kernel."""
        if not hasattr(shell, "kernel"):
            return None

        try:
            from comm import create_comm  # pyright: ignore[reportMissingImports]

            return cls(create_comm(target_name=_TARGET_NAME))
        except (ImportError, RuntimeError):
            return None

    def close(self) -> None:
        """Close the frontend connection."""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for future in (*self._conversation_waiters, *self._save_waiters.values()):
            future.cancel()
        self._conversation_waiters.clear()
        self._save_waiters.clear()
        self._comm.close()
        self.ready = False

    def handle_questions(self, handler: _QuestionHandler) -> None:
        """Handle questions submitted by Codebind cells."""
        self._question_handler = handler

    async def load_conversation(self) -> dict[str, Any] | None:
        if self._conversation_received:
            return self._conversation
        future = asyncio.get_running_loop().create_future()
        self._conversation_waiters.add(future)
        try:
            return await asyncio.wait_for(future, _HANDSHAKE_TIMEOUT_SECONDS)
        finally:
            self._conversation_waiters.discard(future)

    async def save_conversation(self, value: dict[str, Any]) -> None:
        if not self.ready:
            await self.load_conversation()
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._save_waiters[request_id] = future
        self._comm.send(
            {
                "type": "conversation_save",
                "request_id": request_id,
                "conversation": value,
            }
        )
        try:
            await asyncio.wait_for(future, _HANDSHAKE_TIMEOUT_SECONDS)
        finally:
            self._save_waiters.pop(request_id, None)

    def start_code_cell(self, source: str) -> str | None:
        """Insert a running code cell before its execution begins."""
        if not self.ready:
            return None
        cell_id = str(uuid4())
        self._comm.send(
            {
                "type": "code_cell_started",
                "cell_id": cell_id,
                "source": source,
            }
        )
        return cell_id

    def finish_code_cell(
        self,
        cell_id: str,
        execution_count: int | None,
        outputs: list[dict[str, Any]],
    ) -> bool:
        """Finish a previously inserted code cell with its native outputs."""
        if not self.ready:
            return False
        self._comm.send(
            {
                "type": "code_cell_finished",
                "cell_id": cell_id,
                "execution_count": execution_count,
                "outputs": outputs,
            }
        )
        return True

    def append_code_output(self, cell_id: str, output: dict[str, Any]) -> bool:
        """Append one live output to a running code cell."""
        if not self.ready:
            return False
        self._comm.send(
            {
                "type": "code_cell_output",
                "cell_id": cell_id,
                "output": output,
            }
        )
        return True

    def clear_code_output(self, cell_id: str, *, wait: bool = False) -> bool:
        """Clear a running code cell's outputs."""
        if not self.ready:
            return False
        self._comm.send(
            {
                "type": "code_cell_clear",
                "cell_id": cell_id,
                "wait": wait,
            }
        )
        return True

    def insert_markdown_cell(self, source: str) -> bool:
        """Insert one rendered Markdown cell through JupyterLab."""
        if not self.ready:
            return False
        self._comm.send({"type": "markdown_cell", "source": source})
        return True

    def ensure_instructions_cell(self, source: str) -> bool:
        """Show the conversation's immutable system instructions in the notebook."""
        if not self.ready:
            return False
        self._comm.send({"type": "instructions_cell", "source": source})
        return True

    def report_session_ready(self, error: BaseException | None = None) -> None:
        """Tell JupyterLab whether persisted conversation state is ready."""
        self._comm.send(
            {
                "type": "session_ready",
                "error": (
                    {"type": type(error).__name__, "message": str(error)}
                    if error is not None
                    else None
                ),
            }
        )

    def _on_message(self, message: dict[str, Any]) -> None:
        data = message.get("content", {}).get("data", {})
        if not isinstance(data, dict):
            return
        if data.get("type") == "ready":
            conversation = data.get("conversation")
            self._conversation = conversation if isinstance(conversation, dict) else None
            self._conversation_received = True
            self.ready = True
            for future in tuple(self._conversation_waiters):
                if not future.done():
                    future.set_result(self._conversation)
            return
        if data.get("type") == "conversation_saved":
            request_id = data.get("request_id")
            future = self._save_waiters.get(request_id) if isinstance(request_id, str) else None
            if future is None or future.done():
                return
            error = data.get("error")
            if isinstance(error, str) and error:
                future.set_exception(RuntimeError(error))
            else:
                future.set_result(None)
            return
        if data.get("type") == "cancel":
            for task in tuple(self._tasks):
                task.cancel()
            return
        if data.get("type") != "question":
            return
        cell_id = data.get("cell_id")
        question = data.get("question")
        notebook = data.get("notebook")
        if not all(isinstance(value, str) for value in (cell_id, question)) or not isinstance(
            notebook, list
        ):
            return
        self._comm.send({"type": "question_started", "cell_id": cell_id})
        task = asyncio.create_task(self._answer_question(cell_id, question, notebook))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_question(
        self,
        cell_id: str,
        question: str,
        notebook: _Notebook,
    ) -> None:
        error: dict[str, str] | None = None
        cancelled = False
        try:
            if self._question_handler is None:
                raise RuntimeError("Codebind is not ready to receive questions.")
            await self._question_handler(question, notebook)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exception:
            error = {"type": type(exception).__name__, "message": str(exception)}
        self._comm.send(
            {
                "type": "question_finished",
                "cell_id": cell_id,
                "error": error,
                "cancelled": cancelled,
            }
        )
