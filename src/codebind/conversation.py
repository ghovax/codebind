"""Durable conversation records and storage boundaries."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, message_to_dict, messages_from_dict

_VERSION = 2


class ConversationStore(Protocol):
    async def load(self) -> dict[str, Any] | None: ...

    async def save(self, value: dict[str, Any]) -> None: ...


class ConversationBridge(Protocol):
    async def load_conversation(self) -> dict[str, Any] | None: ...

    async def save_conversation(self, value: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class Conversation:
    id: str = field(default_factory=lambda: str(uuid4()))
    revision: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    cwd: str = field(default_factory=os.getcwd)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Conversation:
        if value.get("version") != _VERSION:
            raise ValueError("Unsupported Codebind conversation version")
        identifier = value.get("id")
        events = value.get("events")
        revision = value.get("revision")
        cwd = value.get("cwd")
        if not isinstance(identifier, str) or not isinstance(events, list):
            raise ValueError("Invalid Codebind conversation")
        if not isinstance(revision, int) or revision != len(events):
            raise ValueError("Invalid Codebind conversation revision")
        if any(
            not isinstance(event, dict) or event.get("sequence") != sequence
            for sequence, event in enumerate(events, start=1)
        ):
            raise ValueError("Invalid Codebind conversation sequence")
        if not isinstance(cwd, str):
            cwd = ""
        return cls(identifier, revision, deepcopy(events), cwd)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "id": self.id,
            "revision": self.revision,
            "cwd": self.cwd,
            "events": deepcopy(self.events),
        }

    def append(self, event_type: str, **data: Any) -> dict[str, Any]:
        event = {"sequence": self.revision + 1, "type": event_type, **data}
        self.events.append(event)
        self.revision += 1
        return event

    def append_message(self, message: BaseMessage, turn_id: str) -> dict[str, Any]:
        return self.append("message", turn_id=turn_id, message=message_to_dict(message))

    def append_notebook(
        self,
        cells: list[dict[str, Any]],
        turn_id: str,
    ) -> dict[str, Any] | None:
        current = self.notebook()
        incoming = _normalize_notebook(cells)
        has_notebook = any(event.get("type") == "notebook" for event in self.events)

        if not has_notebook:
            payload: dict[str, Any] = {"mode": "snapshot", "cells": incoming}
        else:
            current_by_id = {cell["id"]: cell for cell in current}
            incoming_by_id = {cell["id"]: cell for cell in incoming}
            upsert = [cell for cell in incoming if current_by_id.get(cell["id"]) != cell]
            remove = [cell["id"] for cell in current if cell["id"] not in incoming_by_id]
            current_order = [cell["id"] for cell in current]
            incoming_order = [cell["id"] for cell in incoming]
            if not upsert and not remove and current_order == incoming_order:
                return None
            payload = {"mode": "delta"}
            if upsert:
                payload["upsert"] = upsert
            if remove:
                payload["remove"] = remove
            if current_order != incoming_order:
                payload["order"] = incoming_order

        content = (
            "<notebook-context>"
            + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "</notebook-context>"
        )
        return self.append(
            "notebook",
            turn_id=turn_id,
            **payload,
            message=message_to_dict(HumanMessage(content)),
        )

    def rollback(self, event: dict[str, Any]) -> None:
        if not self.events or self.events[-1] is not event:
            raise RuntimeError("Only the latest conversation event can be rolled back")
        self.events.pop()
        self.revision -= 1

    def messages(self) -> list[BaseMessage]:
        serialized = [
            event["message"]
            for event in self.events
            if event.get("type") in {"message", "notebook"}
            and isinstance(event.get("message"), dict)
        ]
        return messages_from_dict(serialized)

    def notebook(self) -> list[dict[str, Any]]:
        cells: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("type") != "notebook":
                continue
            mode = event.get("mode")
            if mode == "snapshot":
                cells = _normalize_notebook(event.get("cells"))
                continue
            if mode != "delta":
                raise ValueError("Invalid Codebind notebook event")

            by_id = {cell["id"]: cell for cell in cells}
            for identifier in event.get("remove", []):
                if not isinstance(identifier, str):
                    raise ValueError("Invalid Codebind notebook removal")
                by_id.pop(identifier, None)
            for cell in _normalize_notebook(event.get("upsert", [])):
                by_id[cell["id"]] = cell

            order = event.get("order")
            if order is None:
                retained = [cell["id"] for cell in cells if cell["id"] in by_id]
                added = [identifier for identifier in by_id if identifier not in retained]
                order = [*retained, *added]
            if (
                not isinstance(order, list)
                or any(not isinstance(identifier, str) for identifier in order)
                or len(order) != len(set(order))
                or set(order) != set(by_id)
            ):
                raise ValueError("Invalid Codebind notebook order")
            cells = [by_id[identifier] for identifier in order]
        return deepcopy(cells)

    def unfinished_turns(self) -> list[str]:
        started: list[str] = []
        finished: set[str] = set()
        for event in self.events:
            turn_id = event.get("turn_id")
            if not isinstance(turn_id, str):
                continue
            if event.get("type") == "turn_started":
                started.append(turn_id)
            elif event.get("type") == "turn_finished":
                finished.add(turn_id)
        return [turn_id for turn_id in started if turn_id not in finished]

    def pending_tool_calls(self, turn_ids: set[str]) -> list[tuple[str, str]]:
        pending: dict[str, str] = {}
        for event in self.events:
            turn_id = event.get("turn_id")
            message = event.get("message")
            if turn_id not in turn_ids or not isinstance(message, dict):
                continue
            message_type = message.get("type")
            data = message.get("data")
            if not isinstance(data, dict):
                continue
            if message_type == "ai":
                tool_calls = data.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        identifier = call.get("id") if isinstance(call, dict) else None
                        if isinstance(identifier, str) and identifier:
                            pending[identifier] = turn_id
            elif message_type == "tool":
                identifier = data.get("tool_call_id")
                if isinstance(identifier, str):
                    pending.pop(identifier, None)
        return list(pending.items())


class MemoryConversationStore:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any] | None:
        return self.value

    async def save(self, value: dict[str, Any]) -> None:
        self.value = value


class NotebookConversationStore:
    def __init__(self, bridge: ConversationBridge):
        self.bridge = bridge

    async def load(self) -> dict[str, Any] | None:
        return await self.bridge.load_conversation()

    async def save(self, value: dict[str, Any]) -> None:
        await self.bridge.save_conversation(value)


def _normalize_notebook(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Invalid Codebind notebook snapshot")
    cells: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for raw_cell in value:
        if not isinstance(raw_cell, dict):
            raise ValueError("Invalid Codebind notebook cell")
        identifier = raw_cell.get("id")
        cell_type = raw_cell.get("type")
        source = raw_cell.get("source")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or cell_type not in {"code", "markdown", "raw"}
            or not isinstance(source, str)
        ):
            raise ValueError("Invalid Codebind notebook cell")
        identifiers.add(identifier)
        cell: dict[str, Any] = {"id": identifier, "type": cell_type, "source": source}
        if cell_type == "code":
            execution_count = raw_cell.get("execution_count")
            outputs = raw_cell.get("outputs", [])
            if not (execution_count is None or isinstance(execution_count, int)) or not isinstance(
                outputs, list
            ):
                raise ValueError("Invalid Codebind code cell")
            cell["execution_count"] = execution_count
            cell["outputs"] = deepcopy(outputs)
        cells.append(cell)
    return cells


__all__ = [
    "Conversation",
    "ConversationStore",
    "MemoryConversationStore",
    "NotebookConversationStore",
]
