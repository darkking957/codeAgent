import os
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ThinkingConfig:
    enabled: bool = False
    budget_tokens: int = 10000


@dataclass
class Config:
    protocol: str
    model: str
    api_key: str
    base_url: str = ""
    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)


def _resolve_env_vars(value: str) -> str:
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"未找到 {path}")

    with open(p) as f:
        data = yaml.safe_load(f)

    for key in ("protocol", "model", "base_url", "api_key"):
        if key in data and isinstance(data[key], str):
            data[key] = _resolve_env_vars(data[key])

    thinking_data = data.get("thinking") or {}
    thinking = ThinkingConfig(
        enabled=bool(thinking_data.get("enabled", False)),
        budget_tokens=int(thinking_data.get("budget_tokens", 10000)),
    )

    return Config(
        protocol=data["protocol"],
        model=data["model"],
        api_key=data.get("api_key", ""),
        base_url=data.get("base_url", "") or "",
        thinking=thinking,
    )
