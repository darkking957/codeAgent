import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from coreagent.errors import ConfigError

# 合法的 provider 取值。
_PROTOCOLS = ("anthropic", "openai")


class ThinkingConfig(BaseModel):
    enabled: bool = False
    budget_tokens: int = 10000


class Config(BaseModel):
    protocol: str
    model: str
    api_key: str
    base_url: str = ""
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    # 输出上限（承接原 provider 中的 magic number，带默认值）。
    max_tokens: int = 8192            # 非思考模式
    thinking_max_tokens: int = 16000  # 思考模式

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: str) -> str:
        if v not in _PROTOCOLS:
            raise ValueError(
                f"配置字段 protocol 取值非法：当前为 {v!r}，期望 'anthropic' 或 'openai'"
            )
        return v

    @field_validator("model")
    @classmethod
    def _check_model(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("配置字段 model 不能为空")
        return v

    @field_validator("api_key")
    @classmethod
    def _check_api_key(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "配置字段 api_key 不能为空（可用 ${ENV_VAR} 从环境变量读取，"
                "例如 api_key: ${ANTHROPIC_API_KEY}）"
            )
        return v

    @model_validator(mode="after")
    def _check_thinking_budget(self) -> "Config":
        if self.thinking.enabled and self.thinking.budget_tokens <= 0:
            raise ValueError(
                f"配置字段 thinking.budget_tokens 必须为正整数：当前为 {self.thinking.budget_tokens}"
            )
        return self


def _resolve_env_vars(value: str) -> str:
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def _format_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        # pydantic 把自定义 ValueError 包成 "Value error, <msg>"，去掉前缀让文案干净。
        msg = err["msg"].removeprefix("Value error, ")
        loc = ".".join(str(x) for x in err["loc"])
        # 自定义文案已含字段名；仅当 msg 未提及字段时补上 loc，保证"指明字段"。
        if loc and loc not in msg:
            parts.append(f"{loc}：{msg}")
        else:
            parts.append(msg)
    return "；".join(parts)


def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"未找到配置文件：{path}")

    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ConfigError("配置文件内容必须为 YAML 映射（key: value 形式）")

    # 先解析 ${ENV}，再交给 pydantic 校验。
    for key in ("protocol", "model", "base_url", "api_key"):
        val = data.get(key)
        if isinstance(val, str):
            data[key] = _resolve_env_vars(val)

    # 规整可选空值，避免 None 被 pydantic 当作类型错误。
    if data.get("base_url") is None:
        data["base_url"] = ""
    if data.get("api_key") is None:
        data["api_key"] = ""
    if data.get("thinking") is None:
        data.pop("thinking", None)

    try:
        return Config(**data)
    except ValidationError as e:
        raise ConfigError(f"配置校验失败：{_format_errors(e)}") from e
