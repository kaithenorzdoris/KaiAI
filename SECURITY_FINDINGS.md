# Security Findings

## Summary

The project has a usable security model at a basic level, but it does not yet have a singular security boundary across all bot and web surfaces. The most important findings are concurrency issues in shared economic state and missing authorization and audit protections in the public web app.

## Findings table

| ID | Location | Severity | Impact | Proposed fix | Verification |
| --- | --- | --- | --- | --- | --- |
| SEC-01 | `cogs/games.py`, `cogs/company.py`, `cogs/business.py` | Critical | Race conditions in shared economy state | Central transaction coordinator with one process-wide lock | Parallel balance tests |
| SEC-02 | `kai_core/economy.py`, `transactions` schema | High | Double-spend and replay of same action | Add idempotency key and unique DB constraint | Retry and duplicate-submit tests |
| SEC-03 | `keep_alive.py` / web guide API | High | Prompt injection and secret disclosure attempts | Add AI safety inspection and output sanitization | Prompt injection regression tests |
| SEC-04 | `keep_alive.py` / `GET /api/servers` | High | Leakage of server metadata | Add RBAC and server visibility filtering | Auth test coverage |
| SEC-05 | `keep_alive.py` | Medium | XSS/clickjacking risk | Add standard HTTP security headers | HTTP header assertions |
| SEC-06 | `kai_core/ai/memory.py` | Medium | Unbounded retention of user memory | Add TTL and purge endpoint | Memory retention tests |
| SEC-07 | `kai_core/errors.py` | Low | Secret leakage in errors | Continue central redaction and sanitize stack traces | Secret redaction tests |

## Detailed findings

### SEC-01 — Shared lock desynchronization
- Location: `cogs/games.py`, `cogs/company.py`, `cogs/business.py`
- Risk: Multiple async locks guarding the same SQLite database can allow divergent interleavings and race conditions.
- Impact: Users may see inconsistent balances, lost updates, or database lock errors under parallel commands.
- Fix: Create a single, shared, loop-safe economy lock and route all mutations through a transaction coordinator.
- Verification: Run parallel load tests for transfers, bets, and company share actions.

### SEC-02 — Missing idempotency for transactions
- Location: DB tables and transaction methods in economy logic
- Risk: A repeated interactive click or network retry can apply the same reward or deduction twice.
- Impact: Financial inconsistency and user trust issues.
- Fix: Add `idempotency_key` unique rows and reject duplicate keys within TTL.
- Verification: Two identical transfer requests with the same key must return the first result only.

### SEC-03 — Prompt injection in the public guide API
- Location: `keep_alive.py` and AI guide integration
- Risk: Attackers can send crafted prompts trying to override instructions or extract system-level context.
- Impact: Unsafe model output and possible secret leakage.
- Fix: Add pre-checks for injection patterns and a persistent output sanitizer.
- Verification: Regression tests for known prompt injection payloads.

### SEC-04 — Public metadata exposure
- Location: `keep_alive.py`
- Risk: Public dashboard exposes server metadata without authorization filtering.
- Impact: Private guild information becomes visible.
- Fix: Filter server list by explicit visibility flags and require admin auth.
- Verification: Ensure guest/public users cannot read private server titles or member counts.

### SEC-05 — Missing HTTP security headers
- Location: Flask app object in `keep_alive.py`
- Risk: Lack of CSP, X-Frame-Options, and nosniff headers.
- Impact: Increased attack surface for XSS and clickjacking.
- Fix: Enforce security headers in an `after_request` middleware.
- Verification: Assert header presence for all major endpoints.

### SEC-06 — AI memory retention without purge controls
- Location: `kai_core/ai/memory.py`
- Risk: User memory persists without retention control or purge endpoints.
- Impact: Private conversation memory can be retained longer than desired.
- Fix: Add TTL and explicit memory clear flow.
- Verification: Test that expired entries are pruned and explicit clear removes the user memory.

## Recommended remediation order

1. Fix shared economy lock and idempotency.
2. Add web RBAC and prompt-injection defense.
3. Add security headers and audit logging.
4. Add memory TTL and purge flows.
5. Add security tests to the CI pipeline.

## Security posture target

The end-state target is a single security boundary with:

- centralized access control
- transaction idempotency
- prompt sanitization
- strict output validation
- durable audit logs
- public/private dashboard visibility rules
