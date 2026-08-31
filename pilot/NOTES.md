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
