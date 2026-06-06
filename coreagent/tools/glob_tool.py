"""glob：按模式匹配文件路径（免确认）。

遍历与文件名匹配用标准库 `glob`（支持相对与绝对 pattern、`**` 递归）；gitignore 过滤交给
`pathspec`（声明式、语义正确，不手写忽略规则）。默认排除被 .gitignore 命中的路径与 `.git` 目录。
"""

import glob as globmod
from pathlib import Path

import pathspec

from coreagent.tools.base import Tool, ToolResult


class GlobTool(Tool):
    name = "glob"
    description = "按 glob 模式（如 **/*.py）匹配文件路径，自动跳过 .gitignore 命中项与 .git 目录。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，例如 **/*.py 或 src/*.txt"},
            "path": {"type": "string", "description": "搜索根目录，默认当前目录"},
        },
        "required": ["pattern"],
    }
    requires_confirmation = False

    def execute(self, arguments: dict) -> ToolResult:
        pattern = arguments.get("pattern", "")
        root = Path(arguments.get("path") or ".").resolve()
        if not root.is_dir():
            return ToolResult.fail(f"glob 失败：目录不存在：{root}")

        spec = self._load_gitignore(root)
        matches: list[str] = []
        # 相对 pattern 相对 root 搜索并返回相对路径；绝对 pattern 直接生效（root_dir 被忽略）。
        for m in globmod.glob(pattern, root_dir=str(root), recursive=True, include_hidden=True):
            full = root / m  # m 为绝对路径时即其本身（pathlib 拼接吸收绝对路径）。
            try:
                rel = full.resolve().relative_to(root)
                in_root = True
            except ValueError:
                in_root = False

            # 恒排除 .git 目录下任何路径。
            parts = rel.parts if in_root else full.parts
            if ".git" in parts:
                continue

            if in_root:
                rel_str = rel.as_posix()
                # 目录用带斜杠形式喂给 pathspec，以命中 `dir/` 这类忽略规则。
                probe = rel_str + "/" if full.is_dir() else rel_str
                if spec.match_file(probe):
                    continue
                matches.append(rel_str)
            else:
                # root 外（绝对 pattern 指向别处）：不做 gitignore 过滤，原样返回。
                matches.append(str(full))

        matches.sort()
        if not matches:
            return ToolResult.ok("（无匹配）")
        return ToolResult.ok("\n".join(matches))

    @staticmethod
    def _load_gitignore(root: Path) -> pathspec.PathSpec:
        gi = root / ".gitignore"
        lines: list[str] = []
        if gi.is_file():
            try:
                lines = gi.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        # pathspec 1.x 用 "gitignore"，旧版（>=0.12）仅有 "gitwildmatch"——两者皆兼容。
        try:
            return pathspec.PathSpec.from_lines("gitignore", lines)
        except (KeyError, ValueError, LookupError):
            return pathspec.PathSpec.from_lines("gitwildmatch", lines)
