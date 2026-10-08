# Prime-harness

Prime Harness Phase 1 provides a lane-scoped control API, SQLite-backed operational
history, writer leases, a constrained shell file-operation adapter, and a deterministic
reviewer. The six-lane UI, external MCP integration, notification providers, and other
agent providers are not part of this phase.

## Run the local API

The server binds to `127.0.0.1` by default. Configure the database and permitted
workspace root before starting it:

```sh
export PRIME_HARNESS_DB="$PWD/.prime-harness.sqlite3"
export PRIME_HARNESS_WORKSPACE="$PWD/projects"
python -m prime_harness.server
```

Secret values are encrypted with AES-256-GCM before persistence. Set
`PRIME_HARNESS_SECRET_KEY` to the base64 encoding of a randomly generated 32-byte key
before creating or using secrets. Keep this value in a protected local environment
configuration; it is never written to SQLite. The service fails closed when the key
is absent, malformed, or cannot authenticate stored ciphertext. Development uses this
environment-backed key provider; an OS credential-store provider can replace it
without changing service callers.

Lanes and scoped connections are provisioned through `HarnessService`; the HTTP API
does not expose unauthenticated lane or credential creation. Issue a separate random
API bearer credential for a scoped connection through `HarnessService`. The bearer
value is returned once and only its hash is persisted; the connection ID is not a
credential. Every lane API operation checks its lane and permission. The interactive
API documentation endpoints are disabled. Secret HTTP reads expose owner-scoped
metadata only; plaintext is available only to authorized internal provider operations
through `SecretService`.

## Verify Phase 1

```sh
pytest -q
ruff check .
mypy src/prime_harness
```