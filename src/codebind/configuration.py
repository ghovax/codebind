"""XDG-backed Codebind configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models_provider import Models


def configuration_home() -> Path:
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        path = Path(xdg_home).expanduser()
        if path.is_absolute():
            return path
    return Path.home() / ".config"


@dataclass(frozen=True, slots=True)
class CodebindConfiguration:
    model: str
    parameters: dict[str, Any] = field(default_factory=dict)


def load_configuration() -> CodebindConfiguration:
    path = configuration_home() / "codebind" / "configuration.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Codebind configuration is missing: {path}. "
            'Create it with a JSON object such as {"model": "openai/gpt-5.6-luna"}.'
        )
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not isinstance(value.get("model"), str):
        raise ValueError(f"Codebind configuration must contain a string model: {path}")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError(f"Codebind model parameters must be a JSON object: {path}")
    return CodebindConfiguration(value["model"], parameters)


def load_models() -> Models:
    path = configuration_home() / "codebind" / "models.json"
    if not path.exists():
        return Models()
    values = json.loads(path.read_text())
    if not isinstance(values, dict):
        raise ValueError(f"Codebind model configuration must be a JSON object: {path}")
    return Models(values)


__all__ = [
    "CodebindConfiguration",
    "configuration_home",
    "load_configuration",
    "load_models",
]
