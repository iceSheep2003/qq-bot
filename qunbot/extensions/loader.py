"""Owner-controlled extension composition; no chat or directory auto-discovery."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from ..scheduling.registry import JobHandlerRegistry
from .features import FeatureHost

if TYPE_CHECKING:
    from ..config import Config


# Local allowlist, not automatic module discovery. A new feature contributes one
# entry here and its own register_jobs() implementation.
JOB_EXTENSIONS = {
    "scheduled_chat": "qunbot.extensions.scheduled_chat.job:register_jobs",
    "exam_poster": "qunbot.extensions.exam_poster.job:register_jobs",
}
BACKGROUND_EXTENSIONS = {
    "proactive_chat": "qunbot.extensions.proactive_chat.runner:build_worker",
}
FEATURE_EXTENSIONS = {
    "mood": "qunbot.extensions.mood.register",
    "memes": "qunbot.extensions.memes.register",
    "voice": "qunbot.extensions.voice.register",
}
KNOWN_EXTENSIONS = (
    JOB_EXTENSIONS.keys() | BACKGROUND_EXTENSIONS.keys() | FEATURE_EXTENSIONS.keys()
)
SKILLS_BY_EXTENSION = {
    "scheduled_chat": {"proactive-chat"},
    "proactive_chat": {"proactive-chat"},
    "memes": {"meme"},
    "voice": {"voice"},
}


def enabled_skills(config: Config) -> frozenset[str]:
    names = {"group-chat"}
    for extension in config.extensions:
        names.update(SKILLS_BY_EXTENSION.get(extension, ()))
    return frozenset(names)


def validate_names(config: Config) -> None:
    unknown = config.extensions - KNOWN_EXTENSIONS
    if unknown:
        raise ValueError(f"unknown extensions: {sorted(unknown)}")


def build_registry(config: Config) -> JobHandlerRegistry:
    registry = JobHandlerRegistry()
    validate_names(config)
    for name in sorted(config.extensions & JOB_EXTENSIONS.keys()):
        module_name, factory_name = JOB_EXTENSIONS[name].split(":", 1)
        register = getattr(import_module(module_name), factory_name)
        register(registry, config)
    return registry


def build_features(config: Config, model, tools=None) -> FeatureHost:
    validate_names(config)
    host = FeatureHost(tools=tools) if tools is not None else FeatureHost()
    for name in sorted(config.extensions & FEATURE_EXTENSIONS.keys()):
        import_module(FEATURE_EXTENSIONS[name]).register(host, config, model)
    return host


def validate_features(config: Config) -> dict:
    validate_names(config)
    return {
        name: import_module(FEATURE_EXTENSIONS[name]).validate()
        for name in sorted(config.extensions & FEATURE_EXTENSIONS.keys())
    }


def build_workers(config: Config, service, gateway, features: FeatureHost) -> list:
    workers = []
    for name in sorted(config.extensions & BACKGROUND_EXTENSIONS.keys()):
        module_name, factory_name = BACKGROUND_EXTENSIONS[name].split(":", 1)
        workers.append(getattr(import_module(module_name), factory_name)(service, gateway, config, features.proactive_gate))
    return workers
