# KaiAI Rust Migration Status

This workspace is the staged Rust migration foundation for the existing KaiAI Python bot. It is intentionally conservative: preserve Python behavior, validate each migration slice, and only then move higher-risk logic into Rust.

## Current status

The migration is in a healthy, validated intermediate state.

- Python project remains the production source of truth.
- Rust workspace is operational and test-covered.
- Core migration slices have been validated independently and as a workspace.
- The project is not yet a full Rust rewrite; it is a safe hybrid migration path.

## What is already validated

- `kai-config`: environment parsing and runtime defaults
- `kai-database`: SQLite connectivity, migration application, and repository operations
- `kai-economy`: atomic transfer logic, idempotency handling, negative-balance guard
- `kai-ai`: provider routing, prompt safety inspection, redaction and injection rejection
- `kai-web`: router endpoints and security-header behavior
- `kai-database`: business schema compatibility plus transactional contracts, acquisitions, preferred shares, listing, debt, ESOP, escrow, and reporting operations
- `kai-discord`: Gateway command handlers for the business operations, alongside core runtime handlers and a versioned compatibility envelope

## Architecture

- `kai-core` — shared primitives and app metadata
- `kai-config` — environment-aware runtime config
- `kai-security` — prompt and secret validation
- `kai-database` — SQLite pool, migration system, repository layer
- `kai-economy` — balance and transfer logic
- `kai-ai` — AI request routing and safety policy
- `kai-discord` — Discord Gateway client, slash-command synchronization, and repository-backed command handlers
- `kai-web` — API/dashboard endpoints and middleware

## Validation summary

The migration scaffold is considered valid when:

- the Python test suite remains green
- the Rust workspace passes `cargo test --workspace`
- core modules retain behavior expected from the legacy Python contract
- database and economy semantics remain atomic and idempotent

## Current recommendation

Continue the staged migration rather than rushing into a full rewrite. The Rust executable now connects to Discord's Gateway, synchronizes registered slash commands, and routes interactions through the repository-backed handlers. It also exposes a versioned request/response type for a trusted Python adapter.

The compatibility envelope is a typed in-process contract, not a network listener or a live Python-to-Rust transport. An adapter must populate user and guild IDs from an authenticated Discord event and must not expose the handler directly to untrusted callers. Administrator status is deliberately excluded from the serialized envelope; the Rust owner check is derived from `BOT_OWNER_ID`, while the Discord Gateway adapter reads administrator permission from Discord's interaction member data.

AI prompt safety and provider selection are integrated, but provider inference is not; AI execution reports this explicitly instead of pretending a response was generated. Command parity with all existing Python cogs has not been established, so this runtime is not yet a safe production replacement for the Python bot.

The Rust business slice registers the primary `business.py` commands and these `company.py` operations as Discord slash commands: `ctytao`, `nganhnghe`, `chonnganh`, `tuyennv`, `baotri`, `nhanviencty`, `bannpc`, `chuonhhdt`, `bautich`, `ctytongquan`, and `lamvieccty`. Commands with multiple values accept one `input` string separated by `|` (for example `Company A|marketing` or `Company A|@employee|cto`). Transactional operations cover company creation, hiring/firing, NPC share sales, shareholder voting, contracts, takeovers, preferred shares, IPO/repricing, lending, ESOP, bid escrow, bankruptcy, financial reporting, and the daily revenue/payroll/tax/history cycle. `lamvieccty` accrues term or overdue loan interest and pays it from post-operating company cash, at most once per business day. Skipped days are caught up when a loan is next run, repaid, or renewed; this remains command-triggered, not a background scheduler. Existing legacy loans are not retroactively charged for periods whose prior interest history cannot be reconstructed, but their remaining term is aligned to the recorded opening date when available. The runtime dispatcher recognizes aliases declared by `business.py` and routes them to the canonical handlers; the Gateway also accepts business-category prefix commands using `COMMAND_PREFIXES` (or a per-guild prefix set with the admin command). Enable Discord's privileged Message Content intent for this feature. Prefix arguments with ambiguous multi-word company names can use the same `|` separators as slash input; slash-command synchronization still publishes canonical names only, and prefix dispatch currently covers business-category commands. Revenue shocks and founding costs are deterministic but are not bit-for-bit identical to Python's PRNG. The Rust database path is separate from the Python bot's `economy.db`: data import/shared-database cutover, an autonomous daily-cycle scheduler, and parity for the remaining cogs are not yet integrated.

Application-command synchronization is opt-in to avoid overwriting Python's commands during migration: set `SYNC_GUILD_ID` for a test guild, or explicitly set `SYNC_GLOBAL_COMMANDS=true` for a global deployment. Do not run both runtimes against the same Discord application while either is synchronizing commands.

## Roadmap

1. Connect Python to the versioned compatibility contract over an authenticated local transport
2. Integrate actual AI provider inference and broader database-backed command behavior
3. Prove Python/Rust command parity before considering a production cutover

This status reflects a disciplined migration strategy: preserve compatibility, verify behavior, and only migrate what is proven safe.
