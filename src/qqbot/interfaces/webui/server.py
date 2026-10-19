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

from qqbot.interfaces.webui.dashboard import DashboardQuery
from qqbot.interfaces.webui.database_query import DatabaseConsoleQuery
from qqbot.interfaces.webui.config import WebUIConfig
from qqbot.interfaces.webui.envfile import EnvConfigStore
from qqbot.interfaces.webui.experiments import ExperimentConsoleQuery
from qqbot.interfaces.webui.review_queue import ReviewFileEditor, ReviewFormatError
from qqbot.interfaces.webui.schema import SECTIONS, SETTINGS
from qqbot.interfaces.webui.memory_query import MemoryConsoleQuery
from qqbot.interfaces.webui.skill_query import SkillConsoleQuery
from qqbot.interfaces.webui.skill_admin import SkillAdmin
from qqbot.interfaces.webui.persona_review import PersonaQueueQuery
from qqbot.interfaces.webui.monitor_query import MonitorQuery
from qqbot.interfaces.webui.route_query import RouteConsoleQuery
from qqbot.interfaces.webui.tools_query import ToolConsoleQuery
from qqbot.interfaces.webui import logbuffer

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
        self.dashboard = None
        self.database_query = None
        self.skills_query = None
        self.tools_query = None
        # Editing a skill is a file write, not a runtime call: the board works
        # while the bot is down, and only gains the allow-list and live reload
        # once the catalogue exists.
        self.skill_admin = SkillAdmin(Path(config.env_path).resolve().parent / "skills")
        # The routing board edits the same env file the process reads, so it is
        # available before the runtime is composed: an operator has to be able
        # to fix a bad model or graph path while the bot is down.
        self.routes = RouteConsoleQuery(self.store, Path(config.env_path).resolve().parent)
        # A baseline monitor exists before the runtime is composed: the board
        # then reports "unknown" sources instead of refusing to load while the
        # bot is still starting.
        values = self.store.read()
        model_name = str(values.get("BOT_MODEL_NAME", {}).get("value") or "")
        model_base = str(values.get("BOT_MODEL_BASE_URL", {}).get("value") or "")
        self.logs = logbuffer.install()
        self.monitor = MonitorQuery(
            started_at=self.started_at,
            model_configured=bool(model_name and model_base),
            model_name=model_name,
            logs=self.logs,
        )
        # Offline experiment reports. Unlike the projections above this needs no
        # composed runtime: it reads a JSON file an operator produced, so it is
        # available even on a console whose bot is not running.
        self.experiments = ExperimentConsoleQuery()
        # The one write surface outside the env editor. It rewrites a file a
        # deployer owns, and nothing else in the bot reads decisions from
        # anywhere else.
        self.review = ReviewFileEditor(config.review_path)

    def bind_runtime(self, service) -> None:
        """Bind the console's projections after the composition root exists."""
        traces = getattr(service, "trace_sink", None)
        values = self.store.read()
        model_name = str(values["BOT_MODEL_NAME"]["value"] or "") if "BOT_MODEL_NAME" in values else ""
        model_base = str(values["BOT_MODEL_BASE_URL"]["value"] or "") if "BOT_MODEL_BASE_URL" in values else ""
        self.memory_query = MemoryConsoleQuery(service)
        self.database_query = DatabaseConsoleQuery(getattr(service.conversations, "database", service.conversations.db))
        self.skills_query = SkillConsoleQuery(service.agent.skills)
        skills = service.agent.skills
        self.skill_admin = SkillAdmin(
            getattr(skills, "root", self.skill_admin.root),
            catalog=skills,
        )
        self.tools_query = ToolConsoleQuery(traces)
        self.monitor.traces = traces
        self.monitor.gateway = getattr(service, "gateway", None)
        self.monitor.model_configured = bool(model_name and model_base)
        self.monitor.model_name = model_name
        reader = getattr(service.agent, "wiki_reader", None)
        self.dashboard = DashboardQuery(traces, getattr(reader, "_store", None))
        self.persona_queue = PersonaQueueQuery(
            service.conversations.db, self.config.persona_path
        )

    def bind_schedule_management(self, management) -> None:
        self.schedule_management = management
        # The scheduler owns the run ledger the monitor reports on.
        self.monitor.scheduler = getattr(management, "scheduler", None)

    async def run(self) -> None:
        if self.schedule_management is not None:
            self.schedule_management.bind_loop(asyncio.get_running_loop())
        handler = self._handler()
        self._server = ThreadingHTTPServer(
            (self.config.host, self.config.port), handler
        )
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
            server_version = "KinnaBot-WebUI/1.0"

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
                return bool(supplied) and hmac.compare_digest(
                    supplied, owner.config.token
                )

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
                if path == "/components.js":
                    self._asset("components.js", "text/javascript; charset=utf-8")
                    return
                dashboard_assets = {
                    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                    "/advanced": ("advanced.html", "text/html; charset=utf-8"),
                    "/kinna-avatar.png": ("kinna-avatar.png", "image/png"),
                }
                if path in dashboard_assets:
                    self._asset(*dashboard_assets[path])
                    return
                if path == "/kinna.png":
                    self._asset("kinna.png", "image/png")
                    return
                if not path.startswith("/api/"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._authorized():
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                    return
                if path == "/api/skills":
                    if owner.skills_query is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Skill 服务尚未就绪"})
                        return
                    usage = {}
                    if owner.dashboard is not None and owner.dashboard.traces is not None:
                        usage = owner.dashboard.traces.overview(hours=168).get("skill_counts", {})
                    self._json(HTTPStatus.OK, {"data": owner.skills_query.catalog(usage)})
                elif path.startswith("/api/skills/admin"):
                    admin = owner.skill_admin
                    if admin is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Skill 服务尚未就绪"})
                        return
                    if owner.dashboard is not None and owner.dashboard.traces is not None:
                        admin.calls = owner.dashboard.traces.overview(hours=168).get("skill_counts", {})
                    if path == "/api/skills/admin":
                        self._json(HTTPStatus.OK, {"data": admin.overview()})
                        return
                    detail = admin.detail(unquote(path.rsplit("/", 1)[1]))
                    if detail is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Skill 不存在"})
                        return
                    self._json(HTTPStatus.OK, {"data": detail})
                elif path == "/api/wiki/overview":
                    if owner.dashboard is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Wiki 服务尚未就绪"})
                        return
                    try:
                        data = owner.dashboard.wiki_catalog(str(query.get("scope", [""])[0]).strip() or None, int(query.get("limit", ["200"])[0]))
                    except (ValueError, OverflowError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/wiki/pages/"):
                    if owner.dashboard is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Wiki 服务尚未就绪"})
                        return
                    data = owner.dashboard.wiki_page(unquote(path.rsplit("/", 1)[1]))
                    if data is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Wiki 页面不存在或未启用"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/database/"):
                    database = owner.database_query
                    if database is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "数据库服务尚未就绪"})
                        return
                    try:
                        if path == "/api/database/schema":
                            data = database.schema()
                        elif path.startswith("/api/database/tables/"):
                            table = unquote(path.rsplit("/", 1)[1])
                            data = database.rows(table, limit=int(query.get("limit", ["100"])[0]))
                        elif path == "/api/database/query":
                            # Query text is sent as URL-safe JSON only for GET previews; writes are impossible in this port.
                            data = database.execute(str(query.get("sql", [""])[0]), limit=int(query.get("limit", ["100"])[0]))
                        else:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                    except (ValueError, OverflowError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    except Exception:
                        log.exception("Database console query failed")
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "读取数据库失败"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/dashboard/"):
                    dashboard = owner.dashboard
                    if dashboard is None or dashboard.traces is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "运行数据未接入。请随 Bot 启动控制台。"})
                        return
                    try:
                        scope = str(query.get("scope", [""])[0]).strip() or None
                        hours = int(query.get("hours", ["24"])[0])
                        if not 1 <= hours <= 168:
                            raise ValueError("hours 必须在 1–168 之间")
                        if path == "/api/dashboard/overview":
                            data = dashboard.traces.overview(scope=scope, hours=hours)
                            data["runtime_bound"] = True
                            data["wiki_available"] = dashboard.wiki_store is not None
                        elif path == "/api/dashboard/traces":
                            cursor = query.get("before", [None])[0]
                            data = dashboard.traces.list(scope=scope, hours=hours,
                                limit=int(query.get("limit", ["50"])[0]),
                                before=int(cursor) if cursor else None)
                        elif path.startswith("/api/dashboard/traces/"):
                            data = dashboard.traces.detail(int(path.rsplit("/", 1)[1]))
                        elif path == "/api/dashboard/wiki":
                            data = dashboard.wiki_graph(scope, int(query.get("limit", ["200"])[0]))
                        elif path == "/api/dashboard/wiki-page":
                            data = dashboard.wiki_page(str(query.get("id", [""])[0]))
                        else:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                        if data is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "记录不存在"})
                            return
                    except (ValueError, OverflowError):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "筛选参数无效"})
                        return
                    except Exception:
                        log.exception("Dashboard query failed")
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "读取运行数据失败，请检查本地运行日志。"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/monitor/"):
                    monitor = owner.monitor
                    try:
                        if path == "/api/monitor/live":
                            data = monitor.live(int(query.get("minutes", ["15"])[0]))
                        elif path == "/api/monitor/series":
                            data = monitor.series(int(query.get("minutes", ["60"])[0]))
                        elif path == "/api/monitor/alerts":
                            data = monitor.alerts(int(query.get("minutes", ["15"])[0]))
                        elif path == "/api/monitor/logs":
                            data = monitor.tail(
                                int(query.get("limit", ["20"])[0]),
                                str(query.get("level", [""])[0]).strip() or None,
                                str(query.get("q", [""])[0]).strip() or None,
                                int(query.get("offset", ["0"])[0]),
                            )
                        elif path == "/api/monitor/turns":
                            data = monitor.turns(
                                int(query.get("limit", ["12"])[0]),
                                int(query.get("offset", ["0"])[0]),
                                str(query.get("scope", [""])[0]).strip() or None,
                            )
                        elif path == "/api/monitor/scopes":
                            data = monitor.scopes(int(query.get("hours", ["24"])[0]))
                        elif path.startswith("/api/monitor/traces/"):
                            data = monitor.trace(path.rsplit("/", 1)[1])
                            if data is None:
                                self._json(HTTPStatus.NOT_FOUND, {"error": "轨迹不存在或已过期"})
                                return
                        else:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                    except (ValueError, OverflowError):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "监控参数无效"})
                        return
                    except Exception:
                        log.exception("Monitor query failed")
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "读取监控数据失败"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path.startswith("/api/tools/"):
                    if owner.tools_query is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "工具数据未接入。请随 Bot 启动控制台。"},
                        )
                        return
                    try:
                        scope = str(query.get("scope", [""])[0]).strip() or None
                        hours = int(query.get("hours", ["24"])[0])
                        if path == "/api/tools/overview":
                            data = owner.tools_query.overview(scope, hours)
                        elif path == "/api/tools/calls":
                            data = owner.tools_query.calls(
                                scope,
                                hours,
                                str(query.get("tool", [""])[0]).strip() or None,
                                int(query.get("limit", ["15"])[0]),
                                int(query.get("offset", ["0"])[0]),
                            )
                        else:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                    except (ValueError, OverflowError):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "工具筛选参数无效"})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path in {"/api/routes/overview", "/api/routes/artifacts"}:
                    try:
                        if path == "/api/routes/overview":
                            data = owner.routes.overview()
                        else:
                            data = owner.routes.artifacts(int(query.get("limit", ["20"])[0]))
                    except (ValueError, OverflowError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
                elif path == "/api/config":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "sections": [
                                {
                                    "id": section,
                                    "label": label,
                                    "description": description,
                                }
                                for section, label, description in SECTIONS
                            ],
                            "settings": [setting.public() for setting in SETTINGS],
                            "values": owner.store.read(),
                        },
                    )
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
                    self._json(
                        HTTPStatus.OK,
                        {
                            "online": True,
                            "uptime_seconds": int(time.time() - owner.started_at),
                            "restart_required": owner.restart_required,
                            "extension_count": len(
                                [x for x in extensions.split(",") if x.strip()]
                            ),
                            "env_path": str(owner.config.env_path),
                        },
                    )
                elif path == "/api/experiments/behavior-retrieval":
                    # Read-only, and deliberately independent of the composed
                    # runtime: the report is a file, so the section still works
                    # on a console whose bot has not finished starting.
                    self._json(
                        HTTPStatus.OK,
                        {"data": owner.experiments.behavior_retrieval()},
                    )
                elif path.startswith("/api/memory/"):
                    if owner.memory_query is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "记忆服务尚未就绪"},
                        )
                        return
                    scope = str(query.get("scope", [""])[0]).strip() or None
                    limit_raw = str(query.get("limit", ["100"])[0])
                    try:
                        limit = max(1, min(200, int(limit_raw)))
                    except ValueError:
                        self._json(
                            HTTPStatus.BAD_REQUEST, {"error": "limit 必须是整数"}
                        )
                        return
                    dashboard = owner.memory_query
                    after = str(query.get("after", [""])[0])
                    if after:
                        try:
                            cursor = json.loads(after)
                            if (
                                not isinstance(cursor, list)
                                or len(cursor) != 2
                                or not all(isinstance(value, str) for value in cursor)
                                or len(after) > 500
                            ):
                                raise ValueError("cursor")
                        except ValueError:
                            self._json(
                                HTTPStatus.BAD_REQUEST, {"error": "分页游标无效"}
                            )
                            return
                    if path == "/api/memory/overview":
                        data = dashboard.overview(scope)
                    elif path == "/api/memory/items":
                        data = dashboard.memories(
                            scope,
                            status=str(query.get("status", [""])[0]) or None,
                            query=str(query.get("q", [""])[0]),
                            limit=limit,
                            after=after,
                        )
                    elif path.startswith("/api/memory/items/"):
                        try:
                            item_id = int(path.rsplit("/", 1)[1])
                        except ValueError:
                            self._json(
                                HTTPStatus.BAD_REQUEST, {"error": "记忆 ID 无效"}
                            )
                            return
                        data = dashboard.memory_detail(item_id)
                        if data is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "记忆不存在"})
                            return
                    elif path == "/api/memory/topics":
                        data = dashboard.topics(scope, limit, after=after)
                    elif path.startswith("/api/memory/topics/"):
                        try:
                            topic_id = int(path.rsplit("/", 1)[1])
                        except ValueError:
                            self._json(
                                HTTPStatus.BAD_REQUEST, {"error": "Topic ID 无效"}
                            )
                            return
                        data = dashboard.topic_detail(topic_id)
                        if data is None:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "Topic 不存在"})
                            return
                    elif path == "/api/memory/people":
                        data = dashboard.people_list(scope, limit, after=after)
                    elif path.startswith("/api/memory/people/"):
                        parts = path.split("/")
                        if len(parts) != 6:
                            self._json(
                                HTTPStatus.BAD_REQUEST, {"error": "人物路径无效"}
                            )
                            return
                        data = dashboard.person_detail(parts[4], parts[5])
                    elif path == "/api/memory/slang":
                        data = dashboard.slang(scope, limit, after=after)
                    elif path == "/api/memory/extractions":
                        data = dashboard.extractions(scope, limit)
                    elif path == "/api/memory/graph":
                        data = dashboard.graph(scope, limit)
                    elif path == "/api/memory/ontology-detail":
                        data = dashboard.ontology_detail(
                            scope, str(query.get("id", [""])[0])
                        )
                        if data is None:
                            self._json(
                                HTTPStatus.NOT_FOUND,
                                {"error": "当前群中没有这个图谱对象"},
                            )
                            return
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
                        self._json(
                            HTTPStatus.BAD_REQUEST, {"error": "limit 必须是整数"}
                        )
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
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "任务服务尚未就绪"},
                        )
                        return
                    try:
                        if path == "/api/schedules/overview":
                            data = manager.overview()
                        elif path == "/api/schedules/jobs":
                            data = manager.jobs()
                        elif path.startswith("/api/schedules/jobs/"):
                            try:
                                data = manager.job(unquote(path.rsplit("/", 1)[1]))
                            except KeyError:
                                self._json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
                                return
                        elif path == "/api/schedules/runs":
                            job_raw = str(query.get("job_id", [""])[0]).strip()
                            data = manager.runs(
                                int(job_raw) if job_raw else None,
                                int(query.get("limit", ["100"])[0]),
                            )
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
                if path == "/api/database/query":
                    database = owner.database_query
                    if database is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "数据库服务尚未就绪"})
                        return
                    try:
                        data = database.execute(str(body.get("sql", "")), limit=int(body.get("limit", 100)))
                    except (ValueError, OverflowError) as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    self._json(HTTPStatus.OK, {"data": data})
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
                        self._json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": f"写入人格示例失败：{error}"},
                        )
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
                        lambda term, scope, body: owner.review.forget(
                            term, scope=scope
                        ),
                        body,
                    )
                    return
                if path == "/api/routes/probe":
                    self._json(HTTPStatus.OK, {"data": owner.routes.probe()})
                    return
                if path == "/api/routes/probe-model":
                    self._json(HTTPStatus.OK, {"data": owner.routes.probe_model()})
                    return
                if path.startswith("/api/skills/admin"):
                    admin = owner.skill_admin
                    if admin is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Skill 服务尚未就绪"})
                        return
                    try:
                        if path == "/api/skills/admin":
                            self._json(HTTPStatus.CREATED, {"ok": True, "data": admin.create(body)})
                            return
                        if path == "/api/skills/admin/import":
                            result = admin.import_document(body.get("filename"), body.get("content"))
                            self._json(HTTPStatus.CREATED, {"ok": True, "data": result})
                            return
                        parts = [item for item in path.split("/") if item]
                        name = unquote(parts[3]) if len(parts) >= 4 else ""
                        if path.endswith("/enabled"):
                            result = admin.set_enabled(name, bool(body.get("enabled")))
                            self._json(HTTPStatus.OK, {"ok": True, "data": result})
                            return
                        if path.endswith("/archive"):
                            self._json(HTTPStatus.OK, {"ok": True, "data": admin.archive(name)})
                            return
                    except KeyError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Skill 不存在"})
                        return
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    except OSError as error:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"写入失败：{error}"})
                        return
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                manager = owner.schedule_management
                if path == "/api/schedules/jobs":
                    if manager is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "任务服务尚未就绪"},
                        )
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
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "任务服务尚未就绪"},
                        )
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
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "任务服务尚未就绪"},
                        )
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
                if path.startswith("/api/skills/admin/"):
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                        return
                    admin = owner.skill_admin
                    if admin is None:
                        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Skill 服务尚未就绪"})
                        return
                    name = unquote(path.rsplit("/", 1)[1])
                    try:
                        body = self._body()
                        result = admin.save(name, body)
                    except KeyError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "Skill 不存在"})
                        return
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    except OSError as error:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"写入失败：{error}"})
                        return
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                    return
                if path == "/api/routes":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "登录密钥无效"})
                        return
                    try:
                        body = self._body()
                        result = owner.routes.apply(body.get("changes"))
                    except ValueError as error:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                        return
                    except OSError as error:
                        self._json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": f"写入配置失败：{error}"},
                        )
                        return
                    owner.restart_required = owner.restart_required or result["restart_required"]
                    self._json(HTTPStatus.OK, {"ok": True, **result})
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
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "updated": updated,
                        "restart_required": owner.restart_required,
                    },
                )

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
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE, {"error": "任务服务尚未就绪"}
                    )
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
