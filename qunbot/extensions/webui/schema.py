from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    label: str
    kind: str = "text"
    help: str = ""
    secret: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = list(self.options)
        return data


SECTIONS = (
    ("connection", "连接与模型", "模型 API、NapCat 与允许接入的群"),
    ("conversation", "对话决策", "何时回复、上下文与成本控制"),
    ("personality", "记忆与人格", "长期记忆、关系、情绪和表达风格"),
    ("proactive", "主动发言", "接话、免打扰和定时任务的公共策略"),
    ("moderation", "群管理", "管理员授权、入群审核、欢迎和头衔"),
    ("media", "媒体与频道", "表情包、语音及 QQ 官方频道"),
    ("system", "系统", "扩展装配、存储和并发边界"),
)

def S(key: str, section: str, label: str, kind: str = "text", **kw) -> Setting:
    return Setting(key, section, label, kind, **kw)


SETTINGS = (
    S("BOT_MODEL_BASE_URL", "connection", "模型 API 地址", help="OpenAI 兼容的 /v1 地址"),
    S("BOT_MODEL_API_KEY", "connection", "模型 API Key", "password", secret=True),
    S("BOT_MODEL_NAME", "connection", "模型名称"),
    S("BOT_MODEL_REASONING_EFFORT", "connection", "推理强度", "select", options=("", "minimal", "low", "medium", "high")),
    S("BOT_ONEBOT_HOST", "connection", "OneBot 监听地址"),
    S("BOT_ONEBOT_PORT", "connection", "OneBot 端口", "number", minimum=1, maximum=65535),
    S("BOT_ONEBOT_TOKEN", "connection", "OneBot Token", "password", secret=True),
    S("BOT_GROUP_ALLOWLIST", "connection", "群白名单", help="多个群号用英文逗号分隔"),
    S("BOT_PRIVATE_ENABLED", "connection", "允许私聊", "boolean"),

    S("BOT_REPLY_POLICY_ENABLED", "conversation", "启用读空气策略", "boolean"),
    S("BOT_REPLY_POLICY_MODE", "conversation", "回复模式", "select", options=("mention_only", "room")),
    S("BOT_REPLY_POLICY_THRESHOLD", "conversation", "回复阈值", "number", minimum=0, maximum=1),
    S("BOT_REPLY_POLICY_MENTION_PROBABILITY", "conversation", "被 @ 回复概率", "number", minimum=0, maximum=1),
    S("BOT_REPLY_POLICY_AMBIENT_PROBABILITY", "conversation", "主动接话概率", "number", minimum=0, maximum=1),
    S("BOT_REPLY_POLICY_QUESTION_PROBABILITY", "conversation", "正常提问回复概率下限", "number", minimum=0, maximum=1),
    S("BOT_REPLY_POLICY_AFTER_REPLY_PROBABILITY", "conversation", "连续对话回复概率", "number", minimum=0, maximum=1, help="Bot 刚回复后，同一话题自然续聊的概率"),
    S("BOT_REPLY_POLICY_AFTER_REPLY_SECONDS", "conversation", "连续对话保持时间（秒）", "number", minimum=0, maximum=3600),
    S("BOT_REPLY_POLICY_DAILY_GROUP_LIMIT", "conversation", "每群每日回复上限", "number", minimum=0),
    S("BOT_REPLY_POLICY_DAILY_USER_LIMIT", "conversation", "每人每日回复上限", "number", minimum=0),
    S("BOT_REPLY_POLICY_PURSUIT_WINDOW_SECONDS", "conversation", "追问判定时间窗（秒）", "number", minimum=30, maximum=86400),
    S("BOT_MEMORY_EXTRACT_EVERY", "conversation", "每多少条消息提炼记忆", "number", minimum=2),

    S("BOT_AFFECTION_AUTO_ENABLED", "personality", "自动更新好感度", "boolean"),
    S("BOT_MOOD_ENABLED", "personality", "启用情绪系统", "boolean"),
    S("BOT_MOOD_AUTO_ENABLED", "personality", "自动评估情绪", "boolean"),
    S("BOT_MOOD_DECAY_MINUTES", "personality", "情绪半衰期（分钟）", "number", minimum=1),
    S("BOT_PERSONA_ENABLED", "personality", "动态人格", "boolean"),
    S("BOT_PERSONA_PROPOSALS_ENABLED", "personality", "定期扫描对话风格", "boolean"),
    S("BOT_PERSONA_AUTO_EXAMPLES_ENABLED", "personality", "自动演进风格示例", "boolean"),
    S("BOT_STYLE_ECHO_ENABLED", "personality", "语言风格学习", "boolean"),
    S("BOT_STYLE_ECHO_ALLOWED_USERS", "personality", "指定用户风格采样名单", help="留空时学习群聊整体节奏；填写 QQ 号后仅采样名单内用户"),
    S("BOT_SLANG_ENABLED", "personality", "群黑话学习", "boolean"),
    S("BOT_SLANG_ALLOW_IMITATION", "personality", "允许模仿已审核黑话", "boolean"),

    S("BOT_PROACTIVE_ENABLED", "proactive", "启用主动接话", "boolean"),
    S("BOT_PROACTIVE_PROBABILITY", "proactive", "主动接话概率", "number", minimum=0, maximum=1),
    S("BOT_PROACTIVE_DAILY_LIMIT", "proactive", "每日主动发言上限", "number", minimum=0),
    S("BOT_PROACTIVE_QUIET_MINUTES", "proactive", "群沉默多久后接话", "number", minimum=1),
    S("BOT_PROACTIVE_FRESHNESS_MINUTES", "proactive", "话题最长保鲜时间", "number", minimum=1),
    S("BOT_PROACTIVE_QUIET_START_HOUR", "proactive", "免打扰开始小时", "number", minimum=0, maximum=23),
    S("BOT_PROACTIVE_QUIET_END_HOUR", "proactive", "免打扰结束小时", "number", minimum=0, maximum=23),
    S("BOT_JOB_DAILY_LIMIT", "proactive", "定时闲聊每日上限", "number", minimum=0),
    S("BOT_ACTIVE_START_HOUR", "proactive", "任务活跃开始", "number", minimum=0, maximum=23),
    S("BOT_ACTIVE_END_HOUR", "proactive", "任务活跃结束", "number", minimum=1, maximum=24),

    S("BOT_QQ_ADMIN_OPERATOR_IDS", "moderation", "管理操作授权人", help="只有这些 QQ 号可授权写操作"),
    S("BOT_QQ_ADMIN_MAX_MUTE_SECONDS", "moderation", "最长禁言秒数", "number", minimum=0),
    S("BOT_QQ_ADMIN_JOIN_REVIEW_ENABLED", "moderation", "自动入群审核", "boolean"),
    S("BOT_QQ_ADMIN_JOIN_DEFAULT_APPROVE", "moderation", "无规则时默认通过", "boolean"),
    S("BOT_QQ_ADMIN_WELCOME_ENABLED", "moderation", "新人欢迎", "boolean"),
    S("BOT_QQ_ADMIN_TITLE_ENABLED", "moderation", "群头衔提案", "boolean"),
    S("BOT_QQ_ADMIN_TITLE_VOTE_THRESHOLD", "moderation", "头衔投票门槛", "number", minimum=1),

    S("BOT_MEMES_PATH", "media", "表情包目录"),
    S("BOT_MEDIA_QQ_FACE_PROBABILITY", "media", "QQ 原生表情概率", "number", minimum=0, maximum=1, help="命中语义关键词后附带 QQ 内置表情；不调用模型"),
    S("BOT_MEDIA_CASUAL_VOICE_PROBABILITY", "media", "闲聊短句语音概率", "number", minimum=0, maximum=1),
    S("BOT_MEDIA_FOLLOWUP_MEME_PROBABILITY", "media", "发言后表情包概率", "number", minimum=0, maximum=1),
    S("BOT_MEDIA_RANDOM_MEME_PROBABILITY", "media", "随机斗图概率", "number", minimum=0, maximum=1),
    S("BOT_REACTION_ENABLED", "media", "消息贴表情", "boolean"),
    S("BOT_REACTION_PROBABILITY", "media", "消息贴表情概率", "number", minimum=0, maximum=1, help="命中本地语义规则后的抽样概率；不调用模型"),
    S("BOT_REACTION_COOLDOWN_SECONDS", "media", "贴表情群冷却（秒）", "number", minimum=0, maximum=300),
    S("BOT_REACTION_DAILY_GROUP_LIMIT", "media", "每群每日贴表情上限", "number", minimum=0, maximum=1000),
    S("BOT_REACTION_DAILY_USER_LIMIT", "media", "每人每日贴表情上限", "number", minimum=0, maximum=200),
    S("BOT_QUOTE_ENABLED", "media", "引用回复", "boolean"),
    S("BOT_QUOTE_PROBABILITY", "media", "引用回复概率", "number", minimum=0, maximum=1),
    S("BOT_HUMANIZED_PACING_ENABLED", "media", "拟人发送延迟", "boolean"),
    S("BOT_HUMANIZED_PACING_BASE_SECONDS", "media", "基础发送延迟（秒）", "number", minimum=0, maximum=5),
    S("BOT_HUMANIZED_PACING_PER_CHAR_SECONDS", "media", "每字增加延迟（秒）", "number", minimum=0, maximum=.2),
    S("BOT_HUMANIZED_PACING_MAX_SECONDS", "media", "最大发送延迟（秒）", "number", minimum=0, maximum=10),
    S("BOT_HUMANIZED_PACING_FOLLOWUP_SECONDS", "media", "多段消息间隔（秒）", "number", minimum=0, maximum=5),
    S("BOT_REPEAT_PROBABILITY", "media", "参与复读概率", "number", minimum=0, maximum=1),
    S("BOT_REPEAT_COOLDOWN_SECONDS", "media", "复读群冷却（秒）", "number", minimum=0, maximum=3600),
    S("BOT_REPEAT_DAILY_GROUP_LIMIT", "media", "每群每日复读上限", "number", minimum=0, maximum=200),
    S("BOT_POKE_ENABLED", "media", "响应戳一戳", "boolean"),
    S("BOT_POKE_COUNTER_PROBABILITY", "media", "反戳概率", "number", minimum=0, maximum=1),
    S("BOT_POKE_COOLDOWN_SECONDS", "media", "同一群友戳一戳冷却（秒）", "number", minimum=0, maximum=3600),
    S("BOT_POKE_DAILY_USER_LIMIT", "media", "每人每日戳一戳响应上限", "number", minimum=0, maximum=100),
    S("BOT_POKE_FOLLOW_WINDOW_SECONDS", "media", "跟风戳统计窗口（秒）", "number", minimum=5, maximum=600),
    S("BOT_POKE_FOLLOW_DISTINCT_USERS", "media", "触发跟风戳所需人数", "number", minimum=2, maximum=20),
    S("BOT_POKE_FOLLOW_PROBABILITY", "media", "跟风戳概率", "number", minimum=0, maximum=1),
    S("BOT_POKE_FOLLOW_COOLDOWN_SECONDS", "media", "同一目标跟风戳冷却（秒）", "number", minimum=0, maximum=3600),
    S("BOT_REPLY_SEGMENT_MAX_CHARS", "conversation", "长回复单段最大字符数", "number", minimum=60, maximum=1000),
    S("BOT_TTS_PROVIDER", "media", "语音服务", "select", options=("dashscope", "cosyvoice", "qwen_audio", "openai")),
    S("BOT_TTS_API_KEY", "media", "语音 API Key", "password", secret=True),
    S("BOT_TTS_MODEL", "media", "语音模型"),
    S("BOT_TTS_VOICE", "media", "语音音色"),
    S("BOT_QQ_CHANNEL_ENABLED", "media", "启用 QQ 官方频道", "boolean"),
    S("BOT_QQ_CHANNEL_APP_ID", "media", "频道 App ID"),
    S("BOT_QQ_CHANNEL_APP_SECRET", "media", "频道 App Secret", "password", secret=True),
    S("BOT_QQ_CHANNEL_OPERATOR_IDS", "media", "频道写操作授权人"),

    S("BOT_EXTENSIONS", "system", "启用扩展", help="本地允许名单中的名称，逗号分隔；保存后重启"),
    S("BOT_DB_PATH", "system", "主数据库路径"),
    S("BOT_TIMEZONE", "system", "时区"),
    S("BOT_OBSERVER_QUEUE_SIZE", "system", "观察队列容量", "number", minimum=1),
    S("BOT_ONEBOT_MAX_LANES", "system", "群消息并发通道", "number", minimum=1),
)

BY_KEY = {item.key: item for item in SETTINGS}


def validate_value(setting: Setting, value: Any) -> str:
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{setting.key} has an unsupported value")
    if setting.kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        lowered = str(value).lower()
        if lowered not in {"true", "false"}:
            raise ValueError(f"{setting.label} must be true or false")
        return lowered
    text = str(value).strip()
    if setting.kind == "number":
        try:
            number = float(text)
        except ValueError as error:
            raise ValueError(f"{setting.label} must be a number") from error
        if setting.minimum is not None and number < setting.minimum:
            raise ValueError(f"{setting.label} must be at least {setting.minimum:g}")
        if setting.maximum is not None and number > setting.maximum:
            raise ValueError(f"{setting.label} must not exceed {setting.maximum:g}")
    if setting.options and text not in setting.options:
        raise ValueError(f"{setting.label} is not an allowed option")
    if setting.key == "BOT_MODEL_BASE_URL" and text:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("模型 API 地址必须是有效的 http(s) URL")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{setting.label} cannot contain line breaks")
    return text
