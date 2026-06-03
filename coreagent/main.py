import argparse
import asyncio
import sys

from coreagent.config import load_config
from coreagent.conversation import Conversation
from coreagent.providers import create_provider
from coreagent.tui import TUI


def main() -> None:
    parser = argparse.ArgumentParser(description="CoreAgent — Terminal AI Assistant")
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)
    except Exception as e:
        print(f"配置加载失败：{e}")
        sys.exit(1)

    try:
        provider = create_provider(config)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    conversation = Conversation.load()
    tui = TUI(provider, config, conversation)
    asyncio.run(tui.run())
