from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from prime_harness.omp import OmpAgentProvider, OmpRpcError
from prime_harness.services import HarnessService
from prime_harness.stores import SqliteStore


@dataclass(slots=True)
class _VerificationStage:
    """Tracks verification progress for diagnostics."""
    name: str
    description: str


_STAGES = {
    "init": _VerificationStage("init", "Initialize temporary workspace and services"),
    "lane_create": _VerificationStage("lane_create", "Create verification lane"),
    "connection": _VerificationStage("connection", "Create connection with permissions"),
    "provider_register": _VerificationStage("provider_register", "Register OMP agent provider"),
    "session_start": _VerificationStage("session_start", "Start initial OMP session"),
    "first_instruction": _VerificationStage("first_instruction", "Send first file creation instruction"),
    "first_file_verify": _VerificationStage("first_file_verify", "Verify first file created correctly"),
    "checkpoint_review": _VerificationStage("checkpoint_review", "Create review checkpoint and thread"),
    "reviewer_feedback": _VerificationStage("reviewer_feedback", "Conduct checkpoint review"),
    "correction_instruction": _VerificationStage("correction_instruction", "Send correction instruction"),
    "correction_file_verify": _VerificationStage("correction_file_verify", "Verify correction file created"),
    "session_stop": _VerificationStage("session_stop", "Stop original OMP session"),
    "service_restart": _VerificationStage("service_restart", "Restart harness service for restoration test"),
    "session_restore": _VerificationStage("session_restore", "Restore OMP session from checkpoint"),
    "session_identity_verify": _VerificationStage("session_identity_verify", "Verify restored session identity preserved"),
    "continuation_instruction": _VerificationStage("continuation_instruction", "Send continuation instruction"),
    "restored_process_stop": _VerificationStage("restored_process_stop", "Stop restored OMP process"),
}


def _failure_summary(result: object) -> str:
    """Describe categorical failure evidence without printing agent output."""
    evidence = getattr(result, "evidence", None)
    diagnostics = evidence.get("diagnostics", {}) if isinstance(evidence, dict) else {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    fields = ("outcome_status", "agent_invoked", "session_settled", "tools_invoked", "tool_failures", "approval_blocked")
    safe = {key: diagnostics[key] for key in fields if key in diagnostics}
    status = getattr(result, "status", "unknown")
    if status not in {"completed", "failed", "aborted", "approval_required"}:
        status = "unknown"
    return f"status={status} diagnostics={safe}"


def verify(executable: str) -> None:
    """Run verification with detailed stage tracking and error reporting.
    
    Args:
        executable: Path to OMP executable
        
    Raises:
        OmpRpcError: With detailed stage and error context
    """
    current_stage: str | None = None

    try:
        current_stage = "init"
        with TemporaryDirectory(prefix="prime-harness-omp-check-") as temporary:
            root = Path(temporary)
            workspace_root = root / "workspaces"
            workspace_root.mkdir()
            database = root / "state.sqlite3"
            service = HarnessService(
                store=SqliteStore(database),
                workspace_root=workspace_root,
                default_agent_provider="omp",
                omp_executable=executable,
            )

            current_stage = "lane_create"
            lane = service.create_lane(
                "OMP Runtime Verification", "omp-runtime-check", Path("lane")
            )

            current_stage = "connection"
            connection_id = service.create_connection(
                lane.lane_id,
                "runtime-verification",
                {"read", "review", "send_instruction", "continue", "stop"},
            )

            current_stage = "provider_register"
            provider = OmpAgentProvider(
                lane.agent_id, lane.workspace_path, executable=executable
            )
            service.register_agent_provider(lane.lane_id, provider)

            current_stage = "session_start"
            session = service.start_agent_session(lane.lane_id)

            restored_provider: OmpAgentProvider | None = None
            try:
                current_stage = "first_instruction"
                first = service.send_instruction(
                    lane.lane_id,
                    session.session_id,
                    connection_id,
                    (
                        "Create omp-runtime-check.txt containing exactly "
                        "'phase-2a-runtime-check'. Do not run commands."
                    ),
                )
                if not first.success:
                    raise OmpRpcError("Initial OMP instruction did not complete; " + _failure_summary(first))

                current_stage = "first_file_verify"
                first_file = lane.workspace_path / "omp-runtime-check.txt"
                if not first_file.is_file() or first_file.read_text(encoding="utf-8") != (
                    "phase-2a-runtime-check"
                ):
                    raise OmpRpcError("OMP did not create the expected first verification file")

                current_stage = "checkpoint_review"
                checkpoint = service.store.list_checkpoints(lane.lane_id)[-1]
                thread = service.create_review_thread_authenticated(
                    connection_id, lane.lane_id
                )

                current_stage = "reviewer_feedback"
                review = service.review_checkpoint_authenticated(
                    connection_id,
                    lane.lane_id,
                    thread.thread_id,
                    checkpoint.checkpoint_id,
                )
                if review.status != "rejected" or not review.requested_changes:
                    raise OmpRpcError("Reviewer did not produce the expected correction")

                current_stage = "correction_instruction"
                correction = service.continue_agent(
                    lane.lane_id,
                    session.session_id,
                    connection_id,
                    (
                        f"{review.requested_changes[0]}\n"
                        "Also create omp-correction-check.txt containing exactly "
                        "'phase-2a-correction-check'."
                    ),
                )
                if not correction.success:
                    raise OmpRpcError("OMP correction did not complete; " + _failure_summary(correction))

                current_stage = "correction_file_verify"
                correction_file = lane.workspace_path / "omp-correction-check.txt"
                if not correction_file.is_file() or correction_file.read_text(
                    encoding="utf-8"
                ) != ("phase-2a-correction-check"):
                    raise OmpRpcError("OMP did not create the expected correction file")

                second_checkpoint = service.store.list_checkpoints(lane.lane_id)[-1]
                if second_checkpoint.session_id != session.session_id:
                    raise OmpRpcError("Correction did not use the original OMP session")

                current_stage = "session_stop"
                if not provider.stop_session(session):
                    raise OmpRpcError("Original OMP process could not be stopped")

                current_stage = "service_restart"
                restarted = HarnessService(
                    store=SqliteStore(database),
                    workspace_root=workspace_root,
                    default_agent_provider="omp",
                    omp_executable=executable,
                )

                current_stage = "session_restore"
                restored_provider = OmpAgentProvider(
                    lane.agent_id, lane.workspace_path, executable=executable
                )
                restarted.register_agent_provider(lane.lane_id, restored_provider)
                restored = restarted.restore_agent_session(lane.lane_id, session.session_id)

                current_stage = "session_identity_verify"
                if restored.provider_session_id != session.provider_session_id:
                    raise OmpRpcError("Restored OMP session identity changed")

                current_stage = "continuation_instruction"
                continuation = restarted.continue_agent(
                    lane.lane_id,
                    session.session_id,
                    connection_id,
                    "Confirm the correction is present and continue without further edits.",
                )
                if not continuation.success:
                    raise OmpRpcError("Restored OMP session could not continue; " + _failure_summary(continuation))

                current_stage = "restored_process_stop"
                if not restored_provider.stop_session(restored):
                    raise OmpRpcError("Restored OMP process could not be stopped")

            except Exception:
                for active_provider in (provider, restored_provider):
                    if active_provider is not None:
                        active_process = active_provider._processes.get(session.session_id)
                        if active_process is not None:
                            active_process.close()
                raise

    except Exception as exc:
        stage_obj = _STAGES.get(current_stage)
        stage_info = (
            f"{stage_obj.name}:{stage_obj.description}"
            if stage_obj
            else str(current_stage or "unknown")
        )
        error_msg = str(exc) if isinstance(exc, OmpRpcError) else type(exc).__name__
        print(
            f"REAL_OMP_VERIFICATION=FAIL stage={stage_info} error={error_msg}",
            file=sys.stderr,
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real, temporary OMP RPC correction and restore check."
    )
    parser.add_argument("executable", nargs="?", default="omp")
    args = parser.parse_args()
    try:
        verify(args.executable)
    except Exception:
        return 1
    print("REAL_OMP_VERIFICATION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
