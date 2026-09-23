"""Owner-controlled extension composition; versioned manifests, no discovery.

One path in, one path out.

*In*: :func:`resolve_plan` reads the local allowlist
(``qunbot/extensions/manifest.py``) and refuses to continue if the enabled set
is inconsistent — an unknown name, a missing dependency, a contract this host
is too old for, or two extensions claiming the same job action. This runs
**before a single extension module is imported**, so a deployment with a broken
configuration fails at startup with a message naming the extension and what it
lacked, rather than at the first message of the day.

*During*: each extension registers against a
:class:`~qunbot.extensions.features.FeatureHost` while a
:class:`_ContributionGuard` watches the host's surfaces. Anything the extension
touches that its manifest did not declare — a context provider, an observer, a
worker, a tool — is a startup error. A package's blast radius is therefore
reviewable from the manifest table alone, and "it only registers a context
provider" is checked rather than asserted.

*Out*: :func:`close_features` runs every closer in reverse order with failures
isolated.

Nothing here scans a directory. A module dropped into ``qunbot/extensions/``
without a manifest row is not an extension and is never imported; that is the
property the boundary tests in ``tests/test_extension_boundaries.py`` pin down.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from importlib import import_module
from inspect import isawaitable
from typing import TYPE_CHECKING

from ..runtime.context import DEFAULT_MAX_CHARS
from ..runtime.tools import ToolPermission, describe_permissions
from ..scheduling.registry import JobHandlerRegistry
from .features import FeatureHost
from .manifest import (
    CONTRACT_VERSION,
    Contribution,
    ExtensionError,
    ExtensionKind,
    ExtensionManifest,
    MANIFESTS,
    validate_manifests,
)

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)

#: How many times a crashed background loop is restarted before the loader
#: gives up on it. Bounded so a loop that fails on every attempt becomes a
#: logged error instead of a busy retry.
MAX_WORKER_RESTARTS = 5
WORKER_BACKOFF_SECONDS = 1.0

# Backwards-compatible views of the manifest table. They are derived, not
# maintained separately, so the three loaders and the table cannot drift.
JOB_EXTENSIONS = {
    name: m.entry for name, m in MANIFESTS.items() if m.kind is ExtensionKind.JOB
}
BACKGROUND_EXTENSIONS = {
    name: m.entry
    for name, m in MANIFESTS.items()
    if m.kind is ExtensionKind.BACKGROUND
}
FEATURE_EXTENSIONS = {
    name: m.entry for name, m in MANIFESTS.items() if m.kind is ExtensionKind.FEATURE
}
KNOWN_EXTENSIONS = frozenset(MANIFESTS)
SKILLS_BY_EXTENSION = {
    name: set(m.skills) for name, m in MANIFESTS.items() if m.skills
}


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtensionPlan:
    """The validated set of extensions a configuration selects."""

    manifests: tuple[ExtensionManifest, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.manifests)

    def of_kind(self, kind: ExtensionKind) -> tuple[ExtensionManifest, ...]:
        return tuple(m for m in self.manifests if m.kind is kind)

    @property
    def features(self) -> tuple[ExtensionManifest, ...]:
        return self.of_kind(ExtensionKind.FEATURE)

    @property
    def jobs(self) -> tuple[ExtensionManifest, ...]:
        return self.of_kind(ExtensionKind.JOB)

    @property
    def background(self) -> tuple[ExtensionManifest, ...]:
        return self.of_kind(ExtensionKind.BACKGROUND)

    def actions(self) -> dict[str, str]:
        """action name -> owning extension, for every declared action."""
        return {
            action: manifest.name
            for manifest in self.jobs
            for action in manifest.actions
        }

    def summary(self) -> list[dict]:
        return [manifest.summary() for manifest in self.manifests]


def resolve_plan(config: Config) -> ExtensionPlan:
    """Validate the enabled set, or raise :class:`ExtensionError`.

    Pure data: nothing is imported, so a configuration error cannot have a side
    effect on the way to being reported.
    """
    validate_manifests()
    enabled = frozenset(getattr(config, "extensions", ()) or ())
    unknown = sorted(name for name in enabled if name not in MANIFESTS)
    if unknown:
        raise ExtensionError(
            f"unknown extensions: {unknown}; known extensions: {sorted(MANIFESTS)}"
        )

    selected = tuple(MANIFESTS[name] for name in sorted(enabled))

    for manifest in selected:
        if manifest.contract > CONTRACT_VERSION:
            raise ExtensionError(
                f"extension {manifest.name!r} needs host contract "
                f"v{manifest.contract}, but this host provides v{CONTRACT_VERSION}"
            )

    for manifest in selected:
        missing_known = [
            dep for dep in manifest.requires if dep not in MANIFESTS
        ]
        if missing_known:
            raise ExtensionError(
                f"extension {manifest.name!r} requires unknown extension(s) "
                f"{missing_known}"
            )
        missing = [dep for dep in manifest.requires if dep not in enabled]
        if missing:
            raise ExtensionError(
                f"extension {manifest.name!r} requires {missing}, which are not "
                f"enabled; add them to BOT_EXTENSIONS"
            )
        for soft in manifest.prefers:
            if soft not in enabled:
                log.debug(
                    "Extension %s works better with %s enabled; running without it",
                    manifest.name,
                    soft,
                )

    owners: dict[str, str] = {}
    for manifest in selected:
        for action in manifest.actions:
            owner = owners.get(action)
            if owner is not None:
                raise ExtensionError(
                    f"duplicate job action {action!r}: declared by both "
                    f"{owner!r} and {manifest.name!r}"
                )
            owners[action] = manifest.name

    return ExtensionPlan(selected)


def validate_names(config: Config) -> None:
    """Kept for callers that only need the startup check."""
    resolve_plan(config)


# ---------------------------------------------------------------------------
# contribution accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    """Counts of every host surface an extension could touch."""

    context: int
    observers: int
    tools: int
    closers: int
    binders: int
    workers: int
    # The singleton slots are compared by identity rather than by presence, so
    # one extension silently *replacing* another's media source or reply policy
    # counts as touching that surface.
    meme: object
    speech: object
    gate: object
    policy: object

    @classmethod
    def of(cls, host: FeatureHost) -> _Snapshot:
        return cls(
            context=len(host.context.names()),
            observers=len(host.observers),
            tools=len(host.tools.names()) if host.tools is not None else 0,
            closers=len(host.closers),
            binders=len(host.binders),
            workers=len(host.workers),
            meme=host.meme_source,
            speech=host.speech_source,
            gate=host.proactive_gate,
            policy=host.reply_policy,
        )

    @staticmethod
    def _replaced(before: object, after: object) -> bool:
        return after is not None and after is not before

    def changes(self, after: _Snapshot) -> set[Contribution]:
        """Which contributions this registration added."""
        touched: set[Contribution] = set()
        if after.context > self.context:
            touched.add(Contribution.CONTEXT)
        if after.tools > self.tools:
            touched.add(Contribution.TOOL)
        if after.observers > self.observers:
            touched.add(Contribution.OBSERVER)
        if after.closers > self.closers:
            touched.add(Contribution.CLOSER)
        if after.binders > self.binders:
            touched.add(Contribution.BINDER)
        if after.workers > self.workers:
            touched.add(Contribution.BACKGROUND)
        if self._replaced(self.meme, after.meme) or self._replaced(
            self.speech, after.speech
        ):
            touched.add(Contribution.MEDIA)
        if self._replaced(self.gate, after.gate):
            touched.add(Contribution.PROACTIVE_GATE)
        if self._replaced(self.policy, after.policy):
            touched.add(Contribution.REPLY_POLICY)
        return touched


class _ContributionGuard:
    """Watches the host while one extension registers.

    Wraps ``context.register`` and ``tools.register`` for the duration so the
    per-extension budget and the tool declaration list are enforced *at the
    point of use*, then compares before/after snapshots for everything that is
    a plain list.
    """

    def __init__(self, manifest: ExtensionManifest, host: FeatureHost):
        self.manifest = manifest
        self.host = host
        self.before = _Snapshot.of(host)
        self.context_names: list[str] = []
        self.tool_names: list[str] = []
        self._patched: list[tuple[object, str]] = []

    # -- patching -----------------------------------------------------------

    def __enter__(self) -> _ContributionGuard:
        context = self.host.context
        original_context = context.register

        def guarded_context_register(name, provider, **kwargs):
            self._check_context(name, kwargs)
            original_context(name, provider, **kwargs)
            self.context_names.append(name)

        context.register = guarded_context_register
        self._patched.append((context, "register"))

        tools = self.host.tools
        if tools is not None:
            original_tool = tools.register

            def guarded_tool_register(tool, **kwargs):
                self._check_tool(tool)
                kwargs.setdefault("source", self.manifest.name)
                original_tool(tool, **kwargs)
                self.tool_names.append(tool.name)

            tools.register = guarded_tool_register
            self._patched.append((tools, "register"))
        return self

    def __exit__(self, *_exc) -> bool:
        for target, attribute in reversed(self._patched):
            # pop, not restore: the wrapper was an instance attribute, and
            # leaving the bound original behind would shadow the class method.
            vars(target).pop(attribute, None)
        self._patched.clear()
        return False

    # -- per-surface rules --------------------------------------------------

    def _check_context(self, name: str, kwargs: dict) -> None:
        manifest = self.manifest
        if Contribution.CONTEXT not in manifest.contributes:
            raise ExtensionError(
                f"extension {manifest.name!r} registered context provider "
                f"{name!r}, but its manifest does not declare the "
                f"context_provider contribution"
            )
        if name in self.host.context.names():
            raise ExtensionError(
                f"extension {manifest.name!r} registered duplicate context "
                f"provider {name!r}; another extension already owns that name"
            )
        if len(self.context_names) >= manifest.max_context_providers:
            raise ExtensionError(
                f"extension {manifest.name!r} registered more than "
                f"{manifest.max_context_providers} context provider(s); raise "
                f"max_context_providers in its manifest to allow more"
            )
        max_chars = int(kwargs.get("max_chars", DEFAULT_MAX_CHARS))
        if max_chars > manifest.max_context_chars:
            raise ExtensionError(
                f"extension {manifest.name!r} context provider {name!r} asks for "
                f"{max_chars} characters; its manifest bounds it to "
                f"{manifest.max_context_chars}"
            )

    def _check_tool(self, tool) -> None:
        manifest = self.manifest
        declared = manifest.declared_tool(tool.name)
        if Contribution.TOOL not in manifest.contributes or declared is None:
            raise ExtensionError(
                f"extension {manifest.name!r} registered tool {tool.name!r}, "
                f"which its manifest does not declare"
            )
        wanted = declared.permissions
        if wanted is not None and tool.permissions & ~wanted:
            raise ExtensionError(
                f"extension {manifest.name!r} tool {tool.name!r} requests "
                f"permissions its declaration does not grant"
            )
        if int(tool.quota_per_minute) > int(declared.quota_per_minute):
            raise ExtensionError(
                f"extension {manifest.name!r} tool {tool.name!r} asks for a "
                f"quota above its declared {declared.quota_per_minute}/min"
            )

    # -- after --------------------------------------------------------------

    def check(self) -> None:
        """Compare the host against the manifest. Raises on anything undeclared."""
        after = _Snapshot.of(self.host)
        touched = self.before.changes(after)
        undeclared = sorted(
            item.value for item in touched if item not in self.manifest.contributes
        )
        if undeclared:
            raise ExtensionError(
                f"extension {self.manifest.name!r} contributed {undeclared} "
                f"without declaring them; add them to its manifest's "
                f"'contributes' set"
            )
        added_workers = after.workers - self.before.workers
        if added_workers > self.manifest.max_workers:
            raise ExtensionError(
                f"extension {self.manifest.name!r} started {added_workers} "
                f"background workers; its manifest allows "
                f"{self.manifest.max_workers}"
            )


# ---------------------------------------------------------------------------
# worker supervision
# ---------------------------------------------------------------------------


def _supervise(worker, name: str):
    """Wrap a zero-argument coroutine function so its crash is not global.

    Without this, one extension loop raising takes the gateway and the
    scheduler down with it, because ``app.py`` gathers every worker into one
    ``asyncio.gather``. A failure is logged and retried with a linear backoff,
    up to :data:`MAX_WORKER_RESTARTS` times, and then the worker stops on its
    own — a bounded number of log lines rather than a hot loop.
    """
    if getattr(worker, "__extension_supervised__", False):
        return worker

    async def supervised() -> None:
        for attempt in range(MAX_WORKER_RESTARTS + 1):
            try:
                await worker()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt >= MAX_WORKER_RESTARTS:
                    log.exception(
                        "Extension worker %s failed %d times; giving up",
                        name,
                        attempt + 1,
                    )
                    return
                log.exception(
                    "Extension worker %s failed (%d/%d); restarting",
                    name,
                    attempt + 1,
                    MAX_WORKER_RESTARTS,
                )
                await asyncio.sleep(WORKER_BACKOFF_SECONDS * (attempt + 1))

    supervised.__extension_supervised__ = True
    supervised.__name__ = getattr(worker, "__name__", f"worker_{name}")
    return supervised


def _supervise_coroutine(coro, name: str):
    """Wrap an already-created coroutine so a crash stays inside it."""

    async def supervised() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Extension worker %s failed; it will not be restarted", name
            )

    return supervised()


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _entry_callable(manifest: ExtensionManifest, default_attribute: str):
    module = import_module(manifest.module)
    attribute = manifest.attribute or default_attribute
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise ExtensionError(
            f"extension {manifest.name!r} has no {attribute!r} in "
            f"{manifest.module}"
        ) from exc


def enabled_skills(config: Config) -> frozenset[str]:
    names = {"group-chat"}
    for extension in getattr(config, "extensions", ()) or ():
        manifest = MANIFESTS.get(extension)
        if manifest is not None:
            names.update(manifest.skills)
    return frozenset(names)


def build_registry(config: Config) -> JobHandlerRegistry:
    """Register the job actions of every enabled job extension.

    The manifest declares which actions an extension adds; the registry is
    compared against that declaration in both directions. An action nobody
    declared is how two extensions end up fighting over one name — possibly
    years later, when a second package happens to pick the same string.
    """
    plan = resolve_plan(config)
    registry = JobHandlerRegistry()
    for manifest in plan.jobs:
        declare = _entry_callable(manifest, "register_jobs")
        before = registry.actions()
        declare(registry, config)
        added = registry.actions() - before
        undeclared = sorted(added - set(manifest.actions))
        if undeclared:
            raise ExtensionError(
                f"extension {manifest.name!r} registered job action(s) "
                f"{undeclared} that its manifest does not declare"
            )
        missing = sorted(set(manifest.actions) - registry.actions())
        if missing:
            raise ExtensionError(
                f"extension {manifest.name!r} declared job action(s) {missing} "
                f"but did not register them"
            )
    return registry


def build_features(
    config: Config, model, tools=None, *, memory=None
) -> FeatureHost:
    """Register every enabled feature extension against a fresh host."""
    plan = resolve_plan(config)
    host = FeatureHost(tools=tools) if tools is not None else FeatureHost()
    # Available before registration so a feature that reads memories finds it
    # there. Read-only: the memory package stays the sole owner of its data.
    host.memory_coordinator = memory
    for manifest in plan.features:
        _register_feature(manifest, host, config, model)
    return host


def _register_feature(
    manifest: ExtensionManifest, host: FeatureHost, config: Config, model
) -> None:
    register = _entry_callable(manifest, "register")
    guard = _ContributionGuard(manifest, host)
    with guard:
        register(host, config, model)
    guard.check()
    _supervise_new_workers(manifest, host, guard.before.workers)


def _supervise_new_workers(
    manifest: ExtensionManifest, host: FeatureHost, before: int
) -> None:
    """Bound failure propagation for workers an extension just added."""
    if len(host.workers) <= before:
        return
    host.workers[before:] = [
        _supervise(worker, manifest.name) for worker in host.workers[before:]
    ]


def bind_features(features: FeatureHost, service) -> None:
    """Run each feature's wiring callback now that the service exists."""
    for bind in features.binders:
        bind(service)


async def close_features(features: FeatureHost) -> None:
    """Shut every extension down through the one lifecycle exit."""
    await features.aclose()


def validate_features(config: Config) -> dict:
    """Startup report for every enabled feature extension."""
    plan = resolve_plan(config)
    return {
        manifest.name: _entry_callable(manifest, "validate")()
        for manifest in plan.features
    }


def build_workers(config: Config, service, gateway, features: FeatureHost) -> list:
    """Start the loop of every enabled background extension, supervised."""
    plan = resolve_plan(config)
    workers = []
    for manifest in plan.background:
        factory = _entry_callable(manifest, "build_worker")
        coro = factory(service, gateway, config, features.proactive_gate)
        if not isawaitable(coro):
            raise ExtensionError(
                f"extension {manifest.name!r} build_worker did not return an "
                f"awaitable"
            )
        workers.append(_supervise_coroutine(coro, manifest.name))
    return workers


def describe_extensions(config: Config) -> list[dict]:
    """Manifest summaries for the enabled set, for ``qunbot --check``."""
    plan = resolve_plan(config)
    described = []
    for manifest in plan.manifests:
        summary = manifest.summary()
        summary["tools"] = [
            {
                "name": tool.name,
                "permissions": describe_permissions(
                    tool.permissions or ToolPermission.NONE
                ),
                "quota_per_minute": tool.quota_per_minute,
            }
            for tool in manifest.tools
        ]
        summary["max_workers"] = manifest.max_workers
        summary["max_context_chars"] = manifest.max_context_chars
        described.append(summary)
    return described
