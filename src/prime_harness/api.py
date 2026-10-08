from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException

from prime_harness.domain import AgentSession
from prime_harness.services import HarnessService


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "items"):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def create_app(service: HarnessService) -> FastAPI:
    app = FastAPI(title="Prime Harness Phase 1 API", docs_url=None, redoc_url=None)

    def authenticate(lane_id: str, authorization: str | None, permission: str) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        credential = authorization.removeprefix("Bearer ").strip()
        connection_id = service.authenticate_credential(credential, permission, lane_id)
        if connection_id is None:
            raise HTTPException(status_code=403, detail="Connection is not authorized")
        return connection_id

    def ensure_provider(session: AgentSession) -> None:
        try:
            service._agent_provider(session.agent_id)
        except KeyError:
            provider = service.create_agent_provider(session.lane_id, session.workspace_path)
            if session.provider_session_id:
                provider.restore_session(session)
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Agent session cannot be restored",
                ) from None

    @app.get("/api/lanes/{lane_id}/state")
    def lane_state(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        authenticate(lane_id, authorization, "read")
        try:
            return service.get_state(lane_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown lane") from exc

    @app.post("/api/lanes/{lane_id}/sessions")
    def create_session(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "send_instruction")
        lane = service.store.get_lane(lane_id)
        if lane is None:
            raise HTTPException(status_code=404, detail="Unknown lane")
        service.create_agent_provider(lane_id, lane.workspace_path)
        session = service.start_agent_session(lane_id)
        service._event(
            lane_id,
            "agent_session_started",
            "info",
            {"session_id": session.session_id, "provider_session_id": session.provider_session_id},
            connection_id,
            session.session_id,
        )
        return _json_value(session)

    @app.post("/api/lanes/{lane_id}/agents/{session_id}/instructions")
    def instruction(
        lane_id: str,
        session_id: str,
        authorization: str | None = Header(default=None),
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "send_instruction")
        session = service.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise HTTPException(status_code=404, detail="Unknown session")
        ensure_provider(session)
        try:
            return _json_value(
                service.send_instruction(
                    lane_id, session_id, connection_id, str(body.get("instruction", ""))
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Control operation denied") from exc

    @app.post("/api/lanes/{lane_id}/agents/{session_id}/continue")
    def continue_agent(
        lane_id: str,
        session_id: str,
        authorization: str | None = Header(default=None),
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "continue")
        session = service.get_session(session_id)
        if session is None or session.lane_id != lane_id:
            raise HTTPException(status_code=404, detail="Unknown session")
        ensure_provider(session)
        try:
            return _json_value(
                service.continue_agent(
                    lane_id, session_id, connection_id, str(body.get("instruction", ""))
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Control operation denied") from exc

    @app.get("/api/lanes/{lane_id}/checkpoints")
    def checkpoints(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> list[dict[str, Any]]:
        authenticate(lane_id, authorization, "read")
        if service.store.get_lane(lane_id) is None:
            raise HTTPException(status_code=404, detail="Unknown lane")
        return _json_value(service.store.list_checkpoints(lane_id))

    @app.post("/api/lanes/{lane_id}/checkpoints")
    def create_checkpoint(
        lane_id: str,
        authorization: str | None = Header(default=None),
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "send_instruction")
        try:
            checkpoint = service.create_checkpoint(
                lane_id=lane_id,
                objective=str(body.get("objective", "")),
                git_status=str(body.get("git_status", "")),
                changed_files=body.get("changed_files", []),
                build_results=str(body.get("build_results", "not-run")),
                evidence=body.get("evidence", {}),
                session_id=str(body["session_id"]),
                created_by=connection_id,
            )
            return _json_value(checkpoint)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown lane or session") from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid checkpoint data") from exc

    @app.post("/api/lanes/{lane_id}/review-threads")
    def create_review_thread(
        lane_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "review")
        try:
            return _json_value(service.create_review_thread_authenticated(connection_id, lane_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown lane") from exc

    @app.get("/api/lanes/{lane_id}/checkpoints/{checkpoint_id}")
    def checkpoint(
        lane_id: str,
        checkpoint_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "read")
        try:
            return _json_value(
                service.read_checkpoint_authenticated(connection_id, lane_id, checkpoint_id)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown checkpoint") from exc

    @app.get("/api/lanes/{lane_id}/sessions/{session_id}")
    def session(
        lane_id: str,
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "read")
        try:
            return _json_value(
                service.read_session_authenticated(connection_id, lane_id, session_id)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown session") from exc

    @app.get("/api/lanes/{lane_id}/checkpoints/{checkpoint_id}/transcript")
    def transcript(
        lane_id: str,
        checkpoint_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "read")
        try:
            return service.read_transcript_authenticated(connection_id, lane_id, checkpoint_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown transcript") from exc

    @app.get("/api/lanes/{lane_id}/files/{relative_path:path}")
    def read_file(
        lane_id: str,
        relative_path: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        connection_id = authenticate(lane_id, authorization, "read")
        try:
            return {
                "content": service.read_file_authenticated(connection_id, lane_id, relative_path)
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown lane") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid or unavailable workspace path"
            ) from exc

    @app.get("/api/lanes/{lane_id}/events")
    def events(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> list[dict[str, Any]]:
        connection_id = authenticate(lane_id, authorization, "read")
        return _json_value(service.read_events_authenticated(connection_id, lane_id))

    @app.get("/api/lanes/{lane_id}/secrets")
    def secret_metadata(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> list[dict[str, Any]]:
        connection_id = authenticate(lane_id, authorization, "read")
        return _json_value(service.secret_service.list_secrets(connection_id, lane_id))

    @app.get("/api/lanes/{lane_id}/reviews")
    def reviews(
        lane_id: str, authorization: str | None = Header(default=None)
    ) -> list[dict[str, Any]]:
        connection_id = authenticate(lane_id, authorization, "read")
        return _json_value(service.read_reviews_authenticated(connection_id, lane_id))

    @app.post("/api/lanes/{lane_id}/review-threads/{thread_id}/checkpoints/{checkpoint_id}/review")
    def review_checkpoint(
        lane_id: str,
        thread_id: str,
        checkpoint_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        connection_id = authenticate(lane_id, authorization, "review")
        try:
            return _json_value(
                service.review_checkpoint_authenticated(
                    connection_id, lane_id, thread_id, checkpoint_id
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown reviewer context") from exc

    @app.post("/api/lanes/{lane_id}/dangerous-actions")
    def request_dangerous_action(
        lane_id: str,
        authorization: str | None = Header(default=None),
        body: dict[str, Any] = Body(...),
    ) -> dict[str, str]:
        connection_id = authenticate(lane_id, authorization, "dangerous_action")
        try:
            return service.request_dangerous_action(
                lane_id,
                connection_id,
                str(body.get("request_key", "")),
                str(body.get("action", "")),
                body.get("parameters", {}),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid dangerous-action request") from exc

    @app.post("/api/lanes/{lane_id}/dangerous-actions/{request_id}/approve")
    def approve_dangerous_action(
        lane_id: str,
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, bool]:
        connection_id = authenticate(lane_id, authorization, "dangerous_action")
        connection = service.store.get_connection(connection_id)
        if connection is None:
            raise HTTPException(status_code=403, detail="Connection is not authorized")
        approved = service.approve_dangerous_action(
            lane_id,
            connection_id,
            request_id,
            connection.owner_id,
        )
        return {"executed": approved}

    return app
