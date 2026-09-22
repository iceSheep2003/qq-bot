"""The bot's own mood: a self-contained bounded context.

Nothing outside this package needs to know how a mood is stored, decayed or
worded. The application touches exactly three names — ``EmotionConfig``,
``build_emotion`` and the object that comes back — plus the ``MoodObserver``
port in ``qunbot/ports.py``.

Deleting the feature is deleting this directory and the handful of wiring
lines in ``qunbot/app.py`` and ``qunbot/service.py``.
"""

from .config import EmotionConfig
from .state import BASELINE, DIMENSIONS, EmotionPolicy, Mood
from .system import EmotionSystem, build_emotion

__all__ = [
    "BASELINE",
    "DIMENSIONS",
    "EmotionConfig",
    "EmotionPolicy",
    "EmotionSystem",
    "Mood",
    "build_emotion",
]
