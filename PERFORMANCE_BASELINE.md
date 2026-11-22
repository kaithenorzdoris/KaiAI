# Performance Baseline

## Current observations

The project is already testable and mostly stable, but it is not optimized for high concurrency. The main performance hotspots are SQLite I/O, in-process shared mutable state, and repeated database connections.

## Baseline metrics from the repo

### Test suite
- `pytest` is the project's main validation mechanism.
- Current test suite execution is multi-minute in duration due to repeated connection setup and startup flows.
- This is acceptable for a Python codebase in development, but it indicates a need for better database connection pooling and more targeted test fixtures.

### Primary runtime hotspots
- `economy.db` is used across multiple cogs and is a shared mutation source.
- `ai_schedules.db` and other SQLite files create multiple persistence surfaces.
- The web dashboard and AI routes can stack on top of the same underlying data if auth and rate limiting are not centralized.

## Key constraints

### SQLite and transactions
- WAL mode is used in major data paths but not consistently standardized across all modules.
- Multiple cogs each create their own locks and database connections.
- This creates contention and reduces throughput under concurrent users.

### AI async flows
- Provider calls are subject to network variability and retry backoff.
- Router and safety checks should be fast and bounded.
- Long-living memory state should avoid unbounded in-memory growth.

### Web dashboard
- Public dashboard pages should be lightweight and avoid broad JSON payloads.
- Data refresh loops should be bounded and cached.

## Performance targets for Rust migration

| Area | Baseline concern | Rust target |
| --- | --- | --- |
| Economy transactions | Multiple locks and SQLite contention | Single transaction coordinator with bounded async tasks |
| Database access | Repeated ad hoc SQLite connections | Shared pool and typed repositories |
| AI routing | Provider latency and retries | Async provider abstraction with timeout and retry policies |
| Dashboard endpoints | Public metadata and broad scans | Restricted queries and cached channel/server metadata |
| Memory state | In-memory plan/memory duplication | Persistent store with bounded retention |

## Verification methods

- Run economy tests with parallel transfer scenarios.
- Benchmark repeated transfer/update operations under spawned tasks.
- Measure dashboard response times under public requests.
- Measure AI request latency with provider fallback enabled.
- Record baseline and compare after migration.

## Conclusion

The project is operationally healthy enough for production-lite use, but the current architecture is not highly parallelized. Rust should improve safety and throughput, especially in the transaction and repository layers.
