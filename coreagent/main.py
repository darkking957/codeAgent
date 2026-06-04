import argparse
import asyncio
import logging
import os
import sys

from coreagent.config import load_config
from coreagent.conversation import Conversation
from coreagent.errors import ConfigError
from coreagent.providers import create_provider
from coreagent.tui import TUI

# 日志级别由环境变量控制，默认安静（WARNING），且写 stderr 不污染对话区（stdout）。
_LOG_ENV = "COREAGENT_LOG_LEVEL"


def _configure_logging() -> None:
    level_name = os.environ.get(_LOG_ENV, "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="CoreAgent — Terminal AI Assistant")
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件路径")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.debug("配置文件缺失", exc_info=True)
        print(str(e))
        sys.exit(1)
    except ConfigError as e:
        logger.debug("配置校验失败", exc_info=True)
        print(f"配置错误：{e}")
        sys.exit(1)

    try:
        provider = create_provider(config)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    conversation = Conversation.load()
    tui = TUI(provider, config, conversation)
    asyncio.run(tui.run())
