import json
import logging

from coreagent.conversation import Conversation


def test_roundtrip(tmp_path):
    path = tmp_path / "history.json"
    conv = Conversation()
    conv.add_user("hello")
    conv.add_assistant("hi there", thinking="pondering")
    conv.save(path)

    loaded = Conversation.load(path)
    assert loaded.get_messages() == conv.get_messages()
    # 带 thinking 的 assistant 写成内容块列表。
    last = loaded.get_messages()[-1]
    assert last["role"] == "assistant"
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "thinking"
    assert last["content"][1]["type"] == "text"


def test_atomic_no_temp_residue(tmp_path):
    path = tmp_path / "history.json"
    conv = Conversation()
    conv.add_user("x")
    conv.save(path)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["history.json"]
    assert not any(n.endswith(".tmp") for n in names)


def test_corrupt_recovery(tmp_path, caplog):
    path = tmp_path / "history.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        conv = Conversation.load(path)

    assert conv.get_messages() == []  # 空历史启动
    backup = tmp_path / "history.json.bak"
    assert backup.exists()  # <原名>.bak 备份
    assert any("损坏" in r.getMessage() for r in caplog.records)  # 日志告警


def test_corrupt_structure_recovery(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    conv = Conversation.load(path)
    assert conv.get_messages() == []
    assert (tmp_path / "history.json.bak").exists()


def test_missing_file_returns_empty(tmp_path):
    conv = Conversation.load(tmp_path / "nope.json")
    assert conv.get_messages() == []


def test_clear_then_save_is_empty_list(tmp_path):
    path = tmp_path / "history.json"
    conv = Conversation()
    conv.add_user("a")
    conv.clear()
    conv.save(path)
    assert json.loads(path.read_text(encoding="utf-8")) == []
