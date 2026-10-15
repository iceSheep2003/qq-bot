"""Pseudonymization, not a promise of irreversible anonymization."""

import hashlib
import hmac
import re
from collections import defaultdict


class IdentityMap:
    def __init__(self, salt: bytes, *, strict_conflicts: bool = True):
        if len(salt) < 32:
            raise ValueError("Use at least 32 random bytes for the private salt")
        self.salt = salt
        self.strict_conflicts = strict_conflicts
        self.conflicts = 0
        self.aliases: dict[str, str] = {}
        self.names: dict[str, set[str]] = defaultdict(set)
        self._pattern: re.Pattern | None = None

    def token(self, kind: str, value: object) -> str:
        digest = hmac.new(
            self.salt, f"{kind}\0{value}".encode(), hashlib.sha256
        ).hexdigest()[:24]
        return f"{kind}_{digest}"

    def register(self, sender: dict) -> None:
        uid, uin = str(sender.get("uid") or ""), str(sender.get("uin") or "")
        canonical = uin if uin not in {"", "0"} else uid
        if not canonical:
            return
        person = self.token("user", canonical)
        for alias in (uid, uin):
            if alias and alias != "0":
                old = self.aliases.get(alias)
                if old is not None and old != person:
                    self.conflicts += 1
                    if self.strict_conflicts:
                        raise ValueError("Conflicting sender identities in source exports")
                    # Behavior extraction never joins people across messages;
                    # keep the first stable pseudonym and record the anomaly.
                    continue
                self.aliases[alias] = person
        for key in ("name", "nickname", "groupCard", "remark"):
            name = sender.get(key)
            if isinstance(name, str) and name and name not in {"未知用户", "unknown"}:
                self.names[name].add(person)

    def seal(self) -> None:
        # Names are ambiguous natural language; only long unique aliases are replaced in prose.
        terms = [k for k, v in self.names.items() if len(k) >= 4 and len(v) == 1]
        terms += list(self.aliases)
        self._pattern = (
            re.compile(
                "|".join(
                    re.escape(k) for k in sorted(set(terms), key=lambda s: (-len(s), s))
                )
            )
            if terms
            else None
        )

    def person(self, value: object) -> str | None:
        key = str(value or "")
        return (
            None
            if key in {"", "0", "unknown"}
            else self.aliases.get(key, self.token("user", key))
        )

    def text(self, value: str) -> tuple[str, list[str]]:
        flags = []
        text = value
        if self._pattern is not None:

            def replace(match: re.Match) -> str:
                flags.append("known_identity")
                key = match.group()
                return self.aliases.get(key) or next(iter(self.names[key]))

            text = self._pattern.sub(replace, text)
        patterns = (
            ("url", r"(?i)(?:https?://|www\.)[^\s<>]+", "[链接]"),
            ("email", r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]"),
            ("id_number", r"(?<!\d)\d{17}[\dXx](?!\d)", "[证件号]"),
            ("phone", r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[手机号]"),
            ("long_number", r"(?<!\d)\d{8,16}(?!\d)", "[长数字]"),
            (
                "contact",
                r"(?i)(?:微信|wechat|vx|v信|qq号|Q群)\s*[:：=]?\s*[A-Za-z0-9_-]{5,}",
                "[联系方式]",
            ),
            ("nt_uid", r"u_[A-Za-z0-9_-]{10,}", "[用户标识]"),
        )
        for kind, pattern, replacement in patterns:
            text, count = re.subn(pattern, replacement, text)
            if count:
                flags.append(kind)
        return text, sorted(set(flags))
