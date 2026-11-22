# Migration Audit

## Scope and canonical source

This repository is a Python Discord bot with a web dashboard, AI chat, economy/game systems, and SQLite persistence. The canonical application entrypoints are:

- `main.py` — bot bootstrap, environment validation, cog loading, Discord login, slash command sync.
- `keep_alive.py` — Flask/Waitress dashboard and health endpoints.
- `cogs/` — feature modules loaded by `main.py`.
- `kai_core/` — shared config, logging, security, pagination, parity, AI adapters, and economy logic.

The repo contains generated runtime artifacts (`.venv`, `__pycache__`, `.pytest_cache`, SQLite WAL/SHM, logs) that must not be migrated blindly. Only production source files under the project root should be migrated.

## Executive summary

The project is functionally rich and test-covered, but it is not yet a clean, modular enterprise architecture. The main concerns are:

1. Shared economy state is protected by multiple independent locks rather than a single process-wide lock.
2. Transactions are not consistently idempotent.
3. Rust AI plans, pending actions, durable scoped memories, schedules, mood
   preferences, and multimodal inputs now have Rust implementations. Automatic
   fact extraction/summary lifecycle and complete Python AI command parity are
   still outstanding.
4. Web endpoints expose public metadata and lack a centralized security policy.
5. Database schema management is ad hoc and spread across cogs.

These are the primary migration risks before moving any subsystem to Rust.

## Audit findings and severity

| ID | Area | Severity | Impact | Fix | Verification |
| --- | --- | --- | --- | --- | --- |
| MIG-01 | Economy / SQLite | Critical | Concurrent balance mutations can race | Centralize lock and transaction coordinator | Concurrent transfer tests; DB integrity checks |
| MIG-02 | Transaction layer | High | Duplicate user-visible payouts/retries | Add idempotency keys and unique constraints | Retry + duplicate submit tests |
| MIG-03 | AI extended state | High | Long-term AI context is not yet available in the Rust runtime | Port facts/preferences/summaries | Persistence and ownership tests |
| MIG-04 | Web dashboard | High | Private server metadata leakage | Add RBAC + visibility filters | API auth/authorization tests |
| MIG-05 | Security headers | Medium | XSS / clickjacking risk | Add middleware for security headers | Response-header tests |
| MIG-06 | Database schema | Medium | Migration drift and partial upgrades | Introduce versioned migration framework | Schema migration tests |

## Source inventory

### Bot core
- `main.py` — startup lifecycle and cog discovery
- `cogs/*.py` — commands, business logic, chat features
- `kai_core/config.py` — environment config
- `kai_core/logging_setup.py` — logging setup
- `kai_core/errors.py` — error capture and redaction
- `kai_core/parity.py` — slash command parity and prefix mirror logic
- `kai_core/version.py` — version metadata

### AI and security
- `kai_core/ai/` — routing, planner, scheduler, memory, safety, tools
- `kai_core/security.py` — security checks and RBAC primitives
- `kai_core/idempotency.py` — replay/duplicate protection helpers
- `kai_core/ratelimit.py` — shared rate-limit logic

### Database and persistence
- `economy.db` — primary user/economy data
- `kai_core.db` — core config/session metadata
- `kai_logs.db` — logging/audit backend
- `community_chat.db` — public dashboard chat
- `ai_schedules.db` — scheduler state

### Web/frontend
- `web/` — dashboard assets and Flask app routes
- `keep_alive.py` — production health server and app entrypoint

## Recommended migration strategy

1. Freeze the Python production contract.
2. Extract shared domain logic into stable interfaces.
3. Move transaction and economy bookkeeping into a central Rust service.
4. Port database repository and migrations before Discord command logic.
5. Port AI safety and memory modules before external provider integrations.
6. Migrate Discord cogs last, keeping command names and response contracts stable.

## Verification plan

The migration should be considered successful only when:

- `pytest` passes for the existing Python suite.
- Critical concurrent economy tests pass under load.
- Slash and prefix command parity remains consistent.
- Database migrations are reversible and versioned.
- Public dashboard/API access remains restricted by RBAC.

## Conclusion

The repository already contains a large amount of functionality and a decent test suite. The key migration goal is not a blind rewrite, but a disciplined extraction of domain logic into Rust services while preserving behavior.
