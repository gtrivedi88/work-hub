"""Authenticated dashboard on the loopback interface."""

from __future__ import annotations

import hmac
import json
import secrets
import socketserver
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import agenda, service, sources, views
from .store import COLORS, KINDS, SOURCES, STATES, Store, TrackerError

ASSETS = Path(__file__).resolve().parents[1] / "dashboard"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer does a reverse DNS lookup here. The fixed loopback address
        # already provides the only host this dashboard will accept.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address
        self.host = f"{self.server_name}:{self.server_port}"


def dashboard_server(store: Store, port: int = 0):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, data: bytes, content_type: str, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def json(self, data, status=200):
            self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json", status)

        def authorized(self):
            supplied = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(supplied, f"Bearer {token}".encode())

        def do_GET(self):
            self.handle_dashboard()

        def do_POST(self):
            self.handle_dashboard()

        def handle_dashboard(self):
            if self.headers.get("Host") != self.server.host:
                self.json({"error": "Use the loopback dashboard URL."}, 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.server.host:
                self.json({"error": "Invalid dashboard origin."}, 403)
                return
            route = urlsplit(self.path)
            try:
                if self.command == "GET" and route.path in STATIC:
                    filename, mime = STATIC[route.path]
                    self.send((ASSETS / filename).read_bytes(), mime)
                    return
                if not self.authorized():
                    self.json({"error": "Open the URL printed by the dashboard command."}, 401)
                    return
                if self.command == "GET":
                    parameters = {key: value[-1] for key, value in parse_qs(route.query).items()}
                    result = self.read_data(route.path, parameters)
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get_content_type() != "application/json" or not 0 < length <= 65536:
                        raise TrackerError("Send a JSON request of up to 64 KiB.")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise TrackerError("Send a JSON object.")
                    result = self.write_data(route.path, payload)
                if result is None:
                    self.json({"error": "Unknown dashboard route."}, 404)
                else:
                    self.json(result)
            except (TrackerError, KeyError, TypeError, ValueError, sqlite3.Error, OSError) as exc:
                self.json({"error": str(exc)}, 400)

        def read_data(self, path, params):
            if path == "/api/config":
                return {"workspace": store.workspace, "colors": COLORS, "kinds": KINDS,
                        "states": STATES, "sources": SOURCES,
                        "watch_types": sources.WATCH_TYPES,
                        "views": {**views.BUILTIN, **store.settings("view")}}
            if path == "/api/items":
                filters = {key: value for key, value in params.items() if key != "view"}
                for key in ("waiting", "snoozed"):
                    if key in filters:
                        filters[key] = filters[key] == "true"
                return views.query(store, params.get("view"), **filters)
            if path == "/api/history":
                return store.history(params["id"])
            if path == "/api/agenda":
                return agenda.agenda(store)
            if path == "/api/review":
                return views.review(store)
            if path == "/api/notifications":
                return store.notifications()
            if path == "/api/service":
                return service.manage(store, "status")
            return None

        def write_data(self, path, data):
            if path == "/api/items":
                allowed = {"title", "description", "kind", "due", "next_action", "color",
                           "repeat", "remind", "scheduled"}
                if set(data) - allowed:
                    raise TrackerError("Unsupported capture field.")
                return store.add(**data)
            if path == "/api/edit":
                item_id = data.pop("id")
                return store.edit(item_id, **data)
            if path == "/api/status":
                return store.set_status(data["id"], data["status"])
            if path == "/api/dependencies":
                action = data["action"]
                if action not in ("add", "remove"):
                    raise TrackerError("Choose whether to add or remove the dependency.")
                operation = store.depend if action == "add" else store.undepend
                operation(data["id"], data["target"])
                return {"action": action}
            if path == "/api/views":
                if data.get("action") == "remove":
                    if data["name"] in views.BUILTIN:
                        raise TrackerError("Built-in views cannot be removed.")
                    store.remove_setting("view", data["name"])
                else:
                    views.save_view(store, data["name"], data["filters"])
                return {"name": data["name"]}
            if path == "/api/watches":
                if data.get("action") == "add":
                    return sources.add_watch(
                        store, data["title"], data["target"], kind=data["kind"],
                        contains=data.get("contains"), every=data["every"],
                    )
                if store.history(data["id"])["item"]["kind"] != "watch":
                    raise TrackerError("Select a watch.")
                if data["action"] == "check":
                    return {"checked": sources.poll(store, force=True, watch_id=data["id"])}
                if data["action"] in ("pause", "resume"):
                    return store.set_status(data["id"], "waiting" if data["action"] == "pause" else "todo")
                raise TrackerError("Choose a watch action.")
            if path == "/api/notifications":
                sequence = data["sequence"]
                if type(sequence) is not int:
                    raise TrackerError("Select a notification.")
                store.dismiss(sequence)
                return {"dismissed": sequence}
            return None

    server = LoopbackServer(("127.0.0.1", port), Handler)
    url = f"http://{server.host}/#{token}"
    return server, url


def serve(store: Store, port: int, *, open_browser: bool = True):
    if not 0 <= port <= 65535:
        raise TrackerError("Use a valid local port.")
    server, url = dashboard_server(store, port)
    with server:
        # The token is returned only to the process that started the dashboard.
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
