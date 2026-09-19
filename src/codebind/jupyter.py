"""Optional bridge from an IPython kernel to the Codebind JupyterLab extension."""

from __future__ import annotations

from typing import Any

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
        if isinstance(data, dict) and data.get("type") == "ready":
            self.ready = True
