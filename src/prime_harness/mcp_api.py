"""Authenticated external API and MCP-compatible tool surface for the control room.

Endpoints (all require a Bearer pairing token unless noted):

* ``GET  /pair/{token}``            pairing page for the linked terminal (token in path)
* ``GET  /api/sessions``            session discovery
* ``GET  /api/sessions/{tid}``      status of one terminal
* ``GET  /api/sessions/{tid}/output``  selected output (rendered screen tail)
* ``POST /api/sessions/{tid}/commands``  command submission (per-terminal queue)
* ``GET  /api/approvals``           pending approvals
* ``POST /api/approvals/{id}/approve`` | ``/reject``   approval decisions
* ``GET  /api/events?since=&terminal=&wait=``  cursor-based event delivery
* ``POST /mcp``                     JSON-RPC 2.0: initialize / tools/list / tools/call

Scope rules: a token paired to terminal *N* may only read or command terminal
*N* (403 elsewhere). A supervisor-scoped token (terminal_id None) may list all
sessions, but every command still names one explicit terminal. Nothing here
merges terminal contexts.

Remote access (phone/laptop): bind host/port come from the environment
(``PRIME_HARNESS_API_HOST`` default 127.0.0.1, ``PRIME_HARNESS_API_PORT``
default 8420); pair links use ``PRIME_HARNESS_API_PUBLIC`` when set (e.g.
``http://192.168.1.20:8420``) so a phone on the LAN gets a usable URL. Every
API call requires a valid, unrevoked token.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from prime_harness.control_room import ControlRoom, PairingRecord

API_HOST_ENV = "PRIME_HARNESS_API_HOST"
API_PORT_ENV = "PRIME_HARNESS_API_PORT"
API_PUBLIC_ENV = "PRIME_HARNESS_API_PUBLIC"

CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prime Harness — __TERMINAL__</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 0; background: #141414; color: #ddd; }
 #out { white-space: pre-wrap; font-family: monospace; padding: 10px; font-size: 12px;
        height: 60vh; overflow-y: auto; background: #1e1e1e; border-bottom: 1px solid #333; }
 form { display: flex; padding: 8px; gap: 6px; }
 input { flex: 1; font-size: 14px; padding: 8px; }
 button { font-size: 14px; padding: 8px 14px; }
 #meta { padding: 6px 10px; font-size: 12px; color: #8a8; }
</style></head><body>
<div id="meta">__TERMINAL__ · <span id="status">connecting…</span>
 · events <span id="cursor">0</span></div>
<div id="out"></div>
<form id="f"><input id="cmd" placeholder="Command for this terminal only" autocomplete="off">
<button>Send</button></form>
<script>
const TOKEN = "__TOKEN__";
const TID = "__TERMINAL__";
const H = { "Authorization": "Bearer " + TOKEN };
const outEl = document.getElementById("out");
let cursor = 0;
function log(line) { outEl.textContent += line + "\\n"; outEl.scrollTop = outEl.scrollHeight; }
async function refreshOutput() {
  const r = await fetch(`/api/sessions/${TID}/output?lines=80`, { headers: H });
  if (!r.ok) { outEl.textContent = "output error " + r.status; return; }
  const d = await r.json();
  outEl.textContent = d.lines.join("\\n") + "\\n";
}
async function poll() {
  try {
    const r = await fetch(`/api/events?since=${cursor}&terminal=${TID}&wait=15`, { headers: H });
    if (r.ok) {
      const d = await r.json();
      for (const e of d.events) { log(`[${e.kind}] ${e.message}`); cursor = e.cursor; }
      document.getElementById("cursor").textContent = cursor;
      if (d.events.length) await refreshOutput();
    } else { document.getElementById("status").textContent = "auth error " + r.status; }
  } catch (e) { document.getElementById("status").textContent = "disconnected, retrying…"; }
  poll();
}
document.getElementById("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const el = document.getElementById("cmd");
  const text = el.value.trim(); if (!text) return;
  el.value = "";
  const r = await fetch(`/api/sessions/${TID}/commands`, {
    method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, H),
    body: JSON.stringify({ text }) });
  const d = await r.json();
  log(r.ok ? `> queued #${d.command_id}${d.needs_approval ? " (needs approval)" : ""}`
           : `> error ${r.status}: ${d.detail || ""}`);
});
(async () => {
  const s = await fetch(`/api/sessions/${TID}`, { headers: H });
  const st = document.getElementById("status");
  st.textContent = s.ok ? (await s.json()).status : "auth error " + s.status;
  await refreshOutput(); poll();
})();
</script></body></html>
"""


def api_settings() -> tuple[str, int, str]:
    """(host, port, public_base) for the control API, from the environment."""
    host = os.environ.get(API_HOST_ENV, "127.0.0.1")
    try:
        port = int(os.environ.get(API_PORT_ENV, "8420"))
    except ValueError:
        port = 8420
    public = os.environ.get(API_PUBLIC_ENV, f"http://{host}:{port}")
    return host, port, public


def _render_chat_page(token: str, terminal_id: str) -> str:
    """Pairing page. Tokens are URL-safe base64, so no HTML escaping is needed."""
    return CHAT_PAGE.replace("__TOKEN__", token).replace("__TERMINAL__", terminal_id)


def create_control_api(room: ControlRoom) -> FastAPI:
    """Build the FastAPI app for one ControlRoom (docs disabled, auth required)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(authorization: str | None) -> PairingRecord:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer pairing token required")
        record = room.pairings.authenticate(authorization.removeprefix("Bearer ").strip())
        if record is None:
            raise HTTPException(401, "Invalid or revoked pairing token")
        return record

    def require(record: PairingRecord, permission: str, terminal_id: str | None = None) -> None:
        if permission not in record.permissions:
            raise HTTPException(403, f"Permission '{permission}' not granted")
        if (
            terminal_id is not None
            and record.terminal_id is not None
            and record.terminal_id != terminal_id
        ):
            raise HTTPException(403, "Token is not paired to this terminal")

    def scoped_terminal(record: PairingRecord) -> list[str]:
        """Session ids this token may discover: its own, or all for supervisor scope."""
        if record.terminal_id is not None:
            return [record.terminal_id] if record.terminal_id in room.supervisor.sessions else []
        return sorted(room.supervisor.sessions)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (
            "<html><body><h1>Prime Harness control API</h1><p>Pairing required.</p></body></html>"
        )

    @app.get("/pair/{token}", response_class=HTMLResponse)
    def pairing_page(token: str) -> str:
        record = room.pairings.authenticate(token)
        if record is None:
            raise HTTPException(401, "Invalid or revoked pairing token")
        if "output" not in record.permissions or record.terminal_id is None:
            raise HTTPException(403, "This pairing cannot open a terminal chat page")
        return _render_chat_page(token, record.terminal_id)

    @app.get("/api/sessions")
    def sessions(authorization: str | None = Header(None)) -> dict:
        record = authenticate(authorization)
        require(record, "status")
        allowed = set(scoped_terminal(record))
        return {"sessions": [s for s in room.sessions() if s["terminal_id"] in allowed]}

    @app.get("/api/sessions/{terminal_id}")
    def session_status(terminal_id: str, authorization: str | None = Header(None)) -> dict:
        record = authenticate(authorization)
        require(record, "status", terminal_id)
        if terminal_id not in room.supervisor.sessions:
            raise HTTPException(404, "Unknown terminal")
        match = next(s for s in room.sessions() if s["terminal_id"] == terminal_id)
        return match

    @app.get("/api/sessions/{terminal_id}/output")
    def session_output(
        terminal_id: str, lines: int = 40, authorization: str | None = Header(None)
    ) -> dict:
        record = authenticate(authorization)
        require(record, "output", terminal_id)
        return {"terminal_id": terminal_id, "lines": room.output(terminal_id, lines)}

    @app.post("/api/sessions/{terminal_id}/commands", status_code=202)
    async def submit_command(
        terminal_id: str, request: Request, authorization: str | None = Header(None)
    ) -> dict:
        record = authenticate(authorization)
        require(record, "command", terminal_id)
        if terminal_id not in room.supervisor.sessions:
            raise HTTPException(404, "Unknown terminal")
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(422, "Missing 'text'")
        command = room.submit_command(terminal_id, text, actor=record.label or "external")
        return {
            "command_id": command.command_id,
            "status": command.status,
            "needs_approval": command.consequential,
        }

    @app.get("/api/approvals")
    def approvals(authorization: str | None = Header(None)) -> dict:
        record = authenticate(authorization)
        require(record, "approve")
        allowed = set(scoped_terminal(record))
        return {
            "approvals": [
                {
                    "command_id": c.command_id,
                    "terminal_id": c.session_id,
                    "text": c.text,
                    "created_at": c.created_at,
                }
                for c in room.supervisor.pending_approvals()
                if c.session_id in allowed
            ]
        }

    def _decision(action: str, command_id: int, record: PairingRecord) -> dict:
        command = room.supervisor.find(command_id)
        if command is None:
            raise HTTPException(404, "Unknown command")
        require(record, "approve", command.session_id)
        ok = (
            room.approve(command_id, actor=record.label or "external")
            if action == "approve"
            else room.reject(command_id, actor=record.label or "external")
        )
        if not ok:
            raise HTTPException(409, "Command is not awaiting approval")
        return {
            "command_id": command_id,
            "status": "approved" if action == "approve" else "rejected",
        }

    @app.post("/api/approvals/{command_id}/approve")
    def approve(command_id: int, authorization: str | None = Header(None)) -> dict:
        return _decision("approve", command_id, authenticate(authorization))

    @app.post("/api/approvals/{command_id}/reject")
    def reject(command_id: int, authorization: str | None = Header(None)) -> dict:
        return _decision("reject", command_id, authenticate(authorization))

    @app.get("/api/events")
    def events(
        since: int = 0,
        terminal: str | None = None,
        wait: float = 0.0,
        authorization: str | None = Header(None),
    ) -> dict:
        record = authenticate(authorization)
        if not record.permissions:
            raise HTTPException(403, "No permissions granted")
        if record.terminal_id is not None:
            if terminal is not None and terminal != record.terminal_id:
                raise HTTPException(403, "Token is not paired to this terminal")
            terminal = record.terminal_id
        fresh, latest = room.events(
            since=max(0, since), terminal_id=terminal, wait=max(0.0, min(wait, 30.0))
        )
        return {"events": [e.to_dict() for e in fresh], "cursor": latest}

    # ------------------------------------------------------------- MCP
    MCP_TOOLS: list[dict[str, Any]] = [
        {
            "name": "list_sessions",
            "description": "Discover terminals paired to this token, with status and queue depth.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_session_output",
            "description": "Selected rendered output (last N lines) for one paired terminal.",
            "inputSchema": {
                "type": "object",
                "properties": {"terminal_id": {"type": "string"}, "lines": {"type": "integer"}},
                "required": ["terminal_id"],
            },
        },
        {
            "name": "submit_command",
            "description": "Queue one command for one explicit terminal. Consequential commands "
            "require approval before delivery.",
            "inputSchema": {
                "type": "object",
                "properties": {"terminal_id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["terminal_id", "text"],
            },
        },
        {
            "name": "list_approvals",
            "description": "Commands awaiting approval for this token's terminal(s).",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "approve_command",
            "description": "Approve one awaiting command so it can be delivered.",
            "inputSchema": {
                "type": "object",
                "properties": {"command_id": {"type": "integer"}},
                "required": ["command_id"],
            },
        },
        {
            "name": "reject_command",
            "description": "Reject one awaiting command so it is never delivered.",
            "inputSchema": {
                "type": "object",
                "properties": {"command_id": {"type": "integer"}},
                "required": ["command_id"],
            },
        },
        {
            "name": "list_events",
            "description": "Cursor-based event stream: queueing, delivery, approvals, lifecycle.",
            "inputSchema": {
                "type": "object",
                "properties": {"since": {"type": "integer"}, "terminal_id": {"type": "string"}},
                "required": [],
            },
        },
    ]

    def mcp_call_tool(record: PairingRecord, name: str, arguments: dict) -> Any:
        terminal_id = arguments.get("terminal_id")
        if name == "list_sessions":
            require(record, "status")
            allowed = set(scoped_terminal(record))
            return [s for s in room.sessions() if s["terminal_id"] in allowed]
        if name == "get_session_output":
            require(record, "output", str(terminal_id))
            return room.output(str(terminal_id), int(arguments.get("lines", 40)))
        if name == "submit_command":
            require(record, "command", str(terminal_id))
            command = room.submit_command(
                str(terminal_id), str(arguments.get("text", "")), actor=record.label or "mcp"
            )
            return {
                "command_id": command.command_id,
                "status": command.status,
                "needs_approval": command.consequential,
            }
        if name == "list_approvals":
            require(record, "approve")
            allowed = set(scoped_terminal(record))
            return [
                {"command_id": c.command_id, "terminal_id": c.session_id, "text": c.text}
                for c in room.supervisor.pending_approvals()
                if c.session_id in allowed
            ]
        if name == "approve_command":
            approve_cmd = room.supervisor.find(int(arguments.get("command_id", -1)))
            if approve_cmd is None:
                raise HTTPException(404, "Unknown command")
            require(record, "approve", approve_cmd.session_id)
            if not room.approve(approve_cmd.command_id, actor=record.label or "mcp"):
                raise HTTPException(409, "Command is not awaiting approval")
            return {"approved": approve_cmd.command_id}
        if name == "reject_command":
            reject_cmd = room.supervisor.find(int(arguments.get("command_id", -1)))
            if reject_cmd is None:
                raise HTTPException(404, "Unknown command")
            require(record, "approve", reject_cmd.session_id)
            if not room.reject(reject_cmd.command_id, actor=record.label or "mcp"):
                raise HTTPException(409, "Command is not awaiting approval")
            return {"rejected": reject_cmd.command_id}
        if name == "list_events":
            if not record.permissions:
                raise HTTPException(403, "No permissions granted")
            scoped = str(terminal_id) if terminal_id else None
            if record.terminal_id is not None:
                scoped = record.terminal_id
            fresh, latest = room.events(since=int(arguments.get("since", 0)), terminal_id=scoped)
            return {"events": [e.to_dict() for e in fresh], "cursor": latest}
        raise HTTPException(400, f"Unknown tool: {name}")

    @app.post("/mcp")
    async def mcp(request: Request, authorization: str | None = Header(None)) -> JSONResponse:
        """JSON-RPC 2.0 tool surface (initialize / tools/list / tools/call)."""
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON") from None
        rpc_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "prime-harness-control", "version": "0.2.0"},
            }
        elif method == "tools/list":
            authenticate(authorization)
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            record = authenticate(authorization)
            try:
                tool_name = str(params.get("name", ""))
                tool_args = params.get("arguments") or {}
                value = mcp_call_tool(record, tool_name, tool_args)
            except HTTPException as exc:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": exc.status_code, "message": exc.detail},
                    }
                )
            result = {"content": [{"type": "text", "text": _json_text(value)}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    return app


def _json_text(value: Any) -> str:
    import json

    return json.dumps(value, indent=2)


class ApiServer:
    """Runs the control API in a daemon thread; started and stopped by the desktop app."""

    def __init__(self, room: ControlRoom, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.app = create_control_api(room)
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start serving in the background. Returns False if already running."""
        if self.running:
            return False
        import uvicorn

        # log_config=None: a windowed PyInstaller child may have sys.stderr=None,
        # and uvicorn's default log config fails to bind ext://sys.stderr there.
        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level="warning", log_config=None
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, name="prime-harness-api", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the server to exit and wait briefly. Returns True once stopped."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
            stopped = not self._thread.is_alive()
        else:
            stopped = True
        self._thread = None
        self._server = None
        return stopped
