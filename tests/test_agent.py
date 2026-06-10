"""多轮 Agent Loop 测试（离线、假 provider）。"""

import json
import tempfile
import threading
import time
from pathlib import Path

from coreagent.agent import (
    CANCELLED_MESSAGE,
    EXIT_CANCELLED,
    EXIT_CAP,
    EXIT_NATURAL,
    MAX_ROUNDS,
    PLAN_RECORDED_MESSAGE,
    REJECTED_MESSAGE,
    run_agent_turn,
)
from coreagent.conversation import Conversation
from coreagent.providers.base import ChunkType
from coreagent.tools.base import Tool, ToolResult
from coreagent.tools.registry import ToolRegistry

from .conftest import (
    FakeStatusError,
    ScriptedProvider,
    done_chunk,
    text_chunk,
    tool_call,
    tool_use_done,
    tools_done,
)

# ── 测试用假工具 ──────────────────────────────────────────────────────────────

class _FakeRead(Tool):
    """读类（免确认）假工具。可选：按参数 delay 延时、barrier 验并发、execute 时置取消、返回失败。"""

    description = "读"
    parameters = {"type": "object"}
    requires_confirmation = False

    def __init__(self, name="read_file", *, barrier=None, cancel_on_execute=None, fail=False):
        self.name = name
        self._barrier = barrier
        self._cancel_on_execute = cancel_on_execute
        self._fail = fail
        self.calls: list[dict] = []
        self.entered: list[float] = []
        self.exited: list[float] = []

    def execute(self, arguments: dict) -> ToolResult:
        self.entered.append(time.monotonic())
        if self._cancel_on_execute is not None:
            # 在「正在执行」时置取消令牌：验证循环仍等其跑完、保留真实结果。
            self._cancel_on_execute.set()
        if self._barrier is not None:
            try:
                self._barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                self.exited.append(time.monotonic())
                return ToolResult.fail("barrier broken (serial execution)")
        delay = arguments.get("delay", 0)
        if delay:
            time.sleep(delay)
        self.calls.append(arguments)
        self.exited.append(time.monotonic())
        if self._fail:
            return ToolResult.fail(f"FAIL:{arguments.get('path')}")
        return ToolResult.ok(f"CONTENT:{arguments.get('path')}")


class _FakeWrite(Tool):
    """写类（需确认）假工具。"""

    description = "写"
    parameters = {"type": "object"}
    requires_confirmation = True

    def __init__(self, name="write_file"):
        self.name = name
        self.calls: list[dict] = []

    def execute(self, arguments: dict) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult.ok("written")


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


async def _drain(agen):
    chunks = []
    async for c in agen:
        chunks.append(c)
    return chunks


def _has_block(conv, block_type):
    return any(
        isinstance(m["content"], list) and any(b.get("type") == block_type for b in m["content"])
        for m in conv.messages
    )


def _blocks_of_type(conv, block_type):
    return [
        b
        for m in conv.messages
        if isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == block_type
    ]


def _of_type(chunks, chunk_type):
    return [c for c in chunks if c.type == chunk_type]


# ── 单轮编排与回合边界（#0003 行为回归）────────────────────────────────────────

async def test_one_round_tool_then_text():
    read = _FakeRead()
    prov = ScriptedProvider([
        [tool_use_done("read_file", {"path": "a.txt"})],
        [text_chunk("文件内容是 X"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("读 a.txt")
    await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))

    assert prov.calls == 2                       # 两次模型调用（一轮工具 + 一轮收尾）
    assert read.calls == [{"path": "a.txt"}]     # 工具被执行一次
    assert _has_block(conv, "tool_use")          # assistant 含 tool_use
    assert _has_block(conv, "tool_result")       # user 含 tool_result
    assert conv.messages[-1] == {"role": "assistant", "content": "文件内容是 X"}


async def test_tools_seen_carries_api_list():
    """自然终止：无工具调用时循环恰一轮（prov.calls == 1）。"""
    read = _FakeRead()
    prov = ScriptedProvider([[text_chunk("hi"), done_chunk()]])
    conv = Conversation()
    conv.add_user("hi")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))
    assert prov.calls == 1
    # 编排把工具清单传给 provider
    assert prov.tools_seen[0] is not None
    assert prov.tools_seen[0][0]["name"] == "read_file"
    # 单轮：仅一个 TURN_START；循环结束原因 = 自然。
    assert len(_of_type(chunks, ChunkType.TURN_START)) == 1
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_NATURAL


# ── T2 多轮循环与终止 ─────────────────────────────────────────────────────────

async def test_loop_multi_round_until_no_tool():
    """连环：读 a → 读 b → 文本，三轮后自然终止；模型被调 3 次，两读各执行一次。"""
    read = _FakeRead()
    prov = ScriptedProvider([
        [tool_use_done("read_file", {"path": "a.txt"}, call_id="c1")],
        [tool_use_done("read_file", {"path": "b.txt"}, call_id="c2")],
        [text_chunk("两个文件都读完了"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("读 a 和 b")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))

    assert prov.calls == 3
    assert read.calls == [{"path": "a.txt"}, {"path": "b.txt"}]
    assert conv.messages[-1] == {"role": "assistant", "content": "两个文件都读完了"}
    # 三类循环级信号都 emit 了，且自然收尾。
    assert len(_of_type(chunks, ChunkType.TURN_START)) == 3
    assert len(_of_type(chunks, ChunkType.TURN_END)) == 3
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_NATURAL


async def test_loop_stops_at_round_cap():
    """永动脚本（每轮都出工具调用）：prov.calls == 25 后停，emit 循环结束（原因 = 上限）。"""
    read = _FakeRead()
    # 单段脚本被复用：每次调用都返回一个工具调用 → 永不自然终止。
    prov = ScriptedProvider([[tool_use_done("read_file", {"path": "x"})]])
    conv = Conversation()
    conv.add_user("永动")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))

    assert prov.calls == MAX_ROUNDS == 25
    loop_done = _of_type(chunks, ChunkType.LOOP_DONE)
    assert loop_done and loop_done[-1].exit_reason == EXIT_CAP


async def test_loop_emits_loop_error_on_unrecoverable():
    """provider 在重试耗尽后抛不可恢复异常 → emit 循环错误（带错误类型），而非裸上抛、非循环结束(上限)。"""
    prov = ScriptedProvider([[FakeStatusError(400)]])  # 400 不可重试
    conv = Conversation()
    conv.add_user("go")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(_FakeRead()), "sys"))

    errors = _of_type(chunks, ChunkType.LOOP_ERROR)
    assert errors and errors[-1].error_type == "FakeStatusError"
    assert _of_type(chunks, ChunkType.LOOP_DONE) == []   # 不是循环结束(上限)
    # 首字节前失败 → 未提交的 user 提问被回滚。
    assert conv.messages == []


async def test_tool_failure_is_structured_not_loop_error():
    """单个工具失败（execute 返回 fail）不触发循环错误，循环据结果继续。"""
    read = _FakeRead(fail=True)
    prov = ScriptedProvider([
        [tool_use_done("read_file", {"path": "a"})],
        [text_chunk("读失败了，我换个法子"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("读 a")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))

    assert _of_type(chunks, ChunkType.LOOP_ERROR) == []   # 工具失败不是循环错误
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_NATURAL
    tr = _blocks_of_type(conv, "tool_result")
    assert tr[0]["content"] == "FAIL:a" and tr[0].get("is_error") is True
    assert conv.messages[-1] == {"role": "assistant", "content": "读失败了，我换个法子"}


# ── T3 工具分类与并发顺序 ─────────────────────────────────────────────────────

async def test_reads_concurrent_results_in_call_order():
    """一轮含「读 a、读 b、写 c」：两读并发，回灌 tool_result 顺序 == 模型原调用序 (a,b,c)。"""
    read, write = _FakeRead(), _FakeWrite()

    async def approve(tc):
        return True

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("read_file", {"path": "b"}, "c2"),
            tool_call("write_file", {"path": "o"}, "c3"),
        ])],
        [text_chunk("done"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("混合一轮")
    await _drain(run_agent_turn(prov, conv, _registry(read, write), "sys", confirm=approve))

    ids = [b["tool_use_id"] for b in _blocks_of_type(conv, "tool_result")]
    assert ids == ["c1", "c2", "c3"]   # 回灌按调用序（确定性）


async def test_on_tool_completion_order_vs_result_call_order():
    """读类完成序 ≠ 调用序时：on_tool 走完成序、tool_result 走调用序（二者有意不同）。"""
    read = _FakeRead()
    on_tool_results: list[str] = []

    def on_tool(phase, tc, result):
        if phase == "result":
            on_tool_results.append(tc["input"]["path"])

    # 调用序 a(慢) → b(快)：b 先跑完。
    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a", "delay": 0.20}, "c1"),
            tool_call("read_file", {"path": "b", "delay": 0.00}, "c2"),
        ])],
        [text_chunk("done"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("两读")
    await _drain(run_agent_turn(prov, conv, _registry(read), "sys", on_tool=on_tool))

    # tool_result 回灌 == 调用序
    ids = [b["tool_use_id"] for b in _blocks_of_type(conv, "tool_result")]
    assert ids == ["c1", "c2"]
    # on_tool result emit == 完成序（b 先于 a），≠ 调用序
    assert on_tool_results == ["b", "a"]


async def test_reads_truly_concurrent_overlap():
    """读类确实并发：用 barrier 强制两读同时在执行；断言进入时间重叠（第二读早于第一读结束）。"""
    barrier = threading.Barrier(2)
    read = _FakeRead(barrier=barrier)

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("read_file", {"path": "b"}, "c2"),
        ])],
        [text_chunk("done"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("并发两读")
    await _drain(run_agent_turn(prov, conv, _registry(read), "sys"))

    # barrier 未 break → 两读同时到达执行点（串行会超时 BrokenBarrier）。
    tr = _blocks_of_type(conv, "tool_result")
    assert all(not b.get("is_error") for b in tr)
    # 进入时间重叠：最晚进入 < 最早退出（第二读开始早于第一读结束）。
    assert max(read.entered) < min(read.exited)


async def test_mixed_round_confirm_only_writes():
    """同轮混合：写类逐个触发 confirm，读类不触发。"""
    read, write = _FakeRead(), _FakeWrite()
    confirmed: list[str] = []

    async def confirm(tc):
        confirmed.append(tc["name"])
        return True

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("write_file", {"path": "o"}, "c2"),
        ])],
        [text_chunk("done"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("一读一写")
    await _drain(run_agent_turn(prov, conv, _registry(read, write), "sys", confirm=confirm))

    assert confirmed == ["write_file"]   # 只有写类触发确认
    assert read.calls and write.calls    # 两者都执行


# ── 安全确认（#0003 回归）─────────────────────────────────────────────────────

async def test_confirm_called_for_write_not_read():
    read, write = _FakeRead(), _FakeWrite()
    confirmed: list[str] = []

    async def confirm(tc):
        confirmed.append(tc["name"])
        return True

    prov = ScriptedProvider([
        [tool_use_done("read_file", {"path": "a.txt"})],
        [text_chunk("done"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("读")
    await _drain(run_agent_turn(prov, conv, _registry(read, write), "sys", confirm=confirm))
    assert confirmed == []      # read 免确认
    assert read.calls

    prov2 = ScriptedProvider([
        [tool_use_done("write_file", {"path": "o.txt", "content": "x"})],
        [text_chunk("ok"), done_chunk()],
    ])
    conv2 = Conversation()
    conv2.add_user("写")
    await _drain(run_agent_turn(prov2, conv2, _registry(read, write), "sys", confirm=confirm))
    assert confirmed == ["write_file"]
    assert write.calls


async def test_user_reject_feeds_back_message_and_continues():
    write = _FakeWrite()

    async def deny(tc):
        return False

    prov = ScriptedProvider([
        [tool_use_done("write_file", {"path": "o.txt", "content": "x"})],
        [text_chunk("好的，我不写了"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("写 o.txt")
    await _drain(run_agent_turn(prov, conv, _registry(write), "sys", confirm=deny))

    assert write.calls == []
    tr = _blocks_of_type(conv, "tool_result")
    assert tr and tr[0]["content"] == REJECTED_MESSAGE
    assert tr[0].get("is_error") is True
    assert conv.messages[-1] == {"role": "assistant", "content": "好的，我不写了"}


# ── T4 取消配平 ───────────────────────────────────────────────────────────────

async def test_cancel_balances_tool_use_and_result():
    """中途取消（confirm 期间置令牌）：tool_use 数 == tool_result 数；占位文案精确；可重放。"""
    import asyncio

    cancel = asyncio.Event()
    read = _FakeRead("read_file")
    write_b, write_c = _FakeWrite("write_file"), _FakeWrite("delete_file")
    confirmed: list[str] = []

    async def confirm(tc):
        confirmed.append(tc["name"])
        cancel.set()           # 确认期间被取消（模拟用户 Ctrl+C）
        return True

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("write_file", {"path": "o"}, "c2"),
            tool_call("delete_file", {"path": "p"}, "c3"),
        ])],
    ])
    conv = Conversation()
    conv.add_user("go")
    chunks = await _drain(run_agent_turn(
        prov, conv, _registry(read, write_b, write_c), "sys", confirm=confirm, cancel=cancel
    ))

    # 配平：tool_use 块数 == tool_result 块数。
    assert len(_blocks_of_type(conv, "tool_use")) == len(_blocks_of_type(conv, "tool_result")) == 3
    # 读保留真实结果；两写未执行、补「已取消」占位。
    assert read.calls and write_b.calls == [] and write_c.calls == []
    results = {b["tool_use_id"]: b for b in _blocks_of_type(conv, "tool_result")}
    assert results["c1"]["content"] == "CONTENT:a"            # 读：真实结果
    assert results["c2"]["content"] == CANCELLED_MESSAGE      # 已发起被中断（confirm 后取消）
    assert results["c3"]["content"] == CANCELLED_MESSAGE      # 尚未开始
    assert confirmed == ["write_file"]                        # write_b 确实「已发起」
    # 循环结束原因 = 取消。
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_CANCELLED
    # 已进工具阶段：纯文本 user 提问保留（已配平、不回滚）。
    assert conv.messages[0] == {"role": "user", "content": "go"}

    # 取消后历史可重放：存盘再 load 不报错、消息数一致。
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h.json"
        conv.save(p)
        reloaded = Conversation.load(p)
    assert len(reloaded.messages) == len(conv.messages)


async def test_cancel_keeps_inflight_read_result():
    """取消令牌在读「正在执行」时被置：等其跑完、保留真实结果；其后写类（尚未开始）补占位。"""
    import asyncio

    cancel = asyncio.Event()
    read = _FakeRead("read_file", cancel_on_execute=cancel)
    write = _FakeWrite("write_file")

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("write_file", {"path": "o"}, "c2"),
        ])],
    ])
    conv = Conversation()
    conv.add_user("go")
    chunks = await _drain(run_agent_turn(
        prov, conv, _registry(read, write), "sys", confirm=lambda tc: None, cancel=cancel
    ))

    results = {b["tool_use_id"]: b for b in _blocks_of_type(conv, "tool_result")}
    assert read.calls and results["c1"]["content"] == "CONTENT:a"   # 读：保留真实结果
    assert write.calls == [] and results["c2"]["content"] == CANCELLED_MESSAGE  # 写：尚未开始
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_CANCELLED


async def test_cancel_before_first_byte_drops_user():
    """取消发生在首字节前（末条仍纯文本 user）→ 不把该提问追加进历史（未提交）。"""
    import asyncio

    cancel = asyncio.Event()
    cancel.set()   # 预置：循环第一轮开始即检测到取消
    prov = ScriptedProvider([[text_chunk("hi"), done_chunk()]])
    conv = Conversation()
    conv.add_user("go")
    chunks = await _drain(run_agent_turn(prov, conv, _registry(_FakeRead()), "sys", cancel=cancel))

    assert prov.calls == 0          # 未调模型
    assert conv.messages == []      # 纯文本提问视作未提交、被回滚
    assert _of_type(chunks, ChunkType.LOOP_DONE)[-1].exit_reason == EXIT_CANCELLED


# ── T5 plan-only 拦截 ─────────────────────────────────────────────────────────

async def test_plan_only_intercepts_writes_runs_reads():
    """plan 模式：写类不执行、记入计划列表并回灌占位；读类照跑且 on_tool 正常 emit。"""
    read, write = _FakeRead(), _FakeWrite()
    read_events: list[str] = []

    def on_tool(phase, tc, result):
        if tc["name"] == "read_file" and phase == "result":
            read_events.append(tc["input"]["path"])

    prov = ScriptedProvider([
        [tools_done([
            tool_call("read_file", {"path": "a"}, "c1"),
            tool_call("write_file", {"path": "o", "content": "x"}, "c2"),
        ])],
        [text_chunk("已规划完毕"), done_chunk()],
    ])
    conv = Conversation()
    conv.add_user("改 o")
    chunks = await _drain(run_agent_turn(
        prov, conv, _registry(read, write), "sys", on_tool=on_tool, plan_only=True
    ))

    assert read.calls == [{"path": "a"}]   # 读照常执行
    assert write.calls == []               # 写未执行
    assert read_events == ["a"]            # 读类 on_tool 正常 emit（未误伤）
    # 被拦截写类回灌「plan-only 模式：已记录该操作，未执行」。
    results = {b["tool_use_id"]: b for b in _blocks_of_type(conv, "tool_result")}
    assert results["c2"]["content"] == PLAN_RECORDED_MESSAGE
    # 循环结束返回的计划列表含被拦截写类的名与关键参数。
    plan = _of_type(chunks, ChunkType.LOOP_DONE)[-1].plan
    assert plan and plan[0]["name"] == "write_file"
    assert "o" in plan[0]["text"]


# ── #0006 T5/T7：env / reminder 注入到 provider 但不落历史 ──────────────────────────

async def test_env_and_reminder_injected_not_persisted():
    """plan 模式一轮：provider 收到 env(system) + reminder(messages)，但 conversation 不存。"""
    prov = ScriptedProvider([[text_chunk("hi"), done_chunk(stop_reason="end_turn")]])
    conv = Conversation()
    conv.add_user("q")
    env = "<env>\ncwd: /x\nos: y\ndate: z\ngit: g\n</env>"

    await _drain(run_agent_turn(
        prov, conv, _registry(), "sys", env_block=env, plan_only=True
    ))

    # provider 第 1 次调用：system 含 env 块、messages 末条是带标签的提醒。
    assert any("<env>" in str(s) for s in prov.system_seen)
    first_msgs = prov.messages_seen[0]
    assert isinstance(first_msgs[-1]["content"], str)
    assert "<system-reminder>" in first_msgs[-1]["content"]

    # 历史只含 user/assistant，绝不含 env / reminder。
    dumped = json.dumps(conv.messages, ensure_ascii=False)
    assert "<env>" not in dumped
    assert "<system-reminder>" not in dumped
    assert len(conv.messages) == 2  # user "q" + assistant "hi"


async def test_no_env_block_keeps_plain_string_system():
    """不传 env_block 时 system 仍为纯字符串（向后兼容、与既往一致）。"""
    prov = ScriptedProvider([[text_chunk("hi"), done_chunk(stop_reason="end_turn")]])
    conv = Conversation()
    conv.add_user("q")
    await _drain(run_agent_turn(prov, conv, _registry(), "sys"))
    assert prov.system_seen[0] == "sys"
    # 非 plan、无 env：messages 不含提醒。
    assert all(
        not (isinstance(m.get("content"), str) and "<system-reminder>" in m["content"])
        for m in prov.messages_seen[0]
    )
