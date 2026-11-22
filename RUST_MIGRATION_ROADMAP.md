# Rust Migration Roadmap

## Goal

Provide a disciplined migration from the current Python Discord bot to a maintainable Rust architecture without breaking the bot's user-facing behavior.

## Target architecture

```text
kai-ai/
├── Cargo.toml
├── crates/
│   ├── kai-core/
│   ├── kai-config/
│   ├── kai-discord/
│   ├── kai-ai/
│   ├── kai-memory/
│   ├── kai-agent/
│   ├── kai-economy/
│   ├── kai-business/
│   ├── kai-games/
│   ├── kai-security/
│   ├── kai-database/
│   ├── kai-web/
│   ├── kai-dashboard/
│   └── kai-monitoring/
├── migrations/
├── tests/
├── docs/
└── scripts/
```

## Phase 1 — Stabilize the Python contract

- Freeze public command names and outputs.
- Document command parity and expected slash/prefix behaviors.
- Add or preserve regression tests around economy, admin, and AI safety.

## Phase 2 — Extract the core domain

Migrate the foundational pieces first:

1. `kai_core/config.py`
2. `kai_core/errors.py`
3. `kai_core/security.py`
4. `kai_core/logging_setup.py`
5. `kai_core/idempotency.py`
6. `kai_core/ratelimit.py`
7. `kai_core/parity.py`

## Phase 3 — Database and persistence layer

- Create Rust repositories over SQLite.
- Preserve `KAI_DATA_DIR` and existing database names.
- Add versioned migrations and rollback support.
- Keep compatibility with current tables and column names.

## Phase 4 — Economy engine

- Replace the scattered Python lock logic with a single Rust transaction coordinator.
- Implement atomic transfers and balance updates.
- Ensure idempotency for retried or duplicate requests.

## Phase 5 — AI subsystem

- Port routing, provider abstraction, safety checks, memory, and plans.
- Keep provider fallback behavior identical to Python.
- Store AI memory and plan state in a durable repository.

### Current Rust progress

- OpenAI/Gemini provider routing, bounded retries, token budgets, concurrency
  limits, and AI safety checks are implemented and covered by Rust tests. The
  OpenAI adapter sends the complete normalized request history, matching the
  active Python provider instead of applying its unused `max_history` setting.
  It falls back from Responses invocation failures, including HTTP and JSON
  decoding errors, to Chat Completions, matching the attached Python adapter's
  `_invoke` behavior; response-content validation still reports directly.
- The latest 20 conversation messages now persist by user/server in SQLite,
  expire after 30 minutes, redact common secrets, and can be removed with
  `clearai`/`forget`. Facts, preferences, and summaries have bounded,
  redacting, guild-scoped persistence; `aimemory`/`history` shows only the
  requesting user's data for the current server or DM. Every 18 persisted
  messages, Rust requests a bounded structured summary and safe user facts,
  stores them by user/server, and compacts old turns while retaining six recent
  messages. AI request and token totals are available through the scoped `stats`
  command and are removed by `clearai`/`forget`.
- AI schedule persistence and runtime execution are implemented for daily,
  weekly, and monthly Vietnam-time schedules. Commands cover create/list/cancel/
  run-now; schedules are owner-scoped, capped at three per user, atomically
  claimed, and disabled after five consecutive failures. `AI_SCHEDULER` can
  disable execution, and outcomes are sent by DM.
- Mood presets persist per user/server and reset the current conversation.
  Slash `/mood` presents an ephemeral, user-bound selector; prefix `!mood`
  keeps the text preset form. Prefix/DM chat supports `ai ...`, bot mentions,
  replies, and bounded image attachments fetched only from approved Discord CDN
  hosts. OpenAI and Gemini receive multimodal inputs, and the `taoanh`/`image`
  command generates bounded OpenAI image responses with a per-user cooldown.
- The `menu`/`aimenu` command lists AI features. The `stats`/`aistats` command
  reports only the requesting user's scoped memory and AI token totals.
- Ordinary `ai` chat can request inline image generation with `[GEN_IMG]`.
  Generated images are attached on prefix and slash response paths; prompt
  parsing is bounded and focused Rust tests cover the directive behavior.
- Wallet transfers and wallet/bank moves update balances and transaction
  audit rows atomically in the Rust repository. Rust now has typed, owner-scoped
  pending action proposals, 60-second expiry, a three-proposal per-user cap,
  secure one-use tokens, and confirm/cancel/list commands; proposals now persist
  in SQLite and are atomically consumed before execution. The active offline
  multi-step plan parser, typed plan store, and list/show/cancel/dry-run/execute
  commands are also ported. Plans enforce ownership, a 10-step limit, a
  500,000,000 VND total cap, a five-plan per-user cap, 15-minute pending-plan
  expiry, and stop after the first failed transaction.
- Plans now persist as typed JSON in SQLite with owner-scoped access, pending
  expiry and cap enforcement, atomic pending-to-running claims, and persisted
  per-step/final statuses. Startup recovery marks any in-flight plans
  `interrupted` and never retries them automatically, avoiding duplicate
  money operations after an ambiguous crash. Pending actions also persist in
  SQLite and retain one-use, owner-scoped confirmation with expiry and cap
  enforcement; failed confirmations remain consumed to prevent retries.
- The Python bot remains the production entrypoint; this Rust work is not a
  production cutover.
- Phase 5 code and offline regression coverage are implemented. End-to-end live
  Discord validation remains outstanding because this environment has no
  configured bot credentials or connected production/test bot. The Python bot
  remains the production entrypoint; do not treat this Rust migration as a
  production cutover until that validation is completed.

## Phase 6 — Discord command layer

- Migrate cogs to Rust command handlers.
- Maintain command registration names and compatibility.
- Preserve slash/prefix parity handling.

### Current Rust progress

- The first economy/game command slice now includes `cash`, `dep`, `with`, and
  `pay`, with the Python aliases `balance`/`bal`/`money`, `deposit`,
  `withdraw`/`rut`, and `give`/`chuyentien`. These handlers are available through
  both slash commands and configured prefix commands, use the shared SQLite
  account repository, retain transaction audit rows, and validate bounded
  integer amounts plus the Python `all`/`half`/percentage and `k`/`m`/`b`/`t`
  formats. The Rust database currently models wallet and bank balances only, so
  `cash` reports those supported fields and net worth instead of fabricating
  Python-only level, streak, or reputation data.
- A stateless utility slice now includes `math`/`calc`/`calculate`,
  `timestamp`/`timecode`, `color`/`hex`, `random`, `id`, and `inviteinfo`.
  These commands are available through both slash and configured prefix
  dispatch. Calculator expressions use a bounded parser and Python's
  safe-operation allowlist; random selection uses OS-backed randomness and
  preserves the one-million-wide range cap. Timestamp conversion accepts ISO
  8601 date/time input and assumes UTC for timezone-naive values.
- The system-command slice now includes `help` (`h`/`commands`/`cmds`),
  `status` (`sysinfo`/`sys`/`info`), `uptime`, `ping`, and `invite` (`addbot`)
  through the same slash/prefix utility route. Invite URLs use the active bot
  identity and the Python-configured limited OAuth permission set, without
  Administrator; the command reports the not-ready state before the gateway
  provides that identity. Help is generated from Rust's registered command
  definitions and explicitly does not claim unported Python-cog parity.
  Uptime uses a monotonic runtime start time; status reports runtime version,
  registered command count, and whether database services were initialized.
  `ping` confirms runtime responsiveness without claiming a WebSocket latency
  measurement, which this runtime does not expose.
- The economy minigames slice includes `daily` (`diemdanh`), `work` (`lamviec`/`job`),
  `taixiu` (`tx`), `chanle` (`cl`/`oddeven`), `coinflip` (`cf`/`dongxu`), `dice` (`xucxac`),
  and `slots` (`slot`/`quayhu`). Games use server-side secure PRNG settlement, enforce 24-hour
  and 15-minute cooldowns, atomic balance mutations with immediate row-locking in SQLite,
  and transaction logging.
- The career and education progression slice now includes `truonghoc` (`hocvan`/`school`),
  `nhaphoc` (`enroll`), `totnghiep` (`graduate`), `vieclam` (`jobboard`), `ungtuyen` (`apply`),
  `thangchuc` (`promote`), `nghiviec` (`resign`), and `hoso` (`cv`/`career`). It enforces academic
  degree prerequisites (Bachelor, Master, Doctor), study duration timers, minimum work counts,
  and salary tier progression.
- The real-world simulation and savings banking slice now includes `giatham` (`chotroi`/`market`),
  `muadi` (`buyitem`), `tudon` (`pantry`), `an` (`eat`), `laisuat` (`savingsplans`), `moso` (`opensavings`),
  `rutso` (`tattoan`), `socuatoi` (`mysavings`), and server wealth leaderboard `top` (`topmoney`).
  It models daily grocery prices, pantry shelf life, health/energy replenishment with daily meal caps,
  and term deposit interest yields with early withdrawal penalties.
- Full workspace test suite now passes with 171 automated unit tests across all 8 crates.

## Phase 7 — Web frontend and dashboard

- Move Flask routes into an Axum or Actix web service.
- Add the same public/private dashboard protections.
- Surface health and metrics in a structured way.

## Phase 8 — Hardening and release

- Run `cargo test`, `cargo clippy`, and `cargo fmt`.
- Validate security regressions, migration rollback, and concurrency behavior.
- Prepare deployment and Docker assets for the Rust service.

## Exit criteria

The migration is incomplete until:

- all major commands still function
- economy transactions remain atomic and idempotent
- AI safety layers still block prompt injection
- public/private dashboard access is correctly gated
- the Rust project compiles and passes a full test pass
