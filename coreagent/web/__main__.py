"""Web 服务启动入口（#0020）：`python -m coreagent.web`，uvicorn 跑 app。

与 #0019 CLI / 既有 TUI 互不影响（各自独立入口）。配置路径经 ``COREAGENT_CONFIG`` 覆盖，
监听地址 / 端口经 ``COREAGENT_WEB_HOST`` / ``COREAGENT_WEB_PORT`` 覆盖（均带默认值）。
"""

import os
import sys

import uvicorn

from coreagent.errors import ConfigError
from coreagent.web.app import create_app


def main() -> None:
    config_path = os.environ.get("COREAGENT_CONFIG", "config.yaml")
    try:
        app = create_app(config_path)
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)
    except ConfigError as e:
        print(f"配置错误：{e}")
        sys.exit(1)
    except ValueError as e:
        print(str(e))
        sys.exit(1)

    host = os.environ.get("COREAGENT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("COREAGENT_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
