from __future__ import annotations

from .config import WebUIConfig
from .server import WebUIServer


def register(host, _config, _model) -> None:
    config = WebUIConfig.from_env()
    config.validate()
    if not config.enabled:
        return
    server = WebUIServer(config)
    host.binders.append(server.bind_runtime)
    host.management_binders.append(server.bind_schedule_management)
    host.workers.append(server.run)
    host.closers.append(server.close)


def validate() -> dict:
    config = WebUIConfig.from_env()
    config.validate()
    return {
        "enabled": config.enabled,
        "url": f"http://{config.host}:{config.port}" if config.enabled else None,
        "token_configured": bool(config.token),
        "env_path": str(config.env_path),
    }
