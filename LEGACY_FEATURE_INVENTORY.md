# Legacy Feature Inventory

## 1. Core bot and lifecycle

### Discord bot bootstrap
- `main.py`
  - loads config from environment
  - discovers and loads cogs
  - registers startup logging and shutdown hooks
  - syncs slash commands with `tree.sync()`

### Health and web app
- `keep_alive.py`
  - hosts Flask/Waitress app
  - exposes dashboard and public status endpoints
  - serves public community chat and guide endpoints

## 2. Command modules

### Economy and games
- `cogs/games.py`
  - rewards, work, bank, daily claims, item shop, betting, slots, company game logic
- `cogs/company.py`
  - business ownership, share trading, NPC buyback, tax flows
- `cogs/business.py`
  - enterprise / business simulation, contracts, board logic
- `cogs/career.py`
  - work, progression, education, career tasks
- `cogs/realworld.py`
  - simulation and lifecycle systems for a virtual world

### AI
- `cogs/ai_chat.py`
  - AI chat commands and provider routing
- `cogs/ai_plan.py` and related AI support modules under `kai_core/ai/`
  - planning, scheduling, memory, safety filters

### Administration and logs
- `cogs/admin.py`
  - moderation and control commands
- `cogs/system.py`
  - system-wide admin functions
- `cogs/logs.py`, `cogs/logs_center.py`
  - logging, moderation, message auditing, server activity
- `cogs/diagnostics.py`
  - error, command audit, parity, health reporting

### Utility and social
- `cogs/utility_pro.py`
  - server/member diagnostics and helper commands
- `cogs/love.py`
  - social simulation and relationship features
- `cogs/database.py`
  - DB queries and lookup utilities

## 3. Shared foundation

### Kai core
- `kai_core/config.py` — runtime config
- `kai_core/errors.py` — error recorder and redaction
- `kai_core/logging_setup.py` — logger
- `kai_core/pagination.py` — embed pagination
- `kai_core/parity.py` — slash command parity
- `kai_core/idempotency.py` — duplicate protection helpers
- `kai_core/security.py` — safety checks and access controls
- `kai_core/ratelimit.py` — rate-limit primitives

### AI subsystem
- `kai_core/ai/router.py` — provider routing
- `kai_core/ai/memory.py` — memory persistence
- `kai_core/ai/plan.py` — plan management
- `kai_core/ai/scheduler.py` — task schedule runner
- `kai_core/ai/safety.py` — prompt injection and secret filtering

## 4. Key runtime behaviors to preserve

- Prefix command compatibility and slash parity generation
- Shared economy semantics across games, company, business, and career paths
- Data persistence in SQLite files under `KAI_DATA_DIR`
- AI provider fallback behavior and safety checks
- Public dashboard features and community chat endpoints
- Owner/admin-only diagnostics and server admin flows

## 5. Non-goals for migration

These are runtime artifacts and should not be migrated:

- `.venv/`
- `.pytest_cache/`
- `.ruff_cache/`
- `__pycache__/`
- SQLite WAL and SHM files
- generated logs such as `kai.log`

## 6. Migration priority

Priority order:

1. Core config, logging, security, and error handling
2. Economy and transaction service
3. Database repositories and migration layer
4. AI safety and memory subsystems
5. Discord command layer
6. Web dashboard and public endpoints

This ordering minimizes behavior risk and preserves existing user experience while the Rust project is introduced.
