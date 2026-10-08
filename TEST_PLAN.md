# Phase 1 Test Plan

## Unit tests

- State machine accepts only allowed transitions.
- Checkpoint and review records are immutable after creation.
- Stable lane IDs are independent from secret IDs.
- Path validation rejects traversal and workspace escape.
- Permission checks deny cross-lane access.
- Approval policy allows safe actions and blocks dangerous actions.
- SecretService encrypts secrets before persistence, exposes metadata only through API reads, and releases plaintext only inside authorized provider operations.
- Lease acquisition is atomic under concurrent writers.

## Integration tests

- Create a lane and verify a second lane cannot read its files or events.
- Acquire two writer leases for one session; only one lease succeeds.
- Release an expired lease and verify ownership can change explicitly.
- A checkpoint containing A1 evidence cannot be queried by A2.
- A response from A2 cannot be delivered to A1's reviewer thread.
- Concurrent appends from two lanes remain independently ordered.
- Reviewer rollout preserves the source checkpoint and transcript.
- A failed external action is persisted with failed status and no secret leakage.
- An approval request can be approved and then executed exactly once.

## End-to-end test

1. Start the application against a temporary workspace.
2. Create lane A1 with a shell agent and a deterministic reviewer.
3. Send an instruction that creates a visible marker file.
4. Wait for the automatic checkpoint.
5. Review the checkpoint and return a correction.
6. Continue the agent and verify the correction.
7. Restart the web process and verify the state, history, and checkpoint remain.
8. Revoke the connection and verify all API calls fail.
9. Attempt a cross-project file read and verify denial.
10. Request a dangerous operation and verify it remains blocked without approval.

## Stress tests

- Concurrent checkpoints from multiple lanes must maintain independent reviewer contexts.
- Simultaneous lease requests must result in one winner.
- Long transcript history must not affect project lifetime; compaction must preserve evidence references.
- Run the same agent session after UI restart without changing its project or provider session ID.

## Security controls

- No secret tokens in logs, events, history, API responses, or test snapshots.
- Tracing is NOT APPLICABLE: no tracing subsystem exists in Phase 1. Any future tracing subsystem must pass the same secret-leak checks before it can be enabled.
- No arbitrary workspace path accepted from an API client.
- No raw control endpoint available without an authenticated scoped connection.
- No project context from lane A enters lane B.
- No successful external action is recorded without a real result.
