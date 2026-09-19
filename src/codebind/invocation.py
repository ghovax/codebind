"""Independent, awaitable agent invocations."""

from __future__ import annotations

import asyncio
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage


InvocationStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
OutcomeStatus = Literal["completed", "failed", "cancelled"]
current_invocation_id: ContextVar[str | None] = ContextVar(
    "codebind_current_invocation_id",
    default=None,
)


def message_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray, str)):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class Outcome:
    """The single terminal outcome of an invocation."""

    status: OutcomeStatus
    value: AIMessage | None = None
    error: BaseException | None = None

    def __post_init__(self) -> None:
        if self.status == "completed" and (self.value is None or self.error is not None):
            raise ValueError("a completed outcome requires only a value")
        if self.status == "failed" and (self.value is not None or self.error is None):
            raise ValueError("a failed outcome requires only an error")
        if self.status == "cancelled" and (self.value is not None or self.error is not None):
            raise ValueError("a cancelled outcome cannot contain a value or error")

    @property
    def text(self) -> str:
        """Return the final assistant text, if the invocation completed."""
        return message_text(self.value) if self.value is not None else ""


InvocationRunner = Callable[[list[BaseMessage]], Awaitable[AIMessage]]


class Invocation:
    """One independent, repeated model/tool run with an immutable starting history."""

    def __init__(
        self,
        prompt: str,
        history: Sequence[BaseMessage],
        runner: InvocationRunner | None,
        registry: InvocationRegistry,
    ) -> None:
        self.id = str(uuid4())
        self.parent_id = current_invocation_id.get()
        self.input = prompt
        self._starting_history = tuple(history)
        self._new_history: list[BaseMessage] = []
        self._runner = runner
        self._status: InvocationStatus = "pending"
        self._outcome: Outcome | None = None
        self._task: asyncio.Task[object] | None = None
        self._before_wait: Callable[[], None] | None = None
        registry.register(self)

    @property
    def history(self) -> tuple[BaseMessage, ...]:
        """Return the frozen starting history followed by this invocation's history."""
        return (*self._starting_history, *self._new_history)

    @property
    def status(self) -> InvocationStatus:
        """Return the current lifecycle state."""
        return self._status

    @property
    def outcome(self) -> Outcome | None:
        """Return the terminal outcome when available."""
        return self._outcome

    def start(self) -> Invocation:
        """Start in the active event loop, or remain pending until awaited."""
        if self._task is not None or self._outcome is not None:
            return self
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self
        self._task = loop.create_task(self._run())
        return self

    def cancel(self) -> None:
        """Request cancellation of a running invocation."""
        if self._task is not None:
            self._task.cancel()
        elif self._outcome is None:
            self._finish(Outcome("cancelled"))

    def __await__(self) -> Generator[Any, None, Outcome]:
        return self._wait().__await__()

    def __repr__(self) -> str:
        return (
            f"Invocation(id={self.id!r}, parent_id={self.parent_id!r}, "
            f"status={self.status!r})"
        )

    async def _wait(self) -> Outcome:
        if self._outcome is not None:
            return self._outcome
        if self._task is None and self._before_wait is not None:
            before_wait = self._before_wait
            self._before_wait = None
            before_wait()
        self.start()
        if self._task is None:
            raise RuntimeError("invocation could not start without an active event loop")
        if self._task is asyncio.current_task():
            raise RuntimeError("an invocation cannot await itself")
        await self._task
        if self._outcome is None:
            raise RuntimeError("invocation ended without an outcome")
        return self._outcome

    async def _run(self) -> Outcome:
        if self._runner is None:
            raise RuntimeError("inline invocation has no independent runner")
        self._status = "running"
        token = current_invocation_id.set(self.id)
        try:
            response = await self._runner(self._new_history)
        except asyncio.CancelledError:
            self._finish(Outcome("cancelled"))
        except Exception as error:
            self._finish(Outcome("failed", error=error))
        else:
            self._finish(Outcome("completed", value=response))
        finally:
            current_invocation_id.reset(token)
        outcome = self._outcome
        if outcome is None:
            raise RuntimeError("invocation ended without an outcome")
        return outcome

    @contextmanager
    def _inline(self) -> Iterator[list[BaseMessage]]:
        self._status = "running"
        try:
            self._task = asyncio.current_task()
        except RuntimeError:
            self._task = None
        token = current_invocation_id.set(self.id)
        try:
            yield self._new_history
        finally:
            current_invocation_id.reset(token)

    def _finish(self, outcome: Outcome) -> None:
        if self._outcome is not None:
            raise RuntimeError("invocation outcome is already set")
        self._outcome = outcome
        self._status = outcome.status

    def _when_waited(self, callback: Callable[[], None]) -> None:
        self._before_wait = callback


class InvocationRegistry:
    """Own invocation identities and serialize shared-IPython execution by lineage."""

    def __init__(self) -> None:
        self._items: dict[str, Invocation] = {}
        self._condition = asyncio.Condition()
        self._execution_stack: list[str] = []

    @property
    def invocations(self) -> tuple[Invocation, ...]:
        """Return invocations in creation order."""
        return tuple(self._items.values())

    def register(self, invocation: Invocation) -> None:
        """Register one invocation."""
        self._items[invocation.id] = invocation

    @asynccontextmanager
    async def execution(self) -> AsyncIterator[None]:
        """Permit nested descendants while serializing unrelated IPython executions."""
        invocation_id = current_invocation_id.get()
        task = asyncio.current_task()
        owner = invocation_id or f"task:{id(task)}"

        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._execution_stack
                or owner == self._execution_stack[-1]
                or self._is_descendant(owner, self._execution_stack[-1])
            )
            self._execution_stack.append(owner)

        try:
            yield
        finally:
            async with self._condition:
                if not self._execution_stack or self._execution_stack[-1] != owner:
                    raise RuntimeError("IPython execution lineage became inconsistent")
                self._execution_stack.pop()
                self._condition.notify_all()

    def _is_descendant(self, invocation_id: str, ancestor_id: str) -> bool:
        current = self._items.get(invocation_id)
        while current is not None and current.parent_id is not None:
            if current.parent_id == ancestor_id:
                return True
            current = self._items.get(current.parent_id)
        return False
