from __future__ import annotations

from ...runtime.tools import Tool, ToolPermission
from .client import QQChannelClient
from .config import QQChannelConfig
from .service import QQChannelService


def register(host, _config, _model) -> None:
    config = QQChannelConfig.from_env()
    if not config.enabled:
        return
    client = QQChannelClient(config)
    service = QQChannelService(config, client)
    host.closers.append(client.aclose)
    host.tools.register(Tool(
        name="query_qq_channel",
        description="只读查询 QQ 官方频道、子频道、权限、成员、在线数和论坛帖子。",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"}, "query": {"type": "object"},
        }, "required": ["path"]},
        handler=service.query, permissions=ToolPermission.READ_GROUP,
        quota_per_minute=6,
    ))
    host.tools.register(Tool(
        name="write_qq_channel",
        description="受控写入 QQ 官方频道：发论坛帖子/评论、创建公告或日程、修改日程。仅本地 operator 可授权。",
        parameters={"type": "object", "properties": {
            "method": {"type": "string"}, "path": {"type": "string"},
            "body": {"type": "object"},
        }, "required": ["method", "path", "body"]},
        handler=service.write, permissions=ToolPermission.MODERATE_GROUP,
        quota_per_minute=2,
    ))


def validate() -> dict:
    config = QQChannelConfig.from_env()
    return {
        "enabled": config.enabled,
        "configured": bool(config.app_id and config.app_secret),
        "sandbox": config.sandbox,
        "operators": len(config.operator_ids),
    }
