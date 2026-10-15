"""Serve the owner console on its own, without starting the bot.

The console's experiment lab is file-backed, so it is fully usable while the bot
is down — which is the point: deciding whether to enable
``BOT_BEHAVIOR_GRAPH_PATH`` should not require running the thing first.

Sections that need the composed runtime (记忆中心, 任务中心) answer 503 and say so.
Everything else — the configuration editor, the guide, the review queue and the
experiment lab — works.

Reads the same ``BOT_WEBUI_*`` variables the extension does, so it listens on the
port and token the deployer already configured. Like the bot itself, it does not
read ``.env`` on its own — source it first, as ``docs/guides/QUICKSTART.md`` describes::

    source .env
    .venv/bin/python -m tools.operations.serve_console
    .venv/bin/python -m tools.operations.serve_console --port 6201

Binding is loopback-only, matching ``WebUIConfig.validate``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


from qqbot.interfaces.webui.config import WebUIConfig
from qqbot.interfaces.webui.server import WebUIServer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="默认取 BOT_WEBUI_HOST")
    parser.add_argument("--port", type=int, default=None, help="默认取 BOT_WEBUI_PORT")
    parser.add_argument("--token", default=None, help="默认取 BOT_WEBUI_TOKEN")
    args = parser.parse_args()

    config = WebUIConfig.from_env()
    if args.host or args.port or args.token:
        config = WebUIConfig(
            enabled=True,
            host=args.host or config.host,
            port=args.port or config.port,
            token=args.token or config.token,
            env_path=config.env_path,
            review_path=config.review_path,
            persona_path=config.persona_path,
        )
    if not config.token:
        print(
            "没有读到 BOT_WEBUI_TOKEN，控制台无法登录。\n"
            "这个项目和 Bot 一样不会自己加载 .env，先在同一个终端里执行：\n"
            "    source .env\n"
            "或者用 --token 传入一个。",
            file=sys.stderr,
        )
        return 2
    config.validate()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    server = WebUIServer(config)
    print(f"控制台：http://{config.host}:{config.port}")
    print("  · 实验看板 / 使用指南 / 配置编辑 / 审查队列 可用")
    print("  · 记忆中心与任务中心需要 Bot 在跑，会提示「服务尚未就绪」")
    print("  · 登录密钥：.env 里的 BOT_WEBUI_TOKEN")
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
