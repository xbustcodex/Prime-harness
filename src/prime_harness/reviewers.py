from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from prime_harness.config import ReviewerConfig
from prime_harness.domain import Checkpoint, Review, ReviewerThread, new_id, now_utc


class ReviewerProvider(Protocol):
    def create_thread(self, lane_id: str) -> ReviewerThread: ...
    def review(self, checkpoint: Checkpoint, thread: ReviewerThread) -> Review: ...
    def rollover(self, checkpoint: Checkpoint, thread: ReviewerThread) -> ReviewerThread: ...


class DeterministicReviewerProvider:
    def __init__(self, config: ReviewerConfig) -> None:
        self.config = config

    def create_thread(self, lane_id: str) -> ReviewerThread:
        return ReviewerThread(
            thread_id=new_id("thread"),
            lane_id=lane_id,
            reviewer_id=self.config.reviewer_id,
            provider=self.config.provider,
            model=self.config.model,
            current_context_digest="initial",
            current_checkpoint_id=None,
        )

    def review(self, checkpoint: Checkpoint, thread: ReviewerThread) -> Review:
        status = "accepted" if checkpoint.build_results == "passed" else "rejected"
        findings = [] if status == "accepted" else ["The checkpoint requires correction."]
        requested = (
            [] if status == "accepted" else ["Correct the reported failure and rerun the build."]
        )
        return Review(
            review_id=new_id("review"),
            lane_id=checkpoint.lane_id,
            reviewer_thread_id=thread.thread_id,
            checkpoint_id=checkpoint.checkpoint_id,
            status=status,
            verdict="pass" if status == "accepted" else "fail",
            findings=findings,
            requested_changes=requested,
            evidence_refs=[
                f"checkpoint:{checkpoint.checkpoint_id}",
                f"git:{checkpoint.git_status}",
            ],
            model=self.config.model,
            created_at=now_utc(),
            created_by=self.config.reviewer_id,
        )

    def rollover(self, checkpoint: Checkpoint, thread: ReviewerThread) -> ReviewerThread:
        return replace(
            self.create_thread(thread.lane_id),
            rollover_count=thread.rollover_count + 1,
            parent_thread_id=thread.thread_id,
            current_checkpoint_id=checkpoint.checkpoint_id,
            current_context_digest=(
                f"checkpoint={checkpoint.checkpoint_id}; revision={checkpoint.revision}"
            ),
            created_at=now_utc(),
            updated_at=now_utc(),
        )
