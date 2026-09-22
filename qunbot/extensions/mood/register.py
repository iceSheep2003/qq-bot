"""Mood owns its state, dynamic context and post-reply observation."""

from ...emotion import EmotionConfig, build_emotion
from ...runtime.context import Trust


def register(host, _config, model) -> None:
    config = EmotionConfig.from_env()
    if not config.enabled:
        return
    mood = build_emotion(config, model)
    # Model-judged and short-lived: the bot's own state, never an instruction.
    # Low priority so a budget squeeze drops the mood line before it drops the
    # speaker's own history.
    host.context.register(
        "mood",
        lambda event: mood.narration(event.scope),
        trust=Trust.DERIVED,
        priority=60,
        max_chars=200,
    )
    host.observers.append(mood)
    host.proactive_gate = mood
    host.closers.append(mood.close)


def validate() -> dict:
    config = EmotionConfig.from_env()
    return {"enabled": config.enabled, "auto": config.auto_enabled}
