import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from coreagent.errors import ConfigError
from coreagent.worktree.constants import DEFAULT_CLEANUP_IDLE_HOURS

# 合法的 provider 取值。
_PROTOCOLS = ("anthropic", "openai")


class ThinkingConfig(BaseModel):
    enabled: bool = False
    budget_tokens: int = 10000


class MemoryConfig(BaseModel):
    """记忆系统（#0010）开关与阈值；缺省值让功能默认开启且安全。"""

    enabled: bool = True
    # 过期会话清理阈值（天）：超期的 per-session 目录在启动时被清理。
    retention_days: int = 30
    # 会话恢复时间跨度提醒阈值（小时）：距上次会话超过此值则注入时间跨度提醒。
    resume_gap_hours: int = 24
    # 每级笔记索引上限：行数 / 体积（字节）。
    index_max_lines: int = 200
    index_max_bytes: int = 25600
    # @include 嵌套深度上限（第 max_depth+1 层不再展开并报错）。
    include_max_depth: int = 4
    # 记忆根目录覆盖（默认 ~/.coreagent）；留空取默认，便于测试 / 自定义。
    root: str = ""

    @field_validator(
        "retention_days",
        "resume_gap_hours",
        "index_max_lines",
        "index_max_bytes",
        "include_max_depth",
    )
    @classmethod
    def _check_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(
                f"配置字段 memory.{info.field_name} 必须为正整数：当前为 {v}"
            )
        return v


class WorktreeConfig(BaseModel):
    """Git Worktree 隔离（#0015）开关与可覆盖项；缺省让功能默认开启且安全。

    固定值（根位置 / 分支前缀 / 字符集 / 长度上限）不可调，落 worktree.constants；本处仅承载
    可被用户增删覆盖的项。``enabled=False`` 时整体不启用隔离（声明隔离的角色委派将硬失败）。
    """

    enabled: bool = True
    # 空闲清理阈值（小时）：临时工作树空闲超此值且无变更保护触发即可被周期清理回收。
    cleanup_idle_hours: int = DEFAULT_CLEANUP_IDLE_HOURS
    # 软链进工作树的大型依赖目录（源存在才链）；覆盖即整体替换默认清单。
    symlink_dirs: list[str] = Field(default_factory=lambda: ["node_modules", ".venv"])
    # 复制进工作树的本地配置文件 glob；覆盖即整体替换默认清单。
    copy_globs: list[str] = Field(default_factory=lambda: [".env", ".env.*"])
    # 是否把工作树 hooksPath 指向主仓 hooks（使其提交跑同一套钩子）。
    link_git_hooks: bool = True

    @field_validator("cleanup_idle_hours")
    @classmethod
    def _check_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(
                f"配置字段 worktree.{info.field_name} 必须为正整数：当前为 {v}"
            )
        return v


class WebSearchConfig(BaseModel):
    """chat 模式服务端 web_search（#0024）开关与版本。

    - ``enabled``：是否为 chat 装配服务端 web_search 工具声明（端点须支持，否则触发搜索会优雅降级）。
    - ``version``：web_search 工具声明的 ``type`` 字符串。默认 ``web_search_20250305``（基线兼容版）；
      备选 ``web_search_20260209``（支持动态域过滤等，需较新模型）。
    - ``max_uses``：单次请求内 web_search 最多调用次数（服务端约束，防失控）。
    """

    enabled: bool = False
    version: str = "web_search_20250305"
    max_uses: int = 5

    @field_validator("max_uses")
    @classmethod
    def _check_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"配置字段 web.chat.web_search.max_uses 必须为正整数：当前为 {v}")
        return v


class ChatProviderConfig(BaseModel):
    """chat 模式**独立** provider（#0024）：指向支持 server-side web_search 的 Anthropic 兼容端点。

    与 code 用的主 provider **解耦**——code 走 DeepSeek 等不支持 web_search 的端点，chat 可单独指向
    真 Anthropic 端点。语义：
    - ``model`` 非空 → chat 用独立 provider（``protocol`` / ``api_key`` / ``base_url`` 空则回落主配置）；
      ``model`` 为空 → chat **复用主 provider**（此时若主端点不支持 web_search，触发搜索优雅降级）。
    - ``web_search``：web_search 开关与版本（与是否独立 provider 正交——可复用主 provider 仅开 web_search）。
    """

    protocol: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class WebConfig(BaseModel):
    """Web code 模式（#0023）：默认项目根 + 审批集覆盖；chat 独立 provider + web_search（#0024）。

    - ``code_root``：建 code 会话未显式传 ``project_root`` 时回落的**默认项目根**；空串 = 未配置
      （此时建 code 会话且未传根 → 显式报错，**不**静默建空目录）。
    - ``approval``：审批集覆盖（工具名列表）。``None`` = 用 profile 默认（写/改/执行三者）；
      列表（含空列表）= 整体覆盖「哪些工具需审批」。
    - ``chat``：chat 模式独立 provider + 服务端 web_search 配置（#0024，与 code 的 provider 解耦）。
    """

    code_root: str = ""
    approval: list[str] | None = None
    chat: ChatProviderConfig = Field(default_factory=ChatProviderConfig)


class Config(BaseModel):
    protocol: str
    model: str
    api_key: str
    base_url: str = ""
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    # 记忆系统（#0010）：项目指令 / 会话摘要 / 自动笔记的开关与阈值。
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # Git Worktree 隔离（#0015）：子 Agent 隔离工作区的开关 / 清理阈值 / 环境初始化清单。
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    # Web code 模式（#0023）：建 code 会话的默认项目根 + 审批集覆盖。
    web: WebConfig = Field(default_factory=WebConfig)
    # 输出上限（承接原 provider 中的 magic number，带默认值）。
    max_tokens: int = 8192            # 非思考模式
    thinking_max_tokens: int = 16000  # 思考模式
    # 上下文窗口（#0009）：第二层摘要触发阈值的总预算基准；缺省取默认、正整数校验。
    context_window: int = 200000
    # Max 资格占位开关（#0017）：语义代表 Max 订阅；门控 auto 档（Shift+Tab 循环 / /mode 均受控）。
    # 本期为占位布尔，将来替换为真实账号校验时只改只读访问器 is_max_enabled、不动调用点。
    max_enabled: bool = False

    @field_validator("context_window")
    @classmethod
    def _check_context_window(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                f"配置字段 context_window 必须为正整数：当前为 {v}"
            )
        return v

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

    # 嵌套 chat 独立 provider（#0024）的同名字段也解析 ${ENV}（api_key 等常从环境读）。
    web = data.get("web")
    if isinstance(web, dict):
        chat = web.get("chat")
        if isinstance(chat, dict):
            for key in ("protocol", "model", "base_url", "api_key"):
                val = chat.get(key)
                if isinstance(val, str):
                    chat[key] = _resolve_env_vars(val)

    # 规整可选空值，避免 None 被 pydantic 当作类型错误。
    if data.get("base_url") is None:
        data["base_url"] = ""
    if data.get("api_key") is None:
        data["api_key"] = ""
    if data.get("thinking") is None:
        data.pop("thinking", None)
    if data.get("memory") is None:
        data.pop("memory", None)
    if data.get("worktree") is None:
        data.pop("worktree", None)
    if data.get("web") is None:
        data.pop("web", None)

    try:
        return Config(**data)
    except ValidationError as e:
        raise ConfigError(f"配置校验失败：{_format_errors(e)}") from e


def is_max_enabled(config) -> bool:
    """只读访问器：读取 Max 资格占位开关（#0017）。

    将来替换为真实 Max 账号校验时只改本函数、不动调用点（TUI / 命令层经此读取）。
    缺字段 / 非法值 / 任意异常均回落 ``False``（fail-safe，不抛）。
    """
    try:
        return bool(getattr(config, "max_enabled", False))
    except Exception:  # noqa: BLE001 —— 访问器恒不抛，非法一律按未开启处理
        return False
