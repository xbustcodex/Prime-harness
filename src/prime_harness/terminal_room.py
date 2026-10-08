"""Standalone localhost terminal control room with scoped per-slot bearer tokens."""
from __future__ import annotations

import asyncio
import queue
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from prime_harness.terminal_control import TerminalManager

manager = TerminalManager()


class Input(BaseModel):
    data: str = Field(max_length=8192)


def create_terminal_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def auth(slot_id: int, authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Terminal token required")
        if not manager.authorized(slot_id, authorization[7:]):
            raise HTTPException(403, "Invalid terminal token")

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        from pathlib import Path
        return (Path(__file__).parent / "terminal_room.html").read_text(encoding="utf-8")

    @app.get("/api/terminals")
    def terminals() -> list[dict]:
        return [{"slot": i, "running": manager.slot(i).alive} for i in manager.slots]

    @app.post("/api/terminals/{slot_id}/input")
    def input_terminal(slot_id: int, body: Input, authorization: str | None = Header(None)) -> dict:
        auth(slot_id, authorization)
        try:
            manager.write(slot_id, body.data)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"accepted": True}

    @app.get("/api/terminals/{slot_id}/output")
    def output_terminal(slot_id: int, authorization: str | None = Header(None)) -> dict:
        auth(slot_id, authorization)
        slot = manager.slot(slot_id)
        with slot.lock:
            return {"chunks": list(slot.recent)}

    @app.websocket("/ws/terminals/{slot_id}")
    async def stream(websocket: WebSocket, slot_id: int) -> None:
        # WebSocket tokens are supplied in a first message, not in URL/query logs.
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            token = first.get("token") if isinstance(first, dict) else None
            if not isinstance(token, str) or not manager.authorized(slot_id, token):
                await websocket.close(code=1008)
                return
            slot = manager.slot(slot_id)
            updates: queue.Queue[str] = queue.Queue(maxsize=512)
            with slot.lock:
                slot.subscribers.append(updates)
                previous = list(slot.recent)
            try:
                for chunk in previous:
                    await websocket.send_text(chunk)
                while True:
                    while not updates.empty():
                        await websocket.send_text(updates.get_nowait())
                    try:
                        incoming = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                        manager.write(slot_id, incoming)
                    except asyncio.TimeoutError:
                        pass
            finally:
                with slot.lock:
                    slot.subscribers.remove(updates)
        except (WebSocketDisconnect, asyncio.TimeoutError, KeyError, ValueError, RuntimeError):
            pass

    return app


app = create_terminal_app()


def main() -> None:
    import uvicorn
    # Launch fresh, isolated CMD processes; do not attach to existing agent builds.
    for slot_id in manager.slots:
        manager.launch(slot_id)
    # Local UI does not receive control tokens automatically.
    # Tokens are issued once in this startup console for explicit pairing.
    for slot_id in manager.slots:
        print(f"CMD {slot_id} pairing token: {manager.rotate_token(slot_id)}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8793)


if __name__ == "__main__":
    main()
