# Prime Harness Architecture

## 1. Purpose and constraints

Prime Harness is a standalone control room for independently owned coding-agent workspaces. The application is not an agent orchestrator that merges model context. It is an audited state machine and authorization boundary around six lane-specific projects, sessions, reviewers, and credentials.

The system must preserve complete operational history even when model conversations are summarized or rolled over. Model conversation length is never the project lifetime.

## 2. Architecture principles

- **Lane isolation:** Every project, agent session, reviewer thread, transcript, checkpoint, event, credential, and policy decision is keyed by a stable `lane_id`.
- **Separate identity from secret:** Stable routing IDs are public to the harness. Credentials are encrypted records and are never used as identity.
- **Single-writer ownership:** At most one active writer may issue instructions to a controllable agent session. Read-only clients may coexist.
- **Explicit collaboration:** Cross-lane work requires a bounded, audited handoff operation. No implicit delegation exists.
- **Durable evidence:** Checkpoints, Git data, test results, selected files, decisions, reviews, and failed actions are persisted.
- **Least privilege:** Provider connections are scoped to one lane and permissions are checked at every action.
- **Approval before risk:** Destructive, credential, deployment, release, and spending actions require an approval policy.
- **No fabricated success:** Every external action records success, failure, timeout, or cancellation.

## 3. High-level components

- `api`: authenticated, authorized HTTP API and MCP-compatible tool surface.
- `domain`: lane, session, checkpoint, review, event, ownership, and policy rules.
- `persistence`: SQLite database for operational records and authenticated ciphertext.
- `agents`: provider-neutral coding-agent adapters.
- `reviewers`: provider-neutral reviewer adapters.
- `workflows`: autonomous checkpoint → review → continue/correction loop.
- `policy`: approval, escalation, and budget/operation rules.
- `ui`: responsive control-room web interface.
- `notifications`: local inbox and provider abstraction.

## 4. Domain model

### Lane

- `lane_id`: stable UUID used in routing and API paths.
- `name`, `project_id`, `workspace_path`, `agent_id`, `reviewer_id`.
- `agent_provider`, `reviewer_provider`, `model_config`.
- `state`, `owner_id`, `lease_token`, `lease_expires_at`.
- `permissions`: `read`, `review`, `send_instruction`, `continue`, `stop`, `dangerous_action`.
- `approval_policy`, `escalation_policy`, `created_at`, `updated_at`.

### Agent session

- `session_id`, `lane_id`, `agent_id`, `provider_session_id`.
- `working_directory`, `command`, `process_id` when applicable.
- `writer_lease_id`, `writer_lease_expires_at`, `lease_version`.
- `state`, `last_checkpoint_id`, `current_objective`, `termination_reason`.
- Durable session metadata; provider access token is stored separately as a secret record.

### Reviewer thread

- `reviewer_thread_id`, `lane_id`, `reviewer_id`, `provider_model`.
- `thread_rollover_id`, `conversation_id`, `summary_id`.
- `current_context_digest`, `last_review_id`, `status`.
- The thread is never shared with another lane.

### Checkpoint

- `checkpoint_id`, `lane_id`, `session_id`, `revision`.
- Objective, Git status, diff summary, changed files, build/test results, selected files, decisions, evidence references, token/cost metadata, created by, created at.
- Full operational history is separate and remains durable after compaction.

### Review

- `review_id`, `lane_id`, `reviewer_thread_id`, `checkpoint_id`.
- Status: accepted, rejected, needs_correction, failed, blocked, human_required.
- Structured verdict, findings, evidence refs, requested changes, reviewer model, output token usage, and audit metadata.

### Event

- `event_id`, `lane_id`, `event_type`, `severity`, `payload`, `occurred_at`, `actor_id`, `correlation_id`, `acknowledged_at`.
- Event payload never contains secrets.

### Handoff

- `handoff_id`, `from_lane_id`, `to_lane_id`, `status`, `scope`, `reason`, `approver_id`, `audit_hash`, `created_at`.
- Cross-lane collaboration is explicit and bounded; it cannot exceed the authorized scope.

## 5. State machine

| State | Meaning | Allowed transitions |
|---|---|---|
| IDLE | No active work | WORKING, PAUSED, FAILED, COMPLETED |
| WORKING | Coding agent is running | CHECKPOINT, PAUSED, FAILED, COMPLETED |
| CHECKPOINT | Agent reached a durable checkpoint | WAITING_FOR_REVIEW, CONTINUING, PAUSED, FAILED |
| WAITING_FOR_REVIEW | Reviewer has not yet evaluated the checkpoint | REVIEWING, CONTINUING, FAILED, HUMAN_REQUIRED |
| REVIEWING | Reviewer is evaluating evidence | WAITING_FOR_REVIEW, CONTINUING, FAILED, HUMAN_REQUIRED |
| CONTINUING | Agent is applying review feedback | WORKING, PAUSED, FAILED, COMPLETED |
| PAUSED | Work is intentionally suspended | WORKING, FAILED, COMPLETED |
| HUMAN_REQUIRED | Human approval or decision is required | WORKING, PAUSED, FAILED, COMPLETED |
| COMPLETED | Agent finished its objective | IDLE |
| FAILED | Agent or workflow failed | IDLE, PAUSED, HUMAN_REQUIRED |

Forbidden transitions are rejected by the domain service and audited.

## 6. Ownership and concurrency

Each session has one writer lease, acquired through a compare-and-swap operation. A writer may issue `send_instruction`, `continue_agent`, or `stop_agent` only when its lease is active. Readers can inspect without acquiring the lease.

A writer can release a lease on success, failure, stop, or expiry. Ownership changes are recorded as events and require a new explicit lease request. Handoffs are modeled as a new writer lease for the destination session and cannot reuse the source lease.

## 7. Provider contracts

### CodingAgentProvider

- `start_session(lane, config) -> session_handle`
- `send_instruction(session_handle, instruction, context)`
- `continue_session(session_handle, instruction)`
- `stop_session(session_handle)`
- `snapshot(session_handle) -> AgentSnapshot`
- `restore_session(lane, provider_session_id)`
- `release_session(session_handle)`

The shell adapter will implement this contract using a process with a terminal session and explicit command timeouts.

### ReviewerProvider

- `review(checkpoint, thread, policy) -> ReviewResult`
- `summarize(thread, checkpoint) -> Summary`
- `rollover(thread, checkpoint) -> ReviewerThread`

Reviewers receive only the lane's evidence and must not receive another lane's transcript or project context.

### NotificationProvider

- `deliver(event, recipient)`
- `subscribe(event_type, lane_id)`
- `acknowledge(event_id)`

The first implementation uses an in-process inbox. Mobile, email, and push providers are adapters over the same event contract.

## 8. Security model

- All HTTP endpoints require a validated API identity and lane authorization.
- No raw unauthenticated control endpoint is exposed.
- Project paths are canonicalized and restricted to a configured workspace root.
- File reads and writes are path-validated and lane-authorized server-side.
- Secret plaintext is encrypted with AES-256-GCM before persistence. A `SecretKeyProvider` supplies a base64-configured development key from the environment; key material is never stored in SQLite, and unavailable or invalid keys fail closed. An OS credential-store provider can replace the environment provider without changing service callers.
- Secret metadata reads return owner-scoped identity and metadata only. Authorized provider integrations use `SecretService.use_secret` as an internal callback boundary; no ordinary API read returns plaintext.
- Secret creation, update/rotation, provider use, deletion, and failed attempts are audited using IDs, purpose, actor, and status only. Plaintext is excluded from events, API responses, and service diagnostics.
- API clients receive opaque connection IDs and scoped short-lived credentials, never provider tokens.
- Secrets are retrievable only by the connection owner and are never included in logs or events.
- External content is tagged as untrusted and cannot directly authorize control actions.
- Dangerous operations require approval through a policy with an auditable approver.
- Command execution uses allow-listed commands and argument restrictions; shell metacharacters are not accepted as arbitrary command strings.
- Output is bounded by token, byte, line, and time limits.
- Every action records actor, authorization decision, resource ID, result, and correlation ID.

## 9. MCP surface

Tools are implemented as server-side methods that resolve the requested lane from an authenticated connection and verify permission before invoking domain services. The initial tool names are:

- `get_agent_state`
- `get_checkpoint`
- `get_transcript`
- `get_git_status`
- `get_diff`
- `get_test_results`
- `read_project_file`
- `send_instruction`
- `continue_agent`
- `stop_agent`
- `submit_review`
- `request_human`

No tool may accept a raw provider token or an arbitrary server-side workspace path.

## 10. UI and notification architecture

The desktop control room is a resizable grid with a persistent left sidebar, six lane panes, and a central reviewer workspace. The application uses a single server-rendered state model and WebSocket/SSE updates. Each pane has its own focus, maximize, restore, and viewport state.

The central Chat contains Overview and A1–A6 reviewer lanes. Each lane has its own thread and reviewer configuration. Notifications are injected directly into the central inbox, with a count, event history, severity, and deep-link to the exact lane.

## 11. Phased implementation

### Phase 1 tracing invariant

Tracing is **NOT APPLICABLE** in Phase 1; no tracing subsystem exists. Any future tracing
implementation must pass the same secret-leak checks used for SQLite, API responses,
events/history, logs, and provider failures before tracing can be enabled.

### TestClient dependency note

FastAPI's `fastapi.testclient` re-exports Starlette's `TestClient`. Starlette 1.7.0
`starlette/testclient.py` emits `StarletteDeprecationWarning` when its `httpx2` import
falls back to `httpx`, and recommends `httpx2`. The project keeps `httpx` for live
HTTP test requests and installs `httpx2` as a development dependency for Starlette's
in-process `TestClient`, compatible with the current FastAPI/Starlette runtime.

### Phase 1: Domain and vertical slice

- Define persisted domain models and migration strategy.
- Implement encrypted secret records and connection scopes.
- Implement lane/session ownership and authorization.
- Implement state transitions, events, checkpoints, reviews, and durable history.
- Implement shell/terminal coding-agent adapter.
- Implement deterministic reviewer adapter.
- Implement checkpoint → review → continue loop.
- Implement API and one-lane UI.
- Add isolation, restart, rollover, revocation, and approval tests.

### Phase 2: Six-lane control room

- Add six independent lane records and synchronized UI panes.
- Add responsive grid, maximize/restore behavior, and mobile layouts.
- Add concurrent scheduling and per-lane worker loops.
- Add six reviewer lanes and side-by-side evidence.

### Phase 3: External integrations

- Add OMP machine/RPC and ACP adapters.
- Add OpenCode and Pi adapters.
- Add OpenRouter/OpenAI/OpenAI-compatible reviewer providers.
- Add secured remote MCP and public documentation retrieval.

### Phase 4: Notifications and collaboration

- Add event inbox and notification providers.
- Add explicit bounded cross-lane handoff API and approval.
- Add human escalation policies and workflows.

### Phase 5: Production hardening

- Add operational logging, metrics, backups, deployment, audit review, and configuration management.
- Add process isolation, resource limits, and remote deployment.

## 12. Acceptance criteria

The Phase 1 acceptance suite must prove that:

1. A1 checkpoint cannot appear in A2 review context.
2. A2 review response cannot reach A1.
3. Two writers cannot own one agent session.
4. Concurrent checkpoints remain independently reviewable.
5. Restarting the UI preserves agent state and history.
6. Reviewer rollover preserves project history.
7. Revoked credentials fail.
8. Cross-project file reads are denied.
9. Dangerous operations honor approval policy.
10. Every external action is recorded as success, failure, timeout, cancellation, or blocked.

## 13. Assumptions

- The initial implementation runs on one host and uses SQLite.
- The local shell adapter is the first real coding agent and has no access outside its configured workspace except through explicit allow-listed tools.
- OMP, OpenCode, and Pi adapters will be introduced later with their official interfaces.
- Reviewer providers are considered untrusted executors and receive only the data authorized for the lane.
- Human approval is required for destructive or high-risk actions according to configurable policy.
- The first UI uses an authenticated local browser session; remote access requires an authenticated reverse proxy or equivalent deployment boundary.
