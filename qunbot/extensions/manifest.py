"""Versioned, declarative extension manifests and the rules that validate them.

Every optional package the bot can load is described here, in one place, as
data. The table is a *local allowlist*: a directory dropped into
``qunbot/extensions/`` is not an extension and is never imported unless a
manifest in this module names it. There is no directory scan, no entry-point
discovery and no chat surface that can add a row — a deployment edits this file
(or the config that selects from it) and restarts.

Each manifest answers five questions before a single line of the extension
runs:

``name`` / ``version``   what it is, and which revision of it
``contract``             which host contract generation it was written against
``requires``             which other extensions must be enabled alongside it
``contributes``          which kinds of host surface it is allowed to touch
``entry``                how to reach its ``register``/``build_worker`` callable

Validation happens in :func:`qunbot.extensions.loader.resolve_plan`, which runs
*before* any module is imported. A missing dependency, a duplicate job action,
a contract the host is too old for, or an extension that reached for a surface
it did not declare all raise :class:`ExtensionError` at startup — loudly, naming
the extension and what it lacked, rather than at 7am in a group.

Tool declarations live here too. A tool an extension wants to expose declares
its name, the permission it needs and how often it may be called; see
``qunbot.runtime.tools`` for how that is enforced.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field

#: The host contract generation this build implements. An extension declares
#: the *minimum* it needs; a manifest asking for something newer than this
#: refuses to load rather than failing at the first call.
CONTRACT_VERSION = 1

#: Default bounds. Small on purpose: the point of a manifest is that the shape
#: of an extension's surface is reviewable before it runs. A manifest that
#: declares no context contribution gets no context budget, so "how much of the
#: prompt can this package occupy?" has an answer even for a package that was
#: never meant to touch the prompt.
DEFAULT_WORKERS = 1
DEFAULT_CONTEXT_CHARS = 300
DEFAULT_TOOL_QUOTA_PER_MINUTE = 20

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ExtensionError(ValueError):
    """A manifest or a registration violated the extension contract.

    Subclasses ``ValueError`` so startup checks that already catch ``ValueError``
    keep working. The message always names the extension and the problem.
    """


class Contribution(str, enum.Enum):
    """The kinds of surface an extension may touch during registration.

    An extension declares the ones it uses. During ``register`` the loader
    watches each surface and refuses anything undeclared, so "what does this
    package do to the bot?" is answerable from the table below without reading
    its code.
    """

    JOB_ACTION = "job_action"          # a scheduled action name, e.g. "poster"
    BACKGROUND = "background"          # a long-running loop on the host
    CONTEXT = "context_provider"       # a dynamic-suffix contributor
    OBSERVER = "observer"              # a post-reply observer
    TOOL = "tool"                      # a model-visible tool
    CLOSER = "closer"                  # a shutdown callback
    REPLY_POLICY = "reply_policy"      # replaces the built-in @-only rule
    BINDER = "binder"                  # deferred wiring callback
    MEDIA = "media"                    # meme catalogue / speech source
    PROACTIVE_GATE = "proactive_gate"  # veto over unprompted messages
    INBOUND = "inbound_handler"        # raw OneBot notice/request observer


class ExtensionKind(str, enum.Enum):
    """Which loader owns this extension."""

    FEATURE = "feature"        # registered against the FeatureHost
    JOB = "job"                # registers job actions
    BACKGROUND = "background"  # contributes a worker loop


@dataclass(frozen=True)
class ToolDeclaration:
    """A model-visible tool an extension intends to expose, and its limits.

    Declared up front so "which tools can the model call, and what may they
    do?" is a question about this table rather than about the extension's code.
    The runtime refuses to register a tool the manifest did not declare, and
    refuses any tool whose permission set is privileged.
    """

    name: str
    #: ToolPermission. Typed as ``object`` to avoid importing the runtime from
    #: the manifest layer (the dependency runs loader -> runtime, not back).
    permissions: object = None
    quota_per_minute: int = DEFAULT_TOOL_QUOTA_PER_MINUTE
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ExtensionError("tool declaration needs a non-empty name")
        if int(self.quota_per_minute) < 1:
            raise ExtensionError(
                f"tool {self.name!r} declares quota_per_minute < 1; a tool that "
                "may never be called should not be registered at all"
            )


@dataclass(frozen=True)
class ExtensionManifest:
    """One optional extension, described as data.

    Frozen so the table below is effectively a constant: nothing at runtime can
    mutate an extension's declared permissions or bounds.
    """

    name: str
    version: str
    kind: ExtensionKind
    entry: str
    contributes: frozenset[Contribution] = field(default_factory=frozenset)
    contract: int = CONTRACT_VERSION
    #: Hard edges: these must be enabled too, or startup fails.
    requires: tuple[str, ...] = ()
    #: Soft edges: extensions this one reads from when they happen to be on.
    #: A missing one is logged, never fatal — that is the difference from
    #: ``requires``.
    prefers: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    skills: frozenset[str] = field(default_factory=frozenset)
    tools: tuple[ToolDeclaration, ...] = ()
    max_workers: int = DEFAULT_WORKERS
    max_context_providers: int = 0
    max_context_chars: int = DEFAULT_CONTEXT_CHARS
    description: str = ""

    @property
    def module(self) -> str:
        """Import path, without the ``:attribute`` suffix."""
        return self.entry.split(":", 1)[0]

    @property
    def attribute(self) -> str | None:
        """Explicit attribute name, or ``None`` for the conventional ones."""
        parts = self.entry.split(":", 1)
        return parts[1] if len(parts) == 2 else None

    def check(self) -> None:
        """Static self-check. Raises :class:`ExtensionError` on a bad row."""
        if not self.name or not self.name.strip():
            raise ExtensionError(f"manifest with empty name: {self!r}")
        if not _VERSION_RE.match(self.version):
            raise ExtensionError(
                f"extension {self.name!r} declares version {self.version!r}; "
                "expected MAJOR.MINOR.PATCH"
            )
        if not isinstance(self.kind, ExtensionKind):
            raise ExtensionError(
                f"extension {self.name!r} declares unknown kind {self.kind!r}"
            )
        if self.contract < 1:
            raise ExtensionError(
                f"extension {self.name!r} declares contract {self.contract}; "
                "the oldest supported is 1"
            )
        if not self.entry.strip():
            raise ExtensionError(f"extension {self.name!r} has no entry point")
        for item in self.contributes:
            if not isinstance(item, Contribution):
                raise ExtensionError(
                    f"extension {self.name!r} declares unknown contribution {item!r}"
                )
        if self.name in self.requires:
            raise ExtensionError(f"extension {self.name!r} requires itself")
        if self.actions and Contribution.JOB_ACTION not in self.contributes:
            raise ExtensionError(
                f"extension {self.name!r} names job actions but does not declare "
                "the job_action contribution"
            )
        if Contribution.JOB_ACTION in self.contributes and not self.actions:
            raise ExtensionError(
                f"extension {self.name!r} declares the job_action contribution "
                "but names no actions"
            )
        if self.max_workers < 1:
            raise ExtensionError(f"extension {self.name!r} declares max_workers < 1")
        if self.max_context_chars < 1:
            raise ExtensionError(
                f"extension {self.name!r} declares max_context_chars < 1"
            )
        if Contribution.CONTEXT in self.contributes and self.max_context_providers < 1:
            raise ExtensionError(
                f"extension {self.name!r} declares the context_provider "
                "contribution but allows no providers"
            )
        seen: set[str] = set()
        for tool in self.tools:
            if tool.name in seen:
                raise ExtensionError(
                    f"extension {self.name!r} declares tool {tool.name!r} twice"
                )
            seen.add(tool.name)

    def declared_tool(self, name: str) -> ToolDeclaration | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def summary(self) -> dict:
        """A JSON-safe description, for ``qunbot --check`` and log lines."""
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind.value,
            "contract": self.contract,
            "requires": list(self.requires),
            "prefers": list(self.prefers),
            "contributes": sorted(item.value for item in self.contributes),
            "actions": list(self.actions),
            "skills": sorted(self.skills),
            "tools": [tool.name for tool in self.tools],
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
#
# Local allowlist. A package under qunbot/extensions/ without a row here is not
# loadable: nothing scans the directory, so dropping a module in is not a way
# to add behaviour. Contributions are declared so the loader can refuse
# anything undeclared *during* registration.
#
# Convention: bump ``version`` when a package's observable behaviour changes;
# raise ``contract`` only if it needs a newer host than CONTRACT_VERSION.

MANIFESTS: dict[str, ExtensionManifest] = {}


def _register(manifest: ExtensionManifest) -> ExtensionManifest:
    MANIFESTS[manifest.name] = manifest
    return manifest


_register(
    ExtensionManifest(
        name="scheduled_chat",
        version="1.1.0",
        kind=ExtensionKind.JOB,
        entry="qunbot.extensions.scheduled_chat.job:register_jobs",
        contributes=frozenset({Contribution.JOB_ACTION}),
        # "continuation" is declared here rather than in proactive_chat because
        # only a JOB-kind extension may register an action, and the default
        # configuration enables scheduled_chat but not proactive_chat —
        # hosting the engine in the tick extension would make the default
        # configuration import a disabled package.
        actions=("chat", "deliver", "continuation"),
        skills=frozenset({"proactive-chat"}),
        description="定时闲聊：注册 chat（定时）与 continuation（间隔续聊）两个动作",
    )
)

_register(
    ExtensionManifest(
        name="exam_poster",
        version="1.1.0",
        kind=ExtensionKind.JOB,
        entry="qunbot.extensions.exam_poster.job:register_jobs",
        contributes=frozenset({Contribution.JOB_ACTION}),
        actions=("poster",),
        description="考研倒计时海报：注册 poster 任务动作（需要 BOT_EXAM_DATE）",
    )
)

_register(
    ExtensionManifest(
        name="proactive_chat",
        version="1.0.0",
        kind=ExtensionKind.BACKGROUND,
        entry="qunbot.extensions.proactive_chat.runner:build_worker",
        contributes=frozenset({Contribution.BACKGROUND}),
        prefers=("mood",),
        skills=frozenset({"proactive-chat"}),
        max_workers=1,
        max_context_providers=0,
        description="随机主动续聊：60 秒轮询的独立 worker",
    )
)

_register(
    ExtensionManifest(
        name="mood",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.mood.register",
        contributes=frozenset(
            {
                Contribution.CONTEXT,
                Contribution.OBSERVER,
                Contribution.PROACTIVE_GATE,
                Contribution.CLOSER,
            }
        ),
        max_context_providers=1,
        max_context_chars=200,
        description="Bot 自身心情：一条动态上下文、一个回复后观察者、一个主动发言闸门",
    )
)

_register(
    ExtensionManifest(
        name="memes",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.memes.register",
        contributes=frozenset({Contribution.CONTEXT, Contribution.MEDIA}),
        max_context_providers=1,
        max_context_chars=300,
        skills=frozenset({"meme"}),
        description="本地表情包目录：媒体来源 + 可用标签上下文",
    )
)

_register(
    ExtensionManifest(
        name="voice",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.voice.register",
        contributes=frozenset({Contribution.MEDIA, Contribution.CLOSER}),
        max_context_providers=0,
        skills=frozenset({"voice"}),
        description="可选 TTS 语音源：媒体来源 + 关闭回调",
    )
)

_register(
    ExtensionManifest(
        name="persona",
        version="1.1.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.persona",
        contributes=frozenset(
            {
                Contribution.CONTEXT,
                Contribution.OBSERVER,
                Contribution.CLOSER,
                # The suggestion queue's worker. It adds rows to a table a
                # deployer reads and nothing else — no provider, no observer,
                # no write to the persona file.
                Contribution.BACKGROUND,
            }
        ),
        prefers=("mood",),
        max_context_providers=1,
        max_context_chars=200,
        description="每轮临时性格指引：有界上下文 + 回复后观察者 + 人设建议队列",
    )
)

_register(
    ExtensionManifest(
        name="reply_policy",
        version="1.1.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.reply_policy",
        contributes=frozenset({Contribution.REPLY_POLICY, Contribution.BINDER}),
        max_context_providers=0,
        description="可替换的回复决策策略：只换一个决策，不加任何上下文",
    )
)

_register(
    ExtensionManifest(
        name="slang",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.slang",
        contributes=frozenset(
            {
                Contribution.CONTEXT,
                Contribution.BACKGROUND,
                Contribution.BINDER,
                Contribution.CLOSER,
            }
        ),
        max_context_providers=1,
        max_context_chars=300,
        max_workers=1,
        description="群内黑话：低信任上下文 + 挖掘 worker + 延迟接线",
    )
)

_register(
    ExtensionManifest(
        name="style_echo",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.style_echo",
        contributes=frozenset(
            {
                Contribution.CONTEXT,
                Contribution.OBSERVER,
                Contribution.BACKGROUND,
                Contribution.CLOSER,
            }
        ),
        max_context_providers=1,
        max_context_chars=250,
        max_workers=1,
        description="表达风格：有界上下文 + 采集观察者 + 采集 worker",
    )
)

_register(
    ExtensionManifest(
        name="world_context",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.world_context",
        contributes=frozenset(
            {Contribution.CONTEXT, Contribution.BACKGROUND, Contribution.CLOSER}
        ),
        max_context_providers=3,
        max_context_chars=400,
        max_workers=1,
        description="时间/天气/记忆回放：三个可分别禁用的上下文提供者",
    )
)

# Imported at the composition root only: manifests need the same concrete Flag
# values as the runtime registry in order to compare declared and registered
# permissions.  Feature packages themselves still depend only on the host API.
from ..runtime.tools import ToolPermission

_register(
    ExtensionManifest(
        name="qq_admin",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.qq_admin",
        contributes=frozenset(
            {Contribution.TOOL, Contribution.BINDER, Contribution.INBOUND}
        ),
        skills=frozenset({"group-files", "qq-admin"}),
        tools=(
            ToolDeclaration(
                "list_group_files",
                permissions=ToolPermission.READ_GROUP,
                quota_per_minute=3,
                description="列出当前群根目录文件",
            ),
            ToolDeclaration(
                "mute_group_member",
                permissions=ToolPermission.MODERATE_GROUP,
                quota_per_minute=2,
                description="由本地管理员授权禁言/解禁成员",
            ),
            ToolDeclaration(
                "set_group_essence",
                permissions=ToolPermission.MODERATE_GROUP,
                quota_per_minute=2,
                description="由本地管理员授权设置/取消群精华",
            ),
            ToolDeclaration(
                "propose_group_title",
                permissions=ToolPermission.MODERATE_GROUP,
                quota_per_minute=1,
                description="由 Bot 提出头衔，经本人同意或群友投票后授予",
            ),
        ),
        max_context_providers=0,
        description="QQ 群治理：群文件查询、受控禁言/精华、入群审核和欢迎",
    )
)

_register(
    ExtensionManifest(
        name="qq_channel",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.qq_channel",
        contributes=frozenset({Contribution.TOOL, Contribution.CLOSER}),
        skills=frozenset({"qq-channel"}),
        tools=(
            ToolDeclaration(
                "query_qq_channel", permissions=ToolPermission.READ_GROUP,
                quota_per_minute=6, description="查询 QQ 官方频道",
            ),
            ToolDeclaration(
                "write_qq_channel", permissions=ToolPermission.MODERATE_GROUP,
                quota_per_minute=2, description="受控写入 QQ 官方频道",
            ),
        ),
        max_context_providers=0,
        description="QQ 开放平台频道：Token 缓存、只读查询和白名单写操作",
    )
)

_register(
    ExtensionManifest(
        name="webui",
        version="1.0.0",
        kind=ExtensionKind.FEATURE,
        entry="qunbot.extensions.webui",
        contributes=frozenset({
            Contribution.BACKGROUND, Contribution.CLOSER, Contribution.BINDER,
        }),
        max_workers=1,
        max_context_providers=0,
        description="本机所有者配置台：显式配置 schema、密钥遮罩与原子写入",
    )
)


def manifest_for(name: str) -> ExtensionManifest:
    """The manifest for ``name``, or raise :class:`ExtensionError`."""
    found = MANIFESTS.get(name)
    if found is None:
        raise ExtensionError(
            f"unknown extension {name!r}; known extensions: {sorted(MANIFESTS)}"
        )
    return found


def validate_manifests() -> None:
    """Check every row in the table. Raises on the first bad one.

    Run before any resolution: a broken row is a programming error in this
    file, and it should surface at startup rather than as a confusing failure
    inside an extension.
    """
    for name, manifest in MANIFESTS.items():
        if name != manifest.name:
            raise ExtensionError(
                f"manifest table key {name!r} does not match manifest name "
                f"{manifest.name!r}"
            )
        manifest.check()
