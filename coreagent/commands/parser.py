"""T3｜解析器与分流判定（#0011）。

把一行输入解析为「命令名 + 参数原文」或判为「非命令」：
  - 非 ``/`` 开头 → 返回 None（非命令，送 AI）。
  - ``/`` 开头 → 第一个空格前为命令名（转小写做大小写不敏感）、其后为参数原文
    （保留原始大小写与内部空格）。

空输入由主循环早返回、不入此函数（沿用 tui 既有 ``if not user_input: continue``）；
本函数对空 / 纯 ``/`` 仍 robust：返回 name 为空的 ParsedCommand，交注册中心解析为未命中。
"""

from dataclasses import dataclass


@dataclass
class ParsedCommand:
    """解析结果：命令名（小写、不含 /）+ 参数原文。"""

    name: str
    args: str


def parse(line: str) -> ParsedCommand | None:
    """解析一行输入；非命令返回 None，命令返回 ParsedCommand。"""
    if not isinstance(line, str) or not line.startswith("/"):
        return None
    rest = line[1:]
    parts = rest.split(maxsplit=1)
    name = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)
