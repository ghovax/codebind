"""Optional bridge from an IPython kernel to the Codebind JupyterLab extension."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from IPython.core.interactiveshell import InteractiveShell


_TARGET_NAME = "codebind"


class JupyterLabBridge:
    """Send native notebook cells to a connected JupyterLab frontend."""

    def __init__(self, comm: Any) -> None:
        self._comm = comm
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._invocation_starters: dict[str, Callable[[], None]] = {}
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
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._invocation_starters.clear()
        self._comm.close()
        self.ready = False

    def start_invocation(self, invocation_id: str, starter: Callable[[], None]) -> bool:
        """Anchor a background invocation to the currently executing notebook cell."""
        if not self.ready:
            return False
        self._invocation_starters[invocation_id] = starter
        self._comm.send({"type": "invocation_started", "invocation_id": invocation_id})
        return True

    async def execute_cell(self, invocation_id: str, source: str) -> dict[str, Any]:
        """Ask JupyterLab to insert and normally execute one invocation cell."""
        if not self.ready:
            raise RuntimeError("JupyterLab bridge is not connected")
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._comm.send(
            {
                "type": "execute_cell",
                "invocation_id": invocation_id,
                "request_id": request_id,
                "source": source,
            }
        )
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def complete_invocation(self, invocation_id: str, source: str) -> bool:
        """Finish a background invocation with an optional Markdown cell."""
        if not self.ready:
            return False
        self._comm.send(
            {
                "type": "invocation_completed",
                "invocation_id": invocation_id,
                "source": source,
            }
        )
        return True

    def insert_code_cell(
        self,
        source: str,
        execution_count: int | None,
        outputs: list[dict[str, Any]],
    ) -> bool:
        """Insert one executed code cell through JupyterLab."""
        if not self.ready:
            return False
        self._comm.send(
            {
                "type": "code_cell",
                "source": source,
                "execution_count": execution_count,
                "outputs": outputs,
            }
        )
        return True

    def insert_markdown_cell(self, source: str) -> bool:
        """Insert one rendered Markdown cell through JupyterLab."""
        if not self.ready:
            return False
        self._comm.send({"type": "markdown_cell", "source": source})
        return True

    def _on_message(self, message: dict[str, Any]) -> None:
        data = message.get("content", {}).get("data", {})
        if not isinstance(data, dict):
            return
        if data.get("type") == "ready":
            self.ready = True
            return
        if data.get("type") == "invocation_ready":
            invocation_id = data.get("invocation_id")
            if isinstance(invocation_id, str):
                starter = self._invocation_starters.pop(invocation_id, None)
                if starter is not None:
                    starter()
            return
        if data.get("type") != "execution_result":
            return
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = data.get("error")
        if isinstance(error, str):
            future.set_exception(RuntimeError(error))
        else:
            future.set_result(data)
