"""Local, session-scoped interactive review of a published report."""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from silverloupe.contracts import SelectionOverride
from silverloupe.report import build_report
from silverloupe.workflow import apply_overrides, load_run_state


class ReviewServer(HTTPServer):
    def __init__(self, output: Path) -> None:
        self.output = output.expanduser().resolve()
        self.report = build_report(load_run_state(self.output))
        self.review_path = "/" + secrets.token_urlsafe(24) + "/"
        super().__init__(("127.0.0.1", 0), ReviewHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}{self.review_path}report.html"

    def replace(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"original", "alternate", "scope", "revision"}:
            raise ValueError("Invalid replacement request")
        original, alternate, scope, revision = (
            payload["original"], payload["alternate"], payload["scope"], payload["revision"]
        )
        if (not isinstance(original, str) or not isinstance(alternate, str)
                or original == alternate or scope not in ("top", "selects")
                or type(revision) is not int):
            raise ValueError("Invalid replacement request")
        state = load_run_state(self.output)
        if state.status != "completed" or state.revision != revision:
            raise ValueError("The report is outdated or incomplete; reload it")
        selection = json.loads((self.output / "selection.json").read_text())
        selected = selection["top_ids" if scope == "top" else "selects_ids"]
        if original not in selected or alternate in selected:
            raise ValueError("The original must be selected and the alternative must not already be selected")
        decisions = [json.loads(line) for line in (self.output / "decisions.jsonl").read_text().splitlines() if line]
        decision = next((entry for entry in decisions if entry.get("item_id") == original), None)
        if not decision or alternate not in decision.get("provenance", {}).get("alternative_ids", []):
            raise ValueError("The requested photo is not an alternative")
        reason = "User chose an alternative in the review report"
        overrides = [
            SelectionOverride(item_id=original, revision=revision, selection_scope=scope,
                              action="exclude", reason=reason),
            SelectionOverride(item_id=alternate, revision=revision, selection_scope=scope,
                              action="include", reason=reason),
        ]
        state = apply_overrides(self.output, overrides)
        build_report(state)


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _response(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == self.server.review_path + "report.html":
            path, content_type = self.server.report, "text/html; charset=utf-8"
        elif self.path == self.server.review_path + ".silverloupe-report/previews.css":
            path = self.server.report.parent / ".silverloupe-report/previews.css"
            content_type = "text/css; charset=utf-8"
        else:
            self._response(404, "text/plain", b"Not found")
            return
        self._response(200, content_type, path.read_bytes())

    def do_POST(self) -> None:
        host = f"127.0.0.1:{self.server.server_port}"
        if (self.path != self.server.review_path + "replace"
                or self.headers.get("Host") != host
                or self.headers.get("Origin") != f"http://{host}"
                or self.headers.get_content_type() != "application/json"):
            self._response(403, "text/plain", b"Forbidden")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            self.server.replace(payload)
        except (ValueError, OSError, KeyError) as error:
            self._response(400, "application/json", json.dumps({"error": str(error)}).encode())
            return
        self._response(200, "application/json", b'{"ok":true}')


def create_review_server(output: Path) -> ReviewServer:
    return ReviewServer(output)
