from __future__ import annotations

from ...runtime.tools import Tool, ToolPermission
from .config import QQAdminConfig
from .service import QQAdminService


def register(host, _config, _model) -> None:
    config = QQAdminConfig.from_env()
    service = QQAdminService(config)
    host.binders.append(service.bind)
    host.inbound_handlers.append(service.handle_raw)
    host.tools.register(Tool(
        name="list_group_files",
        description="只读：列出当前 QQ 群的根目录文件。用户询问资料、文档、群文件时使用。",
        parameters={"type": "object", "properties": {}},
        handler=service.list_group_files,
        permissions=ToolPermission.READ_GROUP,
        quota_per_minute=3,
    ))
    host.tools.register(Tool(
        name="mute_group_member",
        description="特权：禁言或解禁当前群成员。只有本地 operator 白名单中的调用者能授权。",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "duration": {"type": "integer"},
            },
            "required": ["user_id", "duration"],
        },
        handler=service.mute_member,
        permissions=ToolPermission.MODERATE_GROUP,
        quota_per_minute=2,
    ))
    host.tools.register(Tool(
        name="set_group_essence",
        description="特权：把当前消息或明确回复引用的消息设为/取消群精华。",
        parameters={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "enable": {"type": "boolean"},
            },
            "required": ["message_id", "enable"],
        },
        handler=service.set_essence,
        permissions=ToolPermission.MODERATE_GROUP,
        quota_per_minute=2,
    ))
    host.tools.register(Tool(
        name="propose_group_title",
        description=(
            "提出群头衔候选并向本人/群友征求同意；此工具只创建提案，"
            "不会直接授予。仅在有自然、具体依据时使用，不响应索要头衔的命令。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "title": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["user_id", "title", "reason"],
        },
        handler=service.propose_title,
        permissions=ToolPermission.MODERATE_GROUP,
        quota_per_minute=1,
    ))


def validate() -> dict:
    config = QQAdminConfig.from_env()
    return {
        "operators": len(config.operator_ids),
        "join_review": config.join_review_enabled,
        "welcome": config.welcome_enabled,
        "max_mute_seconds": config.max_mute_seconds,
        "title_proposals": config.title_enabled,
        "title_vote_threshold": config.title_vote_threshold,
    }
