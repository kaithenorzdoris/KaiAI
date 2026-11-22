# KaiAI Ultimate Premium v5.0 — Release Notes

## Highlights

Bản v5 tập trung vào economy/gameplay và reliability. `cogs/games.py` được nâng cấp thành engine lớn hơn với database schema mở rộng, transaction log, persistent cooldowns, game statistics, XP progression và company simulator.

## New commands

- `!daily`
- `!work`
- `!quest`
- `!fish`
- `!mine`
- `!crime`
- `!topmoney`
- `!gamestats`
- `!history`
- `!market`
- `!chanle`
- `!coinflip`
- `!dice`
- `!highlow`
- `!rps`
- `!blackjack`
- `!roulette`
- `!ctyban`

## Reliability fixes

1. Game stake is deducted before settlement.
2. Payout is issued only after server-side result generation.
3. Balance updates reject negative balances.
4. Company investment re-checks balance inside a database lock.
5. Company sale transfers ownership and founder shares in one transaction.
6. Loan interest is calculated only for complete elapsed days.
7. Persistent cooldowns survive process restarts.
8. User creation is idempotent.
9. Database uses WAL and foreign-key mode.
10. User-facing amount parser handles `k`, `m`, `b`, `t`, `all`, `half` and percentage values.

## Known environment limitation

The development container used to package this release does not include Discord.py, so an actual Discord gateway session cannot be started here. Source compilation and repository tests are run locally during packaging; dependency installation and Discord API integration should still be validated in the deployment environment.
