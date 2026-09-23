"""Structured outbound message parts, and the marker scanner that feeds them.

The bot used to describe media with markers (``[[meme:tag]]``, ``[[voice]]``)
buried in free text, and stripped them with a global regex. That is fragile: a
marker spelled by a group member, repeated, or placed mid-sentence silently
mutated the reply. This module replaces that with one bounded, end-anchored
scan that produces an explicit part list.

Rules enforced by :func:`parse_outbound`:

* markers are only recognised in a *trailing run* at the very end of the text;
* each marker kind may appear at most once in that run;
* an @ target is only accepted when it names a member of the current group;
* anything else — a mid-sentence marker, a malformed one, a repeated one, an
  unknown @ — is kept as literal text. Nothing is silently swallowed.

Parts are the OneBot-neutral values ``Text`` / ``Image`` / ``Voice`` / ``At``.
``Image`` and ``Voice`` leave ``source`` empty until
:meth:`ReplyMediaProcessor.resolve` fills it from the meme catalog or the TTS
provider, so a parse can be inspected without touching any media backend.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Union
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# --- limits ---------------------------------------------------------------

DEFAULT_MAX_IMAGE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_URL_CHARS = 2048
# A marker is at most ~50 chars; four of them plus whitespace fit comfortably.
# Only this tail window is ever scanned, so the scan cost is constant.
MARKER_TAIL_CHARS = 400

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
)
IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
)


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("Ignoring non-numeric %s=%r; using %d", name, raw, default)
        return default
    return max(low, min(high, value))


def max_image_bytes() -> int:
    """``BOT_MEDIA_MAX_IMAGE_BYTES``, clamped to 1 KiB .. 64 MiB."""
    return _env_int(
        "BOT_MEDIA_MAX_IMAGE_BYTES",
        DEFAULT_MAX_IMAGE_BYTES,
        low=1024,
        high=64 * 1024 * 1024,
    )


def max_url_chars() -> int:
    """``BOT_MEDIA_MAX_URL_CHARS``, clamped to 64 .. 8192."""
    return _env_int(
        "BOT_MEDIA_MAX_URL_CHARS", DEFAULT_MAX_URL_CHARS, low=64, high=8192
    )


# --- parts ----------------------------------------------------------------


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class At:
    """An @ target. ``user_id`` is a QQ number, already syntax-checked."""

    user_id: str


@dataclass(frozen=True)
class Image:
    """``source`` is the OneBot ``file`` value; empty until resolved.

    ``tag`` is the meme-catalog key the image came from (empty for a direct
    source). ``ref`` is the unresolved tag while parsing.
    """

    source: str = ""
    tag: str = ""
    ref: str = ""


@dataclass(frozen=True)
class Voice:
    """``source`` is the OneBot ``file`` value; empty until resolved."""

    source: str = ""
    ref: bool = False
    text: str = ""


Part = Union[Text, At, Image, Voice]


@dataclass(frozen=True)
class OutboundMessage:
    """An ordered, structured outbound reply."""

    parts: tuple[Part, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, Text))

    @property
    def at_user(self) -> str | None:
        for part in self.parts:
            if isinstance(part, At):
                return part.user_id
        return None

    @property
    def image(self) -> str | None:
        for part in self.parts:
            if isinstance(part, Image) and part.source:
                return part.source
        return None

    @property
    def voice(self) -> str | None:
        for part in self.parts:
            if isinstance(part, Voice) and part.source:
                return part.source
        return None

    def send_kwargs(self) -> dict[str, str]:
        """Keyword arguments for ``MessageSender.send``, empty fields omitted."""
        kwargs: dict[str, str] = {}
        if self.text:
            kwargs["text"] = self.text
        if (target := self.at_user) is not None:
            kwargs["at_user"] = target
        if (image := self.image) is not None:
            kwargs["image"] = image
        if (voice := self.voice) is not None:
            kwargs["voice"] = voice
        return kwargs


# --- marker grammar -------------------------------------------------------

# One well-formed marker. Malformed text simply does not match, so it can never
# be part of a trailing run and therefore survives as literal text.
MARKER = re.compile(
    r"\[\[(?:(?P<meme>meme):(?P<tag>[A-Za-z0-9_-]{1,40})"
    r"|(?P<voice>voice)"
    r"|(?P<at>at):(?P<qq>[0-9]{5,12}))\]\]"
)

# A maximal trailing run of markers and whitespace. Anchored at the end, so a
# marker followed by anything but another marker stays literal.
_TRAILING_RUN = re.compile(
    r"(?:[ \t\r\n]*"
    r"\[\[(?:meme:[A-Za-z0-9_-]{1,40}|voice|at:[0-9]{5,12})\]\]"
    r")+[ \t\r\n]*$"
)

# Liberal form used only to sanitise *display* text (poster captions) that will
# never be sent as media, so position rules do not apply.
_ANY_MARKER = re.compile(
    r"\[\[(?:meme:[A-Za-z0-9_-]{1,40}|voice|at:[0-9]{5,12})\]\]"
)


def roster_from_event(event: object) -> frozenset[str]:
    """Group members the bot has structured evidence for.

    The trigger sender and everyone @-ed in the triggering message. Both are
    facts the platform asserted, not strings the model produced, so they are
    safe to validate an @ target against.
    """
    ids = {str(getattr(event, "user_id", "") or "")}
    for user in getattr(event, "at_users", ()) or ():
        ids.add(str(user))
    return frozenset(i for i in ids if i.isdigit())


def validate_at_target(target: str, roster: Collection[str] | None) -> str | None:
    """Return ``target`` when it is a well-formed, in-roster QQ number."""
    text = str(target or "").strip()
    if not text.isdigit() or not 5 <= len(text) <= 12:
        return None
    if roster is None or text not in roster:
        return None
    return text


def _scan_markers(run: str) -> dict[str, str] | None:
    """Tokenise a trailing run. ``None`` means the whole run is not usable."""
    found: dict[str, str] = {}
    for match in MARKER.finditer(run):
        if match.group("meme") is not None:
            kind, value = "meme", match.group("tag")
        elif match.group("voice") is not None:
            kind, value = "voice", ""
        else:
            kind, value = "at", match.group("qq")
        if kind in found:  # each kind at most once
            return None
        found[kind] = value
    if not found:
        return None
    if _ANY_MARKER.sub("", run).strip():  # trailing junk between markers
        return None
    return found


def parse_outbound(
    text: str, *, allowed_at: Collection[str] | None = None
) -> OutboundMessage:
    """Parse one model reply into structured parts.

    ``allowed_at`` is the set of QQ numbers that may be @-ed (normally
    ``roster_from_event(event)``). When it is ``None`` no @ marker is ever
    accepted, so marking someone requires the caller to supply a roster.
    """
    raw = text or ""
    if "[[" not in raw:
        return OutboundMessage((Text(raw.strip()),)) if raw.strip() else OutboundMessage()

    tail = raw[-MARKER_TAIL_CHARS:]
    match = _TRAILING_RUN.search(tail)
    if match is None:
        return _literal(raw)

    found = _scan_markers(match.group(0))
    if found is None:
        return _literal(raw)

    target = None
    if "at" in found:
        target = validate_at_target(found["at"], allowed_at)
        if target is None:  # unknown/non-member: keep the run as literal text
            return _literal(raw)

    clean = raw[: len(raw) - len(tail) + match.start()].strip()
    parts: list[Part] = []
    if clean:
        parts.append(Text(clean))
    if "meme" in found:
        parts.append(Image(ref=found["meme"], tag=found["meme"]))
    if "voice" in found:
        parts.append(Voice(ref=True, text=clean))
    if target is not None:
        parts.append(At(user_id=target))
    return OutboundMessage(tuple(parts))


def _literal(raw: str) -> OutboundMessage:
    stripped = raw.strip()
    return OutboundMessage((Text(stripped),)) if stripped else OutboundMessage()


def strip_markers(text: str) -> str:
    """Remove every well-formed marker token, wherever it appears.

    A display sanitiser for text that will not be sent as media (for example a
    poster caption). Outbound media parsing must use :func:`parse_outbound`
    instead, which is stricter about position.
    """
    return _ANY_MARKER.sub("", text or "").strip()


# --- media source validation ----------------------------------------------


def payload_bytes(source: str) -> int | None:
    """Size of an inline media payload, or ``None`` when not measurable."""
    if source.startswith("base64://"):
        encoded = source[len("base64://") :]
        return len(encoded) * 3 // 4
    return None


def valid_image_source(source: str, *, max_bytes: int) -> bool:
    if not source:
        return False
    if source.startswith("base64://"):
        size = payload_bytes(source)
        return size is not None and size <= max_bytes
    if source.startswith("file://"):
        return len(source) <= max_url_chars()
    return valid_media_url(source)


def valid_voice_source(source: str) -> bool:
    """Voice sources are inline audio or an http(s) URL — never a local path."""
    if not source:
        return False
    if source.startswith("base64://"):
        return True
    return valid_media_url(source)


def valid_media_url(url: str) -> bool:
    """Static check of an http(s) media URL.

    This is the part of "validate URL, size, MIME and timeout" that needs no
    I/O. Size, MIME and reachability can only be checked once something
    fetches the URL; :class:`MediaUrlPolicy` accepts those as arguments so the
    same policy object can enforce them at that point.
    """
    if not url or len(url) > max_url_chars():
        return False
    try:
        split = urlsplit(url)
    except ValueError:
        return False
    if split.scheme not in {"http", "https"}:
        return False
    if not split.netloc or split.username or split.password:
        return False
    return True


@dataclass(frozen=True)
class MediaUrlPolicy:
    """Inbound image policy: scheme, credentials, length, extension, MIME, size.

    ``max_bytes`` is enforced only when a caller that actually fetched the
    bytes passes ``size=``. Until the visual adapter exists this object is the
    shared contract those checks use, and the static checks apply immediately.
    """

    schemes: frozenset[str] = frozenset({"http", "https"})
    extensions: frozenset[str] = IMAGE_EXTENSIONS
    mime_types: frozenset[str] = IMAGE_MIME_TYPES
    max_url_chars: int = DEFAULT_MAX_URL_CHARS
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES
    require_extension: bool = False

    def check(
        self, url: str, *, content_type: str | None = None, size: int | None = None
    ) -> str | None:
        """Return a rejection reason, or ``None`` when the URL is acceptable."""
        if not url:
            return "empty url"
        if len(url) > self.max_url_chars:
            return "url too long"
        try:
            split = urlsplit(url)
        except ValueError:
            return "unparsable url"
        if split.scheme not in self.schemes:
            return f"scheme not allowed: {split.scheme or '(none)'}"
        if not split.netloc or split.username or split.password:
            return "missing host or embedded credentials"
        suffix = os.path.splitext(split.path)[1].lower()
        if self.require_extension and suffix not in self.extensions:
            return f"extension not allowed: {suffix or '(none)'}"
        if content_type is not None:
            mime = content_type.split(";")[0].strip().lower()
            if mime not in self.mime_types:
                return f"mime not allowed: {mime or '(none)'}"
        if size is not None and size > self.max_bytes:
            return f"too large: {size} bytes"
        return None

    def accepts(
        self, url: str, *, content_type: str | None = None, size: int | None = None
    ) -> bool:
        return self.check(url, content_type=content_type, size=size) is None


DEFAULT_IMAGE_POLICY = MediaUrlPolicy()
