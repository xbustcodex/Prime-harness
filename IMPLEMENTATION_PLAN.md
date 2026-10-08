# Phase 1 Implementation Plan

## Goal

Build a standalone, authenticated, durable one-lane control-room slice. It must implement the domain model, event/state architecture, provider interfaces, ownership model, API contracts, persistent history, autonomous loop, and a responsive UI state that can later be generalized to six lanes.

## Work sequence

1. **Project scaffold**
   - Create Python package, dependency manifest, test configuration, and local development commands.
   - Add repository documentation and an architecture index.

2. **Domain and persistence**
   - Add UUID and typed domain models.
   - Create SQLite schema for lanes, sessions, checkpoints, reviews, events, secrets, leases, handoffs, and transcript records.
   - Add migrations through a deterministic schema initializer.

3. **Authorization and secrets**
   - Implement API identity and scoped connection records.
   - Add encrypted secret storage behind an `SecretStore` interface.
   - Enforce lane and permission checks in every service call.

4. **State and ownership services**
   - Implement the lane state machine and transition audit.
   - Implement atomic writer-lease acquisition/release.
   - Implement event publication and notification inbox.

5. **Provider adapters**
   - Implement `ShellAgentProvider` against a configured workspace.
   - Implement `DeterministicReviewerProvider` for end-to-end proof.
   - Add provider traits for `AgentSnapshot`, `Checkpoint`, and `ReviewResult`.

6. **Autonomous loop**
   - Add a durable worker that runs checkpoint → reviewer → correction/continue → checkpoint.
   - Add bounded retries, timeout, failure, pause, and human-required branches.
   - Ensure every loop step has a correlation ID and event.

7. **API and MCP**
   - Build authenticated REST endpoints for lane, agent, checkpoint, review, and control operations.
   - Build the MCP tool surface using the same domain services.
   - Add request/response validation and audit metadata.

8. **UI vertical slice**
   - Add professional single-lane control-room UI.
   - Add current objective, state, Git status, diff, tests, evidence, transcript, review, and notification inbox.
   - Add lane-specific actions and concurrency-safe state updates.

9. **Verification**
   - Run unit, service, integration, and end-to-end tests.
   - Prove restart and revocation behavior.
   - Run lint/type checks and inspect the final diff.

## API contract outline

- `POST /api/connections` — create a scoped connection.
- `GET /api/lanes/{lane_id}` — lane state and configuration.
- `POST /api/lanes/{lane_id}/agents/{agent_id}/instructions` — send an instruction under the writer lease.
- `POST /api/lanes/{lane_id}/agents/{agent_id}/continue` — continue after review.
- `POST /api/lanes/{lane_id}/agents/{agent_id}/stop` — stop the session.
- `POST /api/lanes/{lane_id}/checkpoints` — create a checkpoint.
- `POST /api/lanes/{lane_id}/reviews` — submit a reviewer result.
- `GET /api/lanes/{lane_id}/events` — inbox and history.
- `POST /api/lanes/{lane_id}/approvals` — approve a pending dangerous action.
- `POST /api/lanes/{lane_id}/handoffs` — explicit cross-lane operation, not an implicit delegation.

## Behavior boundaries

- The shell adapter operates only in its configured workspace and does not use arbitrary shell arguments.
- Reviewer input is a structured `CheckpointEvidence` object, not a transcript dump.
- The reviewer cannot set the lane state directly.
- The worker is a single orchestration service but each lane has independent state and locks.
- A reviewer response is not accepted as an instruction to the coding agent unless the workflow explicitly maps it to `continue`.

## Definition of done

- All Phase 1 tests pass.
- A real local agent session can complete a checkpoint/review/continue cycle.
- UI state survives a process restart.
- Secrets cannot be read from API responses or logs.
- Writer leases prevent concurrent instructions.
- Cross-lane retrieval is denied by API authorization.
- Approval policy blocks dangerous actions without approval.
