"""Optional bridge from an IPython kernel to the Codebind JupyterLab extension."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from IPython.core.interactiveshell import InteractiveShell


_TARGET_NAME = "codebind"


class JupyterLabBridge:
    """Send native notebook cells to a connected JupyterLab frontend."""

    def __init__(self, comm: Any) -> None:
        self._comm = comm
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
        self._comm.close()
        self.ready = False

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

    def _on_message(self, message: dict[str, Any]) -> None:
        data = message.get("content", {}).get("data", {})
        if isinstance(data, dict) and data.get("type") == "ready":
            self.ready = True
