# Database Compatibility Plan

## Overview

The repository currently uses multiple SQLite files for different subsystems. The migration should preserve compatibility with these files and their table layouts while gradually moving to a Rust-backed repository layer.

## Current data stores

| Database | Purpose |
| --- | --- |
| `economy.db` | user balances, games, economy, companies, stock/share data |
| `kai_core.db` | core config and session metadata |
| `kai_logs.db` | moderation and audit logs |
| `community_chat.db` | public dashboard/community chat |
| `ai_schedules.db` | scheduler and AI scheduled tasks |

## Compatibility principles

1. Preserve table names and key columns.
2. Do not delete legacy data in place without backup.
3. Maintain readable SQLite WAL mode when applicable.
4. Add new columns with defaults instead of forcing destructive rebuilds.
5. Provide migration metadata so upgrades are versioned and reversible.

## Migration design

### Phase 1: Repository wrappers
- Create typed repository interfaces for each database.
- Implement adapters that read existing SQLite tables without rewriting stored data.

### Phase 2: Versioned schema tracking
- Maintain a `schema_migrations` table.
- Each migration declares:
  - unique version id
  - `up()` to create or alter columns/tables
  - `down()` to rollback safe changes

### Phase 3: Compatibility checks
- Validate the presence of required tables and columns before runtime.
- Fail fast with a clear migration error when a required table is missing.

### Phase 4: Performance tuning
- Use connection pooling in Rust.
- Keep transaction boundaries explicit for money-changing operations.
- Ensure WAL/foreign key settings are applied consistently.

## Example migration rules

- If `users.balance` exists, keep it and do not rename it.
- If `transactions` is missing `idempotency_key`, add it with `NULL`/default safe values before enforcing uniqueness.
- If required tables already exist, skip creation and check the schema using `PRAGMA table_info`.

## Backups and rollback

Before any schema change:

- snapshot the database file
- record migration version
- keep the backup path in a predictable location

Rollback strategy:

- prefer column-safe changes and `ALTER TABLE ... ADD COLUMN` patterns
- avoid destructive data migration without explicit restore steps
- support a restore path to the previous `.db` file if needed

## Risk management

The highest-risk area is `economy.db`, because it contains balance, company, and share state. The Rust migration should treat it as a write-sensitive critical path and must validate every atomic update against existing business rules.

## Acceptance criteria

The database compatibility layer is complete when:

- existing databases still open under the Rust app
- all legacy table names remain valid
- migrations are versioned and reversible
- no data loss occurs during the compatibility transition
- transaction tests pass under concurrent load
