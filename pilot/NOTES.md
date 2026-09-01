# Friction log → Sconix engine backlog

What hurt while building each slice, and where the fix belongs.

## Slice 1 — observe & assess

Reusing `sconixapp.agent.run_agent` from a non-app context worked, but the seams showed:

1. **`run_agent` assumes a per-user SaaS request.** It requires `user_id` and an
   `AsyncSession`, and `pick_model` is built around a per-user monthly token
   ceiling. An ops agent has no user. Worked around with a synthetic principal
   `"system:pilot"` + a local sqlite. → engine: a first-class system/service
   principal, and a fleet-level (not per-user) budget ceiling.

2. **No reusable tools.** Every app hand-rolls its own `@beta_async_tool`
   functions (`list_merged_prs`, …). An HTTP health probe is generic and every
   ops agent will want it. → engine: a `sconixapp.agent.tools` module with
   `probe_http`, and later `read_logs`, `run_command`.

3. **Table schema needs Alembic wiring per app.** `AgentRun` is a SQLModel
   `table=True`; using it standalone meant `SQLModel.metadata.create_all` by
   hand. Fine for a script, awkward for a real service. → engine: a tiny
   `sconixapp.agent.ensure_tables(engine)` helper.

4. **`run_agent` runs every tool the model calls, no gate.** Not felt in slice 1
   (no tools), but this is the blocker for slice 2 — an agent with `sx restart`
   in its toolbox must not just fire it. → engine: tools declare
   `mutating: bool`; `run_agent` takes an `approve` callback invoked before any
   mutating tool runs.

5. **No shared, non-app secret store.** The pilot needs `ANTHROPIC_API_KEY` but
   has no app `secrets.env`. Added `~/systems/secrets.env` (SOPS+age, same rule
   as apps) + a local `pilot/secrets.py` loader. → engine: move that loader into
   `sconixapp` (`sconixapp.secrets.load`) and give `sx` a `secrets --system`
   subcommand to edit `~/systems/secrets.env`.

## Slice 2 — gated action

Engine changes made (the first real ones):

- **`sconixapp.agent.guarded_tool(fn, *, guard)`** — wraps an async tool so a
  `guard(name, kwargs) -> bool | str` runs first; a str/False is returned to the
  model as `BLOCKED: <reason>` and the side effect never happens. Schema
  inference still sees `fn`'s signature (via `functools.wraps` + `__wrapped__`).
  8 tests in `sconixapp`.
- **`sx restart <app> [user@host]`** — in-place `docker compose restart` + health
  re-check + ledger line. Mirrors `sx deploy`.

Still felt, not yet fixed:

6. **The gate is all-or-nothing per call.** No dry-run, no "allow once then
   re-ask", no diff of what will change. Fine for restart; too blunt for deploy
   / rollback. → engine: a richer `Decision` (allow / allow-once / deny / defer)
   and a `plan` the guard can inspect.
7. **`guarded_tool`'s policy + audit live in the app (`pilot/act.py`).** Every
   ops agent will re-write them. → engine: `sconixapp.agent` ships an `Action`
   table + an `allowset` guard factory; apps supply only the allow-set.
8. **No way to exercise the down→act path E2E without breaking prod.** Need a
   chaos/drill harness (a throwaway target, or `sx` pause) so slice 3's
   autonomous loop can be tested against a real failure. → `pilot` backlog.

## Slice 3 — unattended loop + memory

Built entirely in `pilot` (Codex was editing `~/systems` — no engine changes
this round, on purpose):

- **`pilot/memory.py`** — `Incident` table + `observe()` / `history()` /
  `summarize()`. The assessor now gets prior-incident context; a target that's
  been down six ticks is described differently from a first blip.
- **restart cooldown** — `make_guard(cooldown_s=600)` denies a second
  `restart_app` for the same target inside the window (reads the `Action` log).
  Stops a wedged app from being restart-looped every tick.
- **`watch` mode** — `tick()` on a timer, one line per tick, `Ctrl-C` clean.
  Ran live: 3 ticks / 22 s against the real fleet, all `ok`.
- 9 pilot tests (3 probe, 3 guard, 3 memory).

Still felt:

9. **`PRINCIPAL = "system:pilot"` is still a bare string.** `run_agent` wants a
   `user_id`; an unattended loop has none, so every `AgentRun` row is filed under
   a magic string and `pick_model`'s per-user ceiling is dead weight. This is the
   slice-1 gap #1, now load-bearing. → engine: `sconixapp.agent` accepts a
   `Principal` (user | service), and budget ceilings attach to the principal.
10. **`watch` has no backoff or jitter.** Fixed interval; a flapping fleet gets
    hammered at the same cadence as a calm one. Fine for a demo, wrong for real.
    → `pilot` backlog (slice 4/5).
11. **Incident memory never forgets.** No retention / rollup; `pilot.db` grows
    forever. → `pilot` backlog: close-out summaries + prune.

## Slice 3.5 — drill harness

`pilot/drill.py` — a fake app (`/healthz` + `/readyz`, same shape as
`sconixapp.health`) whose state you toggle: `healthy | wedged | down | degraded`.
`wedged` (healthz 503, deps fine) is the "a restart clears it" case; `down`
(readyz reports a dead dep) is the "restart won't help" case. Per-target
`restart:` in the targets file overrides `sx restart` — the drill heals itself.

`pilot/demo.py` / `task demo` — runs the real `tick()` through
wedge → agent restarts (gate ALLOWED) → recover → wedge again (gate DENIED,
cooldown). Verified live; audit trail comes out as exactly `allowed → done →
denied`.

Fixed along the way: `tick()` was writing a `done` audit row on every `--fix`
pass even when the agent never called the tool. Now the tool records its own
`done` / `failed`, and the row only exists if a restart actually ran.

Still felt:

12. **`observe()` keeps an incident open while severity is `warn`.** After a
    successful restart the app is `ok` at the probe but the assessor calls it
    `warn` (flapping), so the incident doesn't close. Defensible, but the
    close/flap policy needs a real decision. → `pilot` backlog.
13. **The drill only fakes HTTP health.** No CPU/mem pressure, no partial
    failure, no slow-then-fail. Enough for slice 3.5; slice 4 (canary) will want
    a richer fault menu.
