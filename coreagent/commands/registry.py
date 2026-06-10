"""T2｜命令注册中心 + 启动期冲突检测（#0011）。

单一来源管理全部命令：登记、按名 / 别名解析、产出补全候选、可迭代非隐藏命令供 /help。
注册时命令名或别名与已有任意键（命令名或别名）冲突即抛 ``ValueError``——属配置 / 编码错误，
由装配层（main）捕获后中文报错 + ``sys.exit(1)``，fail-closed、不拖到运行时（仿
``tools/registry.py`` 重名抛 ValueError 范式）。
"""

from coreagent.commands.types import CommandSpec

# 冲突错误文案（见 checklist 固定值；装配层据此判定并退出）。
CONFLICT_MSG = "命令注册冲突：{key} 已被占用（命令名 / 别名不可重复）"


class CommandRegistry:
    """登记命令、按名 / 别名解析、产出补全候选与帮助清单。"""

    def __init__(self) -> None:
        # 命令名与别名同登记进同一张表（都解析到对应 spec）。
        self._by_key: dict[str, CommandSpec] = {}
        # 登记顺序（仅命令、不含别名条目）：供 /help 与补全按序展示。
        self._commands: list[CommandSpec] = []

    def register(self, spec: CommandSpec) -> None:
        """登记一条命令；命令名 / 任一别名与已有键冲突即抛 ValueError。"""
        keys = (spec.name, *spec.aliases)
        for key in keys:
            if key in self._by_key:
                raise ValueError(CONFLICT_MSG.format(key=key))
        for key in keys:
            self._by_key[key] = spec
        self._commands.append(spec)

    def resolve(self, name: str) -> CommandSpec | None:
        """按名 / 别名解析到命令（大小写不敏感）；未命中返回 None。"""
        if not isinstance(name, str):
            return None
        return self._by_key.get(name.lower())

    def visible(self) -> list[CommandSpec]:
        """非隐藏命令（登记顺序）：供 /help 生成清单。"""
        return [c for c in self._commands if not c.hidden]

    def completions(self, prefix: str) -> list[tuple[str, str]]:
        """补全候选 ``[(/名称, 描述), ...]``：按前缀匹配命令名，排除隐藏与别名条目。

        prefix 含前导 ``/``（如 ``/pl``）；返回的名称也含 ``/``，供前端裁剪补全。
        """
        out: list[tuple[str, str]] = []
        for c in self._commands:
            if c.hidden:
                continue
            full = "/" + c.name
            if full.startswith(prefix):
                out.append((full, c.summary))
        return out
