from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from prime_harness.omp import OmpAgentProvider, OmpRpcError
from prime_harness.services import HarnessService
from prime_harness.stores import SqliteStore


def verify(executable: str) -> None:
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
        lane = service.create_lane("OMP Runtime Verification", "omp-runtime-check", Path("lane"))
        connection_id = service.create_connection(
            lane.lane_id,
            "runtime-verification",
            {"read", "review", "send_instruction", "continue", "stop"},
        )
        provider = OmpAgentProvider(lane.agent_id, lane.workspace_path, executable=executable)
        service.register_agent_provider(lane.lane_id, provider)
        session = service.start_agent_session(lane.lane_id)
        restored_provider: OmpAgentProvider | None = None
        try:
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
                raise OmpRpcError("Initial OMP instruction did not complete")
            first_file = lane.workspace_path / "omp-runtime-check.txt"
            if not first_file.is_file() or first_file.read_text(encoding="utf-8") != (
                "phase-2a-runtime-check"
            ):
                raise OmpRpcError("OMP did not create the expected first verification file")

            checkpoint = service.store.list_checkpoints(lane.lane_id)[-1]
            thread = service.create_review_thread_authenticated(connection_id, lane.lane_id)
            review = service.review_checkpoint_authenticated(
                connection_id,
                lane.lane_id,
                thread.thread_id,
                checkpoint.checkpoint_id,
            )
            if review.status != "rejected" or not review.requested_changes:
                raise OmpRpcError("Reviewer did not produce the expected correction")

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
                raise OmpRpcError("OMP correction did not complete")
            correction_file = lane.workspace_path / "omp-correction-check.txt"
            if not correction_file.is_file() or correction_file.read_text(encoding="utf-8") != (
                "phase-2a-correction-check"
            ):
                raise OmpRpcError("OMP did not create the expected correction file")
            second_checkpoint = service.store.list_checkpoints(lane.lane_id)[-1]
            if second_checkpoint.session_id != session.session_id:
                raise OmpRpcError("Correction did not use the original OMP session")

            if not provider.stop_session(session):
                raise OmpRpcError("Original OMP process could not be stopped")
            restarted = HarnessService(
                store=SqliteStore(database),
                workspace_root=workspace_root,
                default_agent_provider="omp",
                omp_executable=executable,
            )
            restored_provider = OmpAgentProvider(
                lane.agent_id, lane.workspace_path, executable=executable
            )
            restarted.register_agent_provider(lane.lane_id, restored_provider)
            restored = restarted.restore_agent_session(lane.lane_id, session.session_id)
            if restored.provider_session_id != session.provider_session_id:
                raise OmpRpcError("Restored OMP session identity changed")
            continuation = restarted.continue_agent(
                lane.lane_id,
                session.session_id,
                connection_id,
                "Confirm the correction is present and continue without further edits.",
            )
            if not continuation.success:
                raise OmpRpcError("Restored OMP session could not continue")
            if not restored_provider.stop_session(restored):
                raise OmpRpcError("Restored OMP process could not be stopped")
        except Exception:
            for active_provider in (provider, restored_provider):
                if active_provider is not None:
                    active_process = active_provider._processes.get(session.session_id)
                    if active_process is not None:
                        active_process.close()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real, temporary OMP RPC correction and restore check."
    )
    parser.add_argument("executable", nargs="?", default="omp")
    args = parser.parse_args()
    try:
        verify(args.executable)
    except Exception as exc:
        print(f"REAL_OMP_VERIFICATION=FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("REAL_OMP_VERIFICATION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
