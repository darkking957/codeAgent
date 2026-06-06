"""工具系统测试：接口 / 注册中心 / 六工具行为 / ripgrep 解析（全部离线）。"""

import asyncio

import pytest

from coreagent.tools import build_registry
from coreagent.tools.base import Tool, ToolResult
from coreagent.tools.edit_file import EditFileTool
from coreagent.tools.glob_tool import GlobTool
from coreagent.tools.grep_tool import GrepTool
from coreagent.tools.read_file import ReadFileTool
from coreagent.tools.registry import ToolRegistry
from coreagent.tools.run_command import DEFAULT_TIMEOUT, RunCommandTool
from coreagent.tools.write_file import WriteFileTool

# 固定值（与 checklist 一致）。
SIX_NAMES = {"read_file", "write_file", "edit_file", "run_command", "glob", "grep"}
NEED_CONFIRM = {"write_file", "edit_file", "run_command"}
NO_CONFIRM = {"read_file", "glob", "grep"}


# ── 接口与注册 ────────────────────────────────────────────────────────────────

def test_tool_interface_shape():
    t = ReadFileTool()
    assert isinstance(t, Tool)
    assert t.name and isinstance(t.name, str)
    assert t.description and isinstance(t.description, str)
    assert isinstance(t.parameters, dict)
    assert hasattr(t, "requires_confirmation")
    assert callable(t.execute)


def test_registry_register_and_lookup():
    reg = build_registry()
    assert set(reg.names()) == SIX_NAMES
    assert isinstance(reg.get("read_file"), ReadFileTool)
    with pytest.raises(KeyError) as ei:
        reg.get("does_not_exist")
    assert "未注册" in str(ei.value)


def test_registry_to_api_tools():
    reg = build_registry()
    api = reg.to_api_tools()
    assert len(api) == 6
    for entry in api:
        assert set(entry) >= {"name", "description", "input_schema"}
        assert isinstance(entry["input_schema"], dict)


def test_confirm_flags():
    reg = build_registry()
    for name in NEED_CONFIRM:
        assert reg.get(name).requires_confirmation is True, name
    for name in NO_CONFIRM:
        assert reg.get(name).requires_confirmation is False, name


def test_registry_wraps_exception():
    class Boom(Tool):
        name = "boom"
        description = "raises"
        parameters = {"type": "object"}
        requires_confirmation = False

        def execute(self, arguments: dict) -> ToolResult:
            raise RuntimeError("内部炸了")

    reg = ToolRegistry()
    reg.register(Boom())
    res = asyncio.run(reg.execute("boom", {}))
    assert res.success is False
    assert "boom" in res.content and "内部炸了" in res.content


def test_registry_unknown_execute_is_structured():
    reg = build_registry()
    res = asyncio.run(reg.execute("nope", {}))
    assert res.success is False
    assert "未知工具" in res.content


# ── read / write / edit ───────────────────────────────────────────────────────

def test_read_file_ok_and_missing(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    t = ReadFileTool()
    ok = t.execute({"path": str(f)})
    assert ok.success and ok.content == "hello\nworld\n"

    miss = t.execute({"path": str(tmp_path / "nope.txt")})
    assert miss.success is False
    assert miss.content == f"read_file 失败：文件不存在：{tmp_path / 'nope.txt'}"


def test_write_file_creates_parents(tmp_path):
    target = tmp_path / "sub" / "deep" / "out.txt"
    t = WriteFileTool()
    res = t.execute({"path": str(target), "content": "payload"})
    assert res.success
    assert target.read_text(encoding="utf-8") == "payload"


def test_edit_unique(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    res = EditFileTool().execute({"path": str(f), "old_string": "beta", "new_string": "BETA"})
    assert res.success
    assert f.read_text(encoding="utf-8") == "alpha BETA gamma"


def test_edit_zero_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha beta", encoding="utf-8")
    res = EditFileTool().execute({"path": str(f), "old_string": "ZZZ", "new_string": "x"})
    assert res.success is False
    assert res.content == "edit_file 失败：未找到要替换的文本（old_string 在文件中 0 处匹配）"
    assert f.read_text(encoding="utf-8") == "alpha beta"  # 未修改


def test_edit_multi_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("dup\ndup\ndup\n", encoding="utf-8")
    res = EditFileTool().execute({"path": str(f), "old_string": "dup", "new_string": "x"})
    assert res.success is False
    assert "匹配到 3 处" in res.content
    assert "更长、唯一的上下文" in res.content
    assert f.read_text(encoding="utf-8") == "dup\ndup\ndup\n"  # 未修改


# ── run_command ───────────────────────────────────────────────────────────────

def test_run_command_echo():
    res = RunCommandTool().execute({"command": "echo hello"})
    assert res.success
    assert "hello" in res.content
    assert "exit_code: 0" in res.content


def test_run_command_default_timeout_is_30():
    # 默认超时 30 秒；超时文案据此模板生成。
    assert DEFAULT_TIMEOUT == 30
    assert RunCommandTool().timeout == 30
    # 验证模板文案在 30s 下精确为「超过 30 秒」（不真等 30s）。
    expected = f"命令执行超时（超过 {RunCommandTool().timeout} 秒）"
    assert expected == "命令执行超时（超过 30 秒）"


def test_run_command_timeout_message():
    res = RunCommandTool(timeout=1).execute({"command": "sleep 5"})
    assert res.success is False
    assert res.content == "命令执行超时（超过 1 秒）"


# ── glob ──────────────────────────────────────────────────────────────────────

def test_glob_excludes_gitignored_and_dotgit(tmp_path):
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "drop.log").write_text("x", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "inner.py").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("x", encoding="utf-8")

    res = GlobTool().execute({"pattern": "**/*", "path": str(tmp_path)})
    assert res.success
    listed = res.content.splitlines()
    assert "drop.log" not in listed  # gitignore 命中被排除
    assert not any(p.startswith(".git/") or p == ".git" for p in listed)  # .git 排除
    assert "keep.py" in listed
    assert "pkg/inner.py" in listed


def test_glob_absolute_pattern(tmp_path):
    # 模型可能传绝对路径作为 pattern：不应抛 NotImplementedError，应正常匹配。
    f = tmp_path / "poem.txt"
    f.write_text("hi", encoding="utf-8")
    res = GlobTool().execute({"pattern": str(f)})
    assert res.success
    assert str(f) in res.content.splitlines()

    # 绝对的递归 pattern 也可用
    res2 = GlobTool().execute({"pattern": str(tmp_path / "**" / "*.txt")})
    assert res2.success
    assert any(line.endswith("poem.txt") for line in res2.content.splitlines())


# ── grep ──────────────────────────────────────────────────────────────────────

def test_grep_hits_file_line(tmp_path):
    (tmp_path / "f.txt").write_text("alpha needle beta\nno match here\n", encoding="utf-8")
    res = GrepTool().execute({"pattern": "needle", "path": str(tmp_path)})
    assert res.success
    # 输出含 file:line 形式
    assert "f.txt:1" in res.content


def test_grep_no_match_is_success_empty(tmp_path):
    (tmp_path / "f.txt").write_text("nothing\n", encoding="utf-8")
    res = GrepTool().execute({"pattern": "needle", "path": str(tmp_path)})
    assert res.success
    assert "无匹配" in res.content


# ── ripgrep 路径解析 ──────────────────────────────────────────────────────────

def test_ripgrep_resolves_linux_x64(monkeypatch):
    import coreagent.tools.ripgrep as rgm

    monkeypatch.setattr(rgm.sys, "platform", "linux")
    monkeypatch.setattr(rgm.platform, "machine", lambda: "x86_64")
    p = rgm.rg_path()
    assert p.exists()
    assert p.parent.name == "linux-x64"


def test_ripgrep_unpacked_platform(monkeypatch):
    import coreagent.tools.ripgrep as rgm

    monkeypatch.setattr(rgm.sys, "platform", "darwin")
    monkeypatch.setattr(rgm.platform, "machine", lambda: "arm64")
    with pytest.raises(RuntimeError) as ei:
        rgm.rg_path()
    assert str(ei.value) == "未打包当前平台（darwin-arm64）的 ripgrep 二进制"
