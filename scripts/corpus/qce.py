"""Local QCE adapter. No chat content or credentials are printed."""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler


def write_json(path: Path, value: object) -> None:
    """Atomic private checkpoint, never truncate the previous checkpoint."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class QCEClient:
    def __init__(self, base_url: str, security_file: Path):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("QCE must use a loopback HTTP address")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Credentials must not appear in the URL")
        config = json.loads(security_file.read_text(encoding="utf-8"))
        if config.get("disableIpWhitelist") or set(config.get("allowedIPs", [])) - {
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("QCE must restrict its API to loopback clients")
        self.base_url = base_url.rstrip("/")
        self._token = config["accessToken"]
        self._opener = build_opener(ProxyHandler({}))

    def request(self, path: str, body: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a local API path")
        request = Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=45) as response:
                envelope = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(
                f"QCE request failed with HTTP {exc.code}; inspect private logs"
            ) from None
        except (URLError, TimeoutError):
            raise RuntimeError(
                "QCE connection failed; POST is not retried automatically"
            ) from None
        if not envelope.get("success"):
            raise RuntimeError("QCE reported an API failure; inspect private logs")
        return envelope["data"]


def collect(
    client: QCEClient, root: Path, groups: list[str], poll_seconds: float = 5
) -> None:
    """Sequential exports with resumable task IDs, no duplicate POST on timeout."""
    if (
        not groups
        or len(set(groups)) != len(groups)
        or not all(g.isdecimal() for g in groups)
    ):
        raise ValueError("Supply a nonempty unique group-code allowlist")
    os.umask(0o077)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    raw = root / "raw"
    raw.mkdir(exist_ok=True, mode=0o700)
    checkpoint = root / "collection.json"
    state = (
        json.loads(checkpoint.read_text())
        if checkpoint.exists()
        else {
            "schema_version": 1,
            "qce_version": "6.3.2",
            "scope": "all_readable_group_history",
            "media_downloads": False,
            "groups": groups,
            "tasks": {},
        }
    )
    if state["groups"] != groups:
        raise ValueError(
            "Checkpoint allowlist differs; use a separate collection directory"
        )
    if not client.request("/api/system/status").get("online"):
        raise RuntimeError("QQ is not logged in")
    for group in groups:
        entry = state["tasks"].get(group)
        if entry is None:
            # Persist intent before POST. An ambiguous response requires manual task reconciliation.
            entry = {"status": "submitting"}
            state["tasks"][group] = entry
            write_json(checkpoint, state)
            result = client.request(
                "/api/messages/export",
                {
                    "peer": {"chatType": 2, "peerUid": group},
                    "format": "JSON",
                    "options": {
                        "outputDir": str(raw.resolve()),
                        "batchSize": 500,
                        "skipDownloadResourceTypes": [
                            "image",
                            "video",
                            "audio",
                            "file",
                        ],
                        "embedAvatarsAsBase64": False,
                        "debugExport": False,
                    },
                },
            )
            entry.update(task_id=result["taskId"], status="queued")
            write_json(checkpoint, state)
        if "task_id" not in entry:
            raise RuntimeError(
                "Ambiguous previous POST: reconcile QCE tasks before resuming"
            )
        while True:
            task = client.request("/api/tasks/" + quote(entry["task_id"], safe=""))
            entry.update(
                status=task.get("status"), message_count=task.get("messageCount", 0)
            )
            write_json(checkpoint, state)
            print(
                json.dumps(
                    {
                        "group": group,
                        "status": entry["status"],
                        "messages": entry["message_count"],
                        "progress": task.get("progress"),
                    }
                ),
                flush=True,
            )
            if entry["status"] == "completed":
                path = (raw / task["fileName"]).resolve()
                if not path.is_relative_to(raw.resolve()) or not path.is_file():
                    raise RuntimeError(
                        "Completed export file missing or outside private raw directory"
                    )
                path.chmod(0o600)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                entry.update(
                    file=str(path.relative_to(root)),
                    sha256=digest.hexdigest(),
                    bytes=path.stat().st_size,
                )
                write_json(checkpoint, state)
                break
            if entry["status"] in {"failed", "cancelled"}:
                raise RuntimeError(
                    "Export did not complete; inspect task privately, do not use partial data"
                )
            time.sleep(poll_seconds)
