import json
from pathlib import Path

DEFAULT_PATH = Path("~/.config/coreagent/history.json").expanduser()


class Conversation:
    def __init__(self):
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

    def clear(self) -> None:
        self.messages.clear()

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.messages, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Conversation":
        conv = cls()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                conv.messages = json.load(f)
        return conv
