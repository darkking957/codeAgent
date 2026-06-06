import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("~/.config/coreagent/history.json").expanduser()


class Conversation:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(
        self,
        text: str,
        thinking: str = "",
        blocks: list | None = None,
    ) -> None:
        if blocks is not None:
            self.messages.append({"role": "assistant", "content": blocks})
        elif thinking:
            self.messages.append({
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": thinking},
                    {"type": "text", "text": text},
                ],
            })
        else:
            self.messages.append({"role": "assistant", "content": text})

    def add_assistant_blocks(self, blocks: list) -> None:
        """追加 assistant 内容块消息（含 tool_use 块，供多轮工具调用回灌）。"""
        self.messages.append({"role": "assistant", "content": blocks})

    def add_tool_results(self, results: list[dict]) -> None:
        """追加含 tool_result 块的 user 消息。

        results 每项：{"tool_use_id", "content", "is_error"(可选)}。
        """
        content: list[dict] = []
        for r in results:
            block: dict = {
                "type": "tool_result",
                "tool_use_id": r["tool_use_id"],
                "content": r["content"],
            }
            if r.get("is_error"):
                block["is_error"] = True
            content.append(block)
        self.messages.append({"role": "user", "content": content})

    def clear(self) -> None:
        self.messages.clear()

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 写同目录临时文件 → fsync → 原子替换，保证写入被中断也不损坏既有历史。
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.messages, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            # 任意失败都清理临时文件，避免目录残留。
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Conversation":
        path = Path(path)
        conv = cls()
        if not path.exists():
            return conv
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("历史文件结构非法：顶层应为消息列表")
            conv.messages = data
            return conv
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # 损坏 → 备份为 <原名>.bak、以空历史启动、记录告警。
            backup = path.with_name(path.name + ".bak")
            try:
                os.replace(path, backup)
                logger.warning("历史文件损坏，已备份至 %s 并以空历史启动：%s", backup, e)
            except OSError as be:
                logger.warning("历史文件损坏且备份失败（%s），以空历史启动：%s", be, e)
            return cls()
