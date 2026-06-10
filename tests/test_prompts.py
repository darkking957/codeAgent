"""提示词装配 / 环境快照 / 动态注入测试（#0006 T1/T2/T3/T7）。"""

import os

import coreagent.environment as environment
from coreagent.environment import (
    GIT_UNAVAILABLE,
    build_env_block,
    collect_env_snapshot,
    format_env_block,
)
from coreagent.injection import build_reminder
from coreagent.prompts import build_system_prompt

_MODULE_KEYS = ["身份", "行为", "工具使用", "代码规范", "安全边界", "任务模式", "输出风格"]


# ── T1：7 模块有序拼接 ────────────────────────────────────────────────────────────

def test_system_prompt_contains_seven_modules():
    s = build_system_prompt(["read_file"])
    assert all(k in s for k in _MODULE_KEYS)


def test_system_prompt_appends_tool_list():
    s = build_system_prompt(["read_file"])
    assert "read_file" in s


# ── T7：双重强化（系统提示词侧；工具描述侧由 grep 验，见 checklist）──────────────────

def test_rule_a_in_system_prompt():
    assert "优先使用专用工具" in build_system_prompt([])


def test_rule_b_in_system_prompt():
    assert "编辑前先读" in build_system_prompt([])


# ── T2：环境快照 ──────────────────────────────────────────────────────────────────

def test_env_block_has_four_fields():
    blk = build_env_block()
    assert all(k in blk for k in ["cwd", "os", "date", "git"])


def test_env_block_is_tagged():
    blk = build_env_block()
    assert "<env>" in blk and "</env>" in blk


def test_git_subprocess_called_once(monkeypatch):
    """采集快照时 git 子进程只调一次（会话内）。"""
    calls = {"n": 0}
    real_run = environment.subprocess.run

    def counting_run(cmd, *a, **kw):
        if cmd and cmd[0] == "git":
            calls["n"] += 1
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(environment.subprocess, "run", counting_run)
    collect_env_snapshot()
    assert calls["n"] == 1


def test_non_git_dir_degrades_without_raising(tmp_path, monkeypatch):
    """非 git 目录降级不抛错，git 字段为「非 git 仓库 / 不可用」。"""
    monkeypatch.chdir(tmp_path)
    snap = collect_env_snapshot()  # 不抛异常
    assert snap["git"] == GIT_UNAVAILABLE
    blk = format_env_block(snap)
    assert GIT_UNAVAILABLE in blk


# ── T3：动态注入 + 两档节奏 ───────────────────────────────────────────────────────

def test_plan_reminder_full_first_round():
    r = build_reminder(plan_only=True, round_index=1)
    assert r is not None
    assert "plan 模式" in r and "会被记录" in r and "不会执行" in r


def test_plan_reminder_brief_from_round_two():
    full = build_reminder(plan_only=True, round_index=1)
    brief = build_reminder(plan_only=True, round_index=2)
    assert brief is not None
    assert "plan 模式" in brief
    assert "会被记录" not in brief          # 精简档不含整句
    assert len(brief) < len(full)          # 明显短于全量


def test_no_plan_reminder_when_not_plan():
    r = build_reminder(plan_only=False, round_index=1)
    assert r is None or "plan 模式" not in r


def test_reminder_is_tagged():
    r = build_reminder(plan_only=True, round_index=1)
    assert "<system-reminder>" in r and "</system-reminder>" in r
