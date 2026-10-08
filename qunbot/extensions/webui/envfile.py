from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .schema import BY_KEY, SETTINGS, validate_value

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _decode(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1].replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return raw


def _encode(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./,:+@-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class EnvConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines(keepends=True)

    def read(self) -> dict[str, dict[str, object]]:
        values: dict[str, str] = {}
        for line in self._lines():
            match = _LINE.match(line.rstrip("\r\n"))
            if match:
                values[match.group(1)] = _decode(match.group(2))
        # The checked-in example is also the documented default layer. Showing
        # it as a fallback makes the UI describe the effective configuration,
        # instead of presenting every omitted default as an empty/disabled
        # value. Secret examples are never used or marked configured.
        defaults: dict[str, str] = {}
        example = self.path.with_name(".env.example")
        if example.is_file():
            for line in example.read_text(encoding="utf-8").splitlines():
                match = _LINE.match(line)
                if match:
                    defaults[match.group(1)] = _decode(match.group(2))
        result = {}
        for setting in SETTINGS:
            configured_value = values.get(setting.key, "")
            value = configured_value or ("" if setting.secret else defaults.get(setting.key, ""))
            result[setting.key] = {
                "value": "" if setting.secret else value,
                "configured": bool(configured_value),
                "secret": setting.secret,
            }
        return result

    def update(self, changes: dict[str, object]) -> list[str]:
        unknown = sorted(set(changes) - set(BY_KEY))
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        normalized: dict[str, str] = {}
        for key, value in changes.items():
            setting = BY_KEY[key]
            # Empty password fields intentionally mean "keep the existing key".
            if setting.secret and str(value) == "":
                continue
            normalized[key] = validate_value(setting, value)
        if not normalized:
            return []

        lines = self._lines()
        seen: set[str] = set()
        output: list[str] = []
        for line in lines:
            match = _LINE.match(line.rstrip("\r\n"))
            key = match.group(1) if match else None
            if key in normalized:
                output.append(f"{key}={_encode(normalized[key])}\n")
                seen.add(key)
            else:
                output.append(line)
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\n"
        for key, value in normalized.items():
            if key not in seen:
                output.append(f"{key}={_encode(value)}\n")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.writelines(output)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return list(normalized)
