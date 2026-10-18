# External-chat connectivity

This document separates what Prime Harness **implements and tests** today from
what is **verified about ChatGPT integration options** (researched October 2026,
sources at the end). Nothing here claims a live ChatGPT conversation sync.

## Implemented and tested

### Pairing links and tokens

* `/pair [id]` in the supervisor chat issues one **revocable pairing link and
  token** for one terminal: `http://<public-base>/pair/<token>`.
* Only the SHA-256 hash of a token is stored; the token is shown once.
* A token is bound to exactly one terminal and to an explicit permission set
  (`status`, `output`, `command`, `approve`). A supervisor-scoped token
  (`/pair` without a terminal) may discover all sessions but every command
  still names one explicit terminal.
* `/revoke <id>` and `/revoke all` invalidate tokens immediately; revoked or
  unknown tokens get HTTP 401 on every endpoint.
* Opening the pairing link in a browser (phone or laptop) serves a small chat
  page for that terminal: live output tail, event stream, and a command box.
  Its context is that terminal only.

### Authenticated API and MCP surface

All endpoints require `Authorization: Bearer <token>`; scope is enforced per
token (cross-terminal access returns 403):

| Endpoint | Purpose |
|---|---|
| `GET /api/sessions` | session discovery (scoped to the token's terminal) |
| `GET /api/sessions/{id}` | status, pause state, queue depth |
| `GET /api/sessions/{id}/output?lines=N` | selected rendered output |
| `POST /api/sessions/{id}/commands` | command submission → per-terminal queue |
| `GET /api/approvals` | pending approvals |
| `POST /api/approvals/{id}/approve` \| `/reject` | approval decisions |
| `GET /api/events?since=&terminal=&wait=` | cursor-based event delivery |
| `POST /mcp` | JSON-RPC 2.0: `initialize`, `tools/list`, `tools/call` |

The `/mcp` tool surface exposes `list_sessions`, `get_session_output`,
`submit_command`, `list_approvals`, `approve_command`, `reject_command`, and
`list_events`, mirroring the REST API with the same per-token scope checks.

Commands from external clients go through the existing supervisor: per-terminal
queues, consequential-command approval gating, and pause/resume all apply
unchanged. Terminal contexts are never merged.

### Remote access (phone/laptop)

* The API is **off until started**: `/api start` in the supervisor chat (stopped
  with `/api stop`, status with `/api status`).
* Default bind is `127.0.0.1:8420`. To reach the app from a phone or laptop on
  the LAN, set before launch:
  * `PRIME_HARNESS_API_HOST=0.0.0.0`
  * `PRIME_HARNESS_API_PORT=8420` (optional)
  * `PRIME_HARNESS_API_PUBLIC=http://<your-lan-ip>:8420` (used in pairing links)
* Every call requires a valid, unrevoked token; the server documents no
  unauthenticated endpoint beyond the static index page.

### Clipboard

* `Ctrl+Shift+V` and `Shift+Insert` paste (bracketed when negotiated).
* `Ctrl+Shift+C` copies the visible screen.
* New: mouse-drag selection; `Ctrl+C` copies the selection when one is
  active and still sends SIGINT when there is not. A click (no drag) clears
  the selection; typing or sending keys clears it.

### Tests

`tests/test_control_room_api.py` covers pairing, revocation, cross-terminal
isolation (403), reconnection with event cursors, queueing, approval and
rejection flows, blocking event delivery, the MCP tool surface, the pairing
page, clipboard selection, and a live uvicorn server over TCP.

## ChatGPT integration: what is verified (not implemented)

Researched October 2026 (sources below):

1. **No general API exists to read or write a user's ChatGPT conversation
   threads.** OpenAI's "Conversations" objects belong to API-created
   conversations, not ChatGPT's own chat history. (Enterprise/Edu Compliance
   API is a workspace-scoped exception, not applicable here.)
2. **ChatGPT supports custom MCP connectors**, but the documented requirement
   is a **public HTTPS MCP endpoint** or OpenAI's **Secure MCP Tunnel** —
   ChatGPT does not connect directly to a local MCP server. A local stdio MCP
   route exists only for ChatGPT desktop plugins, limited to approved partners.
3. **Model APIs (Responses / Chat Completions) can act as a supervisor or
   reviewer**, but only with context the app supplies; they do not observe a
   user's live ChatGPT thread.

Implications for Prime Harness:

* The `/mcp` JSON-RPC tool surface is designed so that exposing it through a
  user-provided HTTPS tunnel would let a ChatGPT connector call the tools.
  **This path is untested here**: it needs the user's tunnel and ChatGPT plan
  configuration, neither of which is available for automated verification.
* Making one ChatGPT conversation the central supervisor/reviewer over the
  model APIs would require an OpenAI API key and an outbound integration. **Not
  implemented.**
* No claim is made that a ChatGPT conversation currently syncs with the
  supervisor chat. The in-app supervisor chat is the implemented coordination
  surface; external connectivity today is the authenticated API/MCP layer and
  the per-terminal pairing pages.

## Sources

* Conversation state (API conversations): https://developers.openai.com/api/docs/guides/conversation-state
* Retrieve a conversation (API): https://developers.openai.com/api/reference/resources/conversations/methods/retrieve/
* Connecting custom MCP servers in ChatGPT: https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
* MCP servers for the OpenAI API: https://developers.openai.com/api/docs/guides/tools-connectors-mcp
* Desktop-only plugin submissions (local stdio MCP, approved partners): https://developers.openai.com/plugins/deploy/desktop-only
* OpenAI API overview: https://developers.openai.com/api/reference/overview
