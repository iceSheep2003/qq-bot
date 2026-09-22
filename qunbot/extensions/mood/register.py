"""Mood owns its state, dynamic context and post-reply observation."""

from ...emotion import EmotionConfig, build_emotion


def register(host, _config, model) -> None:
    config = EmotionConfig.from_env()
    if not config.enabled:
        return
    mood = build_emotion(config, model)
    host.context.register("mood", lambda event: mood.narration(event.scope))
    host.observers.append(mood)
    host.proactive_gate = mood
    host.closers.append(mood.close)


def validate() -> dict:
    config = EmotionConfig.from_env()
    return {"enabled": config.enabled, "auto": config.auto_enabled}
