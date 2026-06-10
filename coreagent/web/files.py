"""项目根内文件浏览 + 路径安全（#0023）。

code 会话绑定一个真实项目根；前端要能浏览根内**任意**目录 / 文件（不限于 agent 碰过的）。本模块
提供两个纯函数：列目录树 / 安全读文件，**均限定在根内**——解析 realpath 后越界（含 ``..`` 跳出 /
符号链接逃逸）一律拒绝，绝不读到根外文件。

纯函数、只依赖标准库；**绝不**被引擎 / 入口 / profile / 工具反向 import（只活在 web 层）。
"""

from pathlib import Path

# 列树时跳过的重型 / 噪声目录（仍列出目录本身、但不展开其子树，避免一次性回传爆量）。
_IGNORE_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
# 目录树节点上限（防超大仓库一次性回传爆量；非命令输出截断，与 checklist⑤ 无关）。
_MAX_NODES = 4000
# 单文件读取字节上限（防超大文件拖垮）。
_MAX_READ_BYTES = 1_000_000


def safe_resolve(root: str | Path, rel: str) -> Path | None:
    """把 ``rel`` 解析到 ``root`` 内的真实路径；越界（``..`` / 符号链接逃逸）→ 返回 None。

    解析顺序：``(root/rel).resolve()`` 取 realpath，再校验其在 ``root.resolve()`` 之内。空 / "." 视作根。
    """
    root_p = Path(root).resolve()
    target = (root_p / (rel or ".")).resolve()
    try:
        target.relative_to(root_p)
    except ValueError:
        return None
    return target


def list_tree(root: str | Path) -> list[dict]:
    """递归列出 ``root`` 内目录树（dirs-first 排序、跳过重型目录、节点上限封顶）。

    每个节点 = ``{"name","path","type"}``，目录另带 ``children``（重型目录置空不展开）。``path`` 是
    相对 ``root`` 的相对路径（供前端再调读文件端点）。root 不存在 → 空列表。
    """
    root_p = Path(root).resolve()
    if not root_p.is_dir():
        return []
    counter = {"n": 0}

    def walk(d: Path) -> list[dict]:
        children: list[dict] = []
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return children
        for entry in entries:
            if counter["n"] >= _MAX_NODES:
                break
            counter["n"] += 1
            rel = str(entry.relative_to(root_p))
            if entry.is_dir():
                if entry.name in _IGNORE_DIRS:
                    children.append({"name": entry.name, "path": rel, "type": "dir", "children": []})
                else:
                    children.append(
                        {"name": entry.name, "path": rel, "type": "dir", "children": walk(entry)}
                    )
            else:
                children.append({"name": entry.name, "path": rel, "type": "file"})
        return children

    return walk(root_p)


def read_within(root: str | Path, rel: str) -> tuple[str, str | None]:
    """安全读取 ``root`` 内文件，返回 ``(status, text)``：

    - ``("denied", None)``：路径越界（跳出根）→ 端点转 400/403，绝不读根外文件；
    - ``("missing", None)``：不存在 / 非普通文件 / 读取出错 → 端点转 404；
    - ``("ok", text)``：成功（截至 ``_MAX_READ_BYTES``，非 UTF-8 字节按 replace 兜底）。
    """
    target = safe_resolve(root, rel)
    if target is None:
        return "denied", None
    if not target.is_file():
        return "missing", None
    try:
        data = target.read_bytes()[:_MAX_READ_BYTES]
    except OSError:
        return "missing", None
    return "ok", data.decode("utf-8", errors="replace")
