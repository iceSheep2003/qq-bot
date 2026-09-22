"""Exam countdown poster renderer.

Rendered locally with Pillow so the daily poster never depends on the model
API being reachable. The model only supplies an optional one-line caption.
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import date

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1350
BACKGROUND_TOP = (17, 24, 56)
BACKGROUND_BOTTOM = (44, 26, 74)
ACCENT = (242, 193, 78)
PRIMARY = (244, 246, 252)
MUTED = (158, 168, 196)

# First match wins. macOS then common Linux CJK packages. DejaVu is a last
# resort that draws boxes for Han characters, so it is only a placeholder.
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# Used when the model returns nothing, rotated by date so the poster is not
# byte-identical every morning.
FALLBACK_MOTTOS = (
    "你走的每一步都算数",
    "把今天过好，就是最好的准备",
    "慢一点没关系，别停就好",
    "此刻的枯燥，会变成考场上的从容",
    "稳住，我们能赢",
    "坚持到最后一科，就已经赢过很多人",
)


def resolve_font(explicit: str | None) -> str | None:
    """Return the first usable font path, honouring an explicit override."""
    for path in (explicit, *FONT_CANDIDATES):
        if path:
            try:
                ImageFont.truetype(path, 16)
            except OSError:
                continue
            return path
    return None


def _gradient() -> Image.Image:
    column = Image.new("RGB", (1, HEIGHT))
    pixels = column.load()
    for y in range(HEIGHT):
        ratio = y / (HEIGHT - 1)
        pixels[0, y] = tuple(
            round(start + (end - start) * ratio)
            for start, end in zip(BACKGROUND_TOP, BACKGROUND_BOTTOM)
        )
    return column.resize((WIDTH, HEIGHT), Image.Resampling.BILINEAR)


class ExamCountdownPoster:
    """Renders a countdown poster for a fixed exam date."""

    def __init__(self, exam_date: date, font_path: str | None = None):
        self.exam_date = exam_date
        self.font_path = resolve_font(font_path)
        if self.font_path is None:
            raise ValueError("no usable font found; set BOT_POSTER_FONT")

    def days_left(self, today: date) -> int:
        return (self.exam_date - today).days

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(self.font_path, size)

    def _spaced_text(
        self, draw: ImageDraw.ImageDraw, text: str, *, y: int, font, fill, spacing: int
    ) -> None:
        """Draw letter-spaced text centred horizontally."""
        widths = [draw.textlength(char, font=font) for char in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x = (WIDTH - total) / 2
        for char, width in zip(text, widths):
            draw.text((x, y), char, font=font, fill=fill)
            x += width + spacing

    def _centred(
        self, draw: ImageDraw.ImageDraw, text: str, *, y: int, font, fill
    ) -> None:
        draw.text(
            ((WIDTH - draw.textlength(text, font=font)) / 2, y),
            text,
            font=font,
            fill=fill,
        )

    def render(self, today: date, motto: str = "") -> str | None:
        """Return a OneBot-ready ``base64://`` PNG, or None once the exam passed."""
        days = self.days_left(today)
        if days < 0:
            return None

        image = _gradient().convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        # Two faint rings to keep the flat gradient from looking empty.
        draw.ellipse((-260, -360, 620, 520), outline=(255, 255, 255, 14), width=3)
        draw.ellipse((620, 900, 1420, 1700), outline=(255, 255, 255, 14), width=3)

        self._spaced_text(
            draw,
            "考研倒计时",
            y=225,
            font=self._font(52),
            fill=ACCENT,
            spacing=14,
        )

        if days == 0:
            headline, suffix = "就是今天", "加油"
        else:
            headline, suffix = str(days), "天"
        number_font = self._font(300 if len(headline) <= 2 else 200)
        suffix_font = self._font(72)
        number_width = draw.textlength(headline, font=number_font)
        suffix_width = draw.textlength(suffix, font=suffix_font)
        start = (WIDTH - number_width - 24 - suffix_width) / 2
        draw.text((start, 505), headline, font=number_font, fill=PRIMARY)
        draw.text(
            (start + number_width + 24, 715), suffix, font=suffix_font, fill=ACCENT
        )

        self._centred(
            draw,
            self.exam_date.strftime("%Y.%m.%d"),
            y=905,
            font=self._font(44),
            fill=MUTED,
        )
        draw.line(
            ((WIDTH - 90) / 2, 1005, (WIDTH + 90) / 2, 1005), fill=ACCENT, width=3
        )

        # Model captions arrive with newlines and double spaces, which Pillow
        # cannot measure. Collapse to one line, then fall back to a stock motto.
        line = " ".join(motto.split())
        line = line or FALLBACK_MOTTOS[today.toordinal() % len(FALLBACK_MOTTOS)]
        self._centred(draw, line[:24], y=1065, font=self._font(46), fill=PRIMARY)

        image = Image.alpha_composite(image, overlay).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return "base64://" + base64.b64encode(buffer.getvalue()).decode("ascii")
