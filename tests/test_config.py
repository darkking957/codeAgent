import textwrap

import pytest

from coreagent.config import load_config
from coreagent.errors import ConfigError


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_load_valid(tmp_path):
    path = _write(tmp_path, """
        protocol: anthropic
        model: claude-x
        api_key: sk-abc
    """)
    cfg = load_config(path)
    assert cfg.protocol == "anthropic"
    assert cfg.model == "claude-x"
    assert cfg.api_key == "sk-abc"


def test_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VAR", "abc")
    path = _write(tmp_path, """
        protocol: openai
        model: gpt-x
        api_key: ${TEST_VAR}
    """)
    cfg = load_config(path)
    assert cfg.api_key == "abc"


def test_invalid_protocol(tmp_path):
    path = _write(tmp_path, """
        protocol: bogus
        model: m
        api_key: k
    """)
    with pytest.raises(ConfigError) as ei:
        load_config(path)
    msg = str(ei.value)
    assert "protocol" in msg
    assert "anthropic" in msg and "openai" in msg
    assert "bogus" in msg  # 当前值


def test_empty_api_key(tmp_path):
    path = _write(tmp_path, """
        protocol: anthropic
        model: m
        api_key: ""
    """)
    with pytest.raises(ConfigError) as ei:
        load_config(path)
    msg = str(ei.value)
    assert "api_key" in msg
    assert "${" in msg  # 提示可用 ${ENV_VAR}


def test_empty_model(tmp_path):
    path = _write(tmp_path, """
        protocol: anthropic
        model: ""
        api_key: k
    """)
    with pytest.raises(ConfigError) as ei:
        load_config(path)
    assert "model" in str(ei.value)


def test_bad_thinking_budget(tmp_path):
    path = _write(tmp_path, """
        protocol: anthropic
        model: m
        api_key: k
        thinking:
          enabled: true
          budget_tokens: 0
    """)
    with pytest.raises(ConfigError) as ei:
        load_config(path)
    assert "budget_tokens" in str(ei.value)


def test_default_output_limits(tmp_path):
    path = _write(tmp_path, """
        protocol: anthropic
        model: m
        api_key: k
    """)
    cfg = load_config(path)
    assert cfg.max_tokens == 8192
    assert cfg.thinking_max_tokens == 16000


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("definitely-nonexistent.yaml")
