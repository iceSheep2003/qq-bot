from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import markdown
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .config import WebUIConfig
from .envfile import EnvConfigStore
from .review_queue import ReviewFileEditor, ReviewFormatError
from .schema import SECTIONS, SETTINGS
from .memory_query import MemoryConsoleQuery
from .persona_review import PersonaQueueQuery

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


class WebUIServer:
    def __init__(self, config: WebUIConfig):
        self.config = config
        self.store = EnvConfigStore(config.env_path)
        self.started_at = time.time()
        self.restart_required = False
        self._server: ThreadingHTTPServer | None = None
        self._static = Path(__file__).with_name("static")
        self._guide = Path(__file__).with_name("docs") / "GUIDE.md"
        self.memory_query: MemoryConsoleQuery | None = None
        self.persona_queue: PersonaQueueQuery | None = None
        self.schedule_management = None
        # The one write surface outside the env editor. It rewrites a file a
        # deployer owns, and nothing else in the bot reads decisions from
        # anywhere else.
        self.review = ReviewFileEditor(config.review_path)

    def bind_runtime(self, service) -> None:
        """Bind the console's projections after the composition root exists."""
        self.memory_query = MemoryConsoleQuery(service)
        self.persona_queue = PersonaQueueQuery(
            service.conversations.db, self.config.persona_path
        )

    def bind_schedule_management(self, management) -> None:
        self.schedule_management = management

    async def run(self) -> None:
        if self.schedule_management is not None:
            self.schedule_management.bind_loop(asyncio.get_running_loop())
        handler = self._handler()
        self._server = ThreadingHTTPServer((self.config.host, self.config.port), handler)
        self._server.daemon_threads = True
        log.info("WebUI listening on http://%s:%s", self.config.host, self.config.port)
        try:
            await asyncio.to_thread(self._server.serve_forever, poll_interval=0.2)
        finally:
            self._server.server_close()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "QunBot-WebUI/1.0"

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("WebUI %s", fmt % args)

            def _headers(self, status: int, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self'; script-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                )
                self.end_headers()

            def _json(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(payload)

            def _text(self, status: int, body: str, content_type: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                supplied = header[7:] if header.startswith("Bearer ") else ""
                return bool(supplied) and hmac.compare_digest(supplied, owner.config.token)

            def _body(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise ValueError("invalid Content-Length") from error
                if length <= 0 or length > MAX_BODY:
                    raise ValueError("request body must be between 1 byte and 64 KiB")
                try:
                    body = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError("request body must be valid JSON") from error
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
                return body

            def _review_write(self, apply, body: dict[str, Any]) -> None:
                """Run one review-file edit and report the file back.

                A malformed file on disk is a conflict, not a bad request: the
                caller asked for something reasonable and the file is the
                problem, and the distinction tells the console whether to
                reload rather than retry.
                """
                scope = str(body.get("scope", "")).strip() or None
                try:
                    payload = apply(str(body.get("term", "")), scope, body)
                except ReviewFormatError as error:
                    self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                    return
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                except OSError as error:
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": f"写入审查文件失败：{error}"},
                    )
                    return
                self._json(HTTPStatus.OK, {"ok": True, "data": {"payload": payload}})

            def _asset(self, name: str, mime: str) -> None:
                path = owner._static / name
                if not path.is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                payload = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/":
                    self._asset("index.html", "text/html; charset=utf-8")
                    return
                if path == "/app.css":
                    self._asset("app.css", "text/css; charset=utf-8")
                    return
                if path == "/app.js":
                    self._asset("app.js", "text/javascript; charset=utf-8")
                    return
                if not path.startswith("/api/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                if path == "/api/config":
                    self._json(HTTPStatus.OK, {
                        "sections": [
                            {"id": section, "label": label, "description": description}
                            for section, label, description in SECTIONS
                        ],
                        "settings": [setting.public() for setting in SETTINGS],
                        "values": owner.store.read(),
                    })
                elif path == "/api/guide":
                    source = owner._guide.read_text(encoding="utf-8")
                    self._text(
                        HTTPStatus.OK,
                        markdown.markdown(
                            source,
                            extensions=("extra", "sane_lists", "toc"),
                            extension_configs={"toc": {"toc_depth": "2-3"}},
                            output_format="html5",
                        ),
                        "text/html; charset=utf-8",
                    )
                elif path == "/api/status":
                    values = owner.store.read()
                    extensions = str(values["BOT_EXTENSIONS"]["value"] or "")
                    self._json(HTTPStatus.OK, {
                        "online": True,
                        "uptime_seconds": int(time.time() - owner.started_at),
                        "restart_required": owner.restart_required,
                        "extension_count": len([x for x in extensions.split(",") if x.strip()]),
                        "env_path": str(owner.config.env_path),
                    })
                elif path.startswith("/api/memory/"):
                    if owner.memory_query is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "记忆服务尚未就绪"})
                        return
                    scope = str(query.get("scope", [""])[0]).strip() or None
                    limit_raw = str(query.get("limit", ["100"])[0])
                    try:
                        limit = max(1, min(200, int(limit_raw)))
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "limit 必须是整数"})
                        return
                    dashboard = owner.memory_query
                    if path == "/api/memory/overview":
                        data = dashboard.overview(scope)
                    elif path == "/api/memory/items":
                        data = dashboard.memories(
                            scope,
                            status=str(query.get("status", [""])[0]) or None,
                            query=str(query.get("q", [""])[0]), limit=limit,
                        )
                    elif path.startswith("/api/memory/items/"):
                        try:
                            item_id = int(path.rsplit("/", 1)[1])
                        except ValueError:
                            self._json(HTTPStatus.BAD_REQUEST, {"error": "记忆 ID 无效"})
                            return
                        data = dashboard.memory_detail(item_id)
                        if data is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "记忆不存在"})
                            return
                    elif path == "/api/memory/topics":
                        data = dashboard.topics(scope, limit)
                    elif path.startswith("/api/memory/topics/"):
                        try:
                            topic_id = int(path.rsplit("/", 1)[1])
                        except ValueError:
                            self._json(HTTPStatus.BAD_REQUEST, {"error": "Topic ID 无效"})
                            return
                        data = dashboard.topic_detail(topic_id)
                        if data is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "Topic 不存在"})
                            return
                    elif path == "/api/memory/people":
                        data = dashboard.people_list(scope)
                    elif path.startswith("/api/memory/people/"):
                        parts = path.split("/")
                        if len(parts) != 6:
                            self._json(HTTPStatus.BAD_REQUEST, {"error": "人物路径无效"})
                            return
                        data = dashboard.person_detail(parts[4], parts[5])
                    elif path == "/api/memory/slang":
                        data = dashboard.slang(scope, limit)
                    elif path == "/api/memory/extractions":
                        data = dashboard.extractions(scope, limit)
                    elif path == "/api/memory/graph":
                        data = dashboard.graph(scope, limit)
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path == "/api/review/persona":
                    if owner.persona_queue is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "记忆服务尚未就绪"},
                        )
                        return
                    try:
                        limit = max(1, min(200, int(query.get("limit", ["100"])[0])))
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "limit 必须是整数"})
                        return
                    data = owner.persona_queue.proposals(
                        str(query.get("scope", [""])[0]).strip() or None, limit
                    )
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/review/"):
                    try:
                        data = owner.review.summary()
                    except ReviewFormatError as error:
                        # Refused, not repaired: the feature keeps its last good
                        # decisions when the file will not parse, so silently
                        # rewriting one would look like the console had wiped
                        # every approval.
                        self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/schedules/"):
                    manager = owner.schedule_management
                    if manager is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"})
                        return
                    try:
                        if path == "/api/schedules/overview":
                            data = manager.overview()
                        elif path == "/api/schedules/jobs":
                            data = manager.jobs()
                        elif path == "/api/schedules/runs":
                            job_raw = str(query.get("job_id", [""])[0]).strip()
                            data = manager.runs(int(job_raw) if job_raw else None, int(query.get("limit", ["100"])[0]))
                        elif path == "/api/schedules/suggestions":
                            data = manager.suggestions_list()
                        elif path == "/api/schedules/preview":
                            data = manager.preview(
                                str(query.get("kind", [""])[0]),
                                str(query.get("value", [""])[0]),
                                int(query.get("count", ["3"])[0]),
                            )
                        else:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                    except (ValueError, OverflowError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                try:
                    body = self._body()
                except ValueError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                if path == "/api/login":
                    supplied = str(body.get("token", ""))
                    if supplied and hmac.compare_digest(supplied, owner.config.token):
                        self._json(HTTPStatus.OK, {"ok": True})
                    else:
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                if path.startswith("/api/review/persona/"):
                    queue = owner.persona_queue
                    if queue is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "记忆服务尚未就绪"},
                        )
                        return
                    try:
                        proposal_id = int(path.rsplit("/", 1)[1])
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "提议 ID 无效"})
                        return
                    try:
                        row = queue.decide(
                            proposal_id,
                            str(body.get("status", "")),
                            note=str(body.get("note", "")),
                        )
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    except OSError as error:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"写入人格示例失败：{error}"})
                        return
                    if row is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "提议不存在"})
                        return
                    if row.get("kind") == "example" and row.get("status") == "accepted":
                        owner.restart_required = True
                    self._json(HTTPStatus.OK, {"ok": True, "data": row})
                    return
                if path == "/api/review/decision":
                    self._review_write(
                        lambda term, scope, body: owner.review.set_decision(
                            term, str(body.get("action", "")), scope=scope
                        ),
                        body,
                    )
                    return
                if path == "/api/review/meaning":
                    meaning = body.get("meaning")
                    self._review_write(
                        lambda term, scope, body: owner.review.set_meaning(
                            term, None if meaning is None else str(meaning), scope=scope
                        ),
                        body,
                    )
                    return
                if path == "/api/review/forget":
                    self._review_write(
                        lambda term, scope, body: owner.review.forget(term, scope=scope),
                        body,
                    )
                    return
                manager = owner.schedule_management
                if path == "/api/schedules/jobs":
                    if manager is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"})
                        return
                    try:
                        item = manager.create(body)
                    except (ValueError, OSError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.CREATED, {"ok": True, "data": item})
                    return
                if path.startswith("/api/schedules/jobs/") and path.endswith("/run"):
                    if manager is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"})
                        return
                    try:
                        job_id = int(path.split("/")[-2])
                        run_id = manager.run_now(job_id)
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "任务 ID 无效"})
                        return
                    except KeyError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                        return
                    except RuntimeError as error:
                        self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                        return
                    self._json(HTTPStatus.ACCEPTED, {"ok": True, "run_id": run_id})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_PUT(self) -> None:
                path = urlsplit(self.path).path
                if path.startswith("/api/schedules/jobs/"):
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                        return
                    manager = owner.schedule_management
                    if manager is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"})
                        return
                    key = unquote(path.rsplit("/", 1)[1])
                    try:
                        body = self._body()
                        item = manager.update(key, body)
                    except KeyError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                        return
                    except (ValueError, OSError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"ok": True, "data": item})
                    return
                if path != "/api/config":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                try:
                    body = self._body()
                    changes = body.get("changes")
                    if not isinstance(changes, dict):
                        raise ValueError("changes must be an object")
                    updated = owner.store.update(changes)
                except (ValueError, OSError) as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                owner.restart_required = owner.restart_required or bool(updated)
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "updated": updated,
                    "restart_required": owner.restart_required,
                })

            def do_DELETE(self) -> None:
                path = urlsplit(self.path).path
                if not path.startswith("/api/schedules/jobs/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                manager = owner.schedule_management
                if manager is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"})
                    return
                key = unquote(path.rsplit("/", 1)[1])
                try:
                    manager.delete(key)
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                    return
                except OSError as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                    return
                self._json(HTTPStatus.OK, {"ok": True})

        return Handler
