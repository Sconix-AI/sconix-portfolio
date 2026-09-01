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

## Slice 3.7 — lifecycle, verify, principal

Codex's checkpoint task list, done in `pilot`:

- **Incident state machine** (`pilot/memory.py`): `detected → diagnosed →
  proposed → approved → acted → verified → resolved` (+ `escalated`), forward-only,
  `transition()` rejects illegal moves. Timestamps: `acted_at`, `verified_at`,
  `closed_at`. New fields: `diagnosis`, `confidence`, `resolution`.
- **Verify step** (`run.py:_settle`): after an allowed restart, re-probe the
  target the same tick; resolve only if healthy, else stay `acted` → next tick
  escalates.
- **Principal** (`pilot/principal.py`): `kind ∈ {human, coding-agent, ops-agent,
  ci}`, id, intent, scope. Recorded on every `Incident` and `Action`.
  `run_agent` still takes the `str(principal)` — that's R1 in
  `PILOT_REQUIREMENTS.md`.
- **Confidence** — the assessor now returns `0..1`; stored on the incident.
- 17 tests (adds lifecycle: verify+resolve, still-bad rollback, repeated
  incident, illegal transition, failed restart command).
- `task demo` re-run: incidents #1 `resolved` (recovered after action) / #2
  `escalated`; audit shows `ops-agent:pilot` on every row, linked to `inc#`.

Deliverable: **`PILOT_REQUIREMENTS.md`** — R1–R9 + cautions + open questions,
the Pilot half of the first platform checkpoint.

Still felt:

14. **A failed restart command still transitions the incident to `acted`.** The
    guard's `run_result` says "allowed" but doesn't carry whether the tool then
    succeeded; `_settle` only learns via the follow-up probe. Works, but the
    incident should distinguish "acted, action errored" from "acted, didn't
    help". → `pilot` backlog.
15. **`_settle` re-probes once, immediately.** No grace period for a slow
    restart. A real app needs "verify within N seconds, M attempts". → slice 4.

## Slice 3.8 — consume `sconixcore`

Codex extracted the Phase 2 contracts (`sconixcore` 0.1.0); Pilot now imports
them instead of keeping local copies:

- **`pilot/principal.py`** is a thin adapter over `sconixcore.Principal`
  (`kind ∈ {human, agent, service, ci}` + `role`). `PILOT` = agent/ops;
  `label()` still yields `ops-agent:pilot` for audit rows, so no data churn.
  `may_touch()` is now a free function (the core type has no method).
- **`pilot/act.py`**: `restart_app` is described by a `sconixcore.ActionSpec`
  (`RESTART`) — `risk=external-write`, `idempotent`, `approval=policy`,
  `Verification(checks, within_seconds=30, attempts=3, interval_seconds=2)`,
  `side_effects`, `preconditions`. The gate builds a `sconixcore.Decision`
  (outcome + accountable principal + timestamp) per check; `Action.decision`
  now stores the `DecisionOutcome` value (`allow` / `deny`), execution rows
  stay `done` / `failed`.
- **`run.py:_verify_recovered`** replaces the single re-probe with a retry loop
  driven by `RESTART.verification` — **closes #15**.
- 18 tests (adds `_verify_recovered` retry/give-up). `task demo` unchanged in
  behaviour; audit trail now reads `allow → done → deny`.

Pilot's policy (allow-set + cooldown) stays local, per the checkpoint — the
reusable contract is the types, not the rule.

Still felt:

16. **`ActionSpec.argv` is a placeholder** (`("sx","restart","<app>")`); the real
    argv comes from `_restart_cmd()` (per-target override). The spec and the
    executor should share one source once `sconix.yaml` `commands` land.

## Slice 4 prep — the executor seam

Per Codex's lane assignment (build the seam, don't build the executor):

- **`pilot/executor.py`** — `ActionExecutor` Protocol (`lookup` + `execute` →
  `ExecResult`) and a throwaway `LocalExecutor` (one action, from `pilot.act`).
  `DEFAULT` is the single binding slice 4 swaps for `sconixcore`'s manifest
  executor.
- **`act.make_guard`** now takes `executor=`; it denies **undeclared** actions
  and branches on `spec.approval` (`never` → allow, `policy` → allow-set +
  scope + cooldown, `always` → deny). Gate no longer special-cases
  `"restart_app"` by string.
- **`run.tick`** executes via `EXECUTOR.execute(...)`; **`_verify_recovered`**
  reads `EXECUTOR.lookup("restart_app").verification`.
- `test_executor.py` (+10): lookup, argv-no-shell, undeclared→deny/raise,
  approval modes, principal scope, exec-result→audit. 28 tests total.

Deliverable: **`PILOT_SLICE4_ADAPTER.md`** — the Protocol contract, the
one-line swap, and what `sconixcore`'s executor must provide.

Deferred to the deploy/rollback integration: stale/expired approval + one-time
consumption tests (restart is `approval: policy`, so the gate is the approval).

## Slice 4 — the real executor

Swapped `LocalExecutor` for **`sconixcore.ManifestExecutor`** (Systems
`5e92d6f`). `_restart_cmd` / `restart_app` and the per-target `restart:` override
are **deleted** — `restart`'s argv, risk, approval, and verification now come
from each target's `sconix.yaml`.

- **`pilot/manifest.py:resolve_target`** — target → `(manifest, root)`, read from
  `targets.yaml`'s `manifest:` / `root:` fields. `ManifestExecutor(resolve=…)`.
- **`targets.yaml`** gained `manifest:` + `root:` per target (relnotes,
  skillforge → `~/systems/apps/<name>/sconix.yaml`).
- **the drill** gets a `sconix.yaml`: committed `drill.sconix.yaml` (port 8765)
  for `task pilot -- watch --targets drill.targets.yaml`; the demo generates one
  with an ephemeral port + `sys.executable`.
- **`make_guard`** stashes the `sconixcore.Decision`; `run.tick` passes it to
  `execute()` — `approval: policy` fails closed without it.
- tests reorganised around `tests/conftest.py` (`FakeExecutor`, `session`); real
  `ManifestExecutor` covered against an in-memory manifest. 26 tests.
- `task demo` re-verified: the `done` audit row now shows the real subprocess
  output (`{'mode': 'healthy'}` from `python -m pilot.drill heal`).

## Slice 4b — deploy/rollback proposal + human-approval boundary

`pilot/deploy.py` + `tests/test_deploy.py` (32 tests). Incident state machine
gains **`awaiting_approval`** (`proposed → awaiting_approval → approved`);
`Incident.plan_id` + `Action.plan_id` added.

- `propose` runs `<kind>_plan` (`approval: never`) → parses the plan id → parks
  the incident. `approval_status` reads `$SCONIX_STATE_DIR/deploy/*` via
  `sconixcore.deploy` (Pilot never writes). `execute_approved` runs `<kind>`
  only when a human approval exists, with an `allow-once` Decision naming the
  approver.
- Built against snake_case command keys (`deploy_plan`, …); the real app
  manifests still say `deploy-plan` — **do not wire `deploy.py` into a real
  manifest until Codex's rename commit.**
- Canary held — shared edge aliases make parallel canaries unsafe until Codex
  ships release-scoped aliases.

Still felt:

17. **`RESTART` ActionSpec in `pilot/act.py` is now only used for its `.name`.**
    The real contract lives in the manifest.
18. **`resolve_target` re-reads `targets.yaml` on every call.** Fine at this
    scale; cache on mtime if the fleet grows.
19. **No watch-loop trigger for deploy/rollback yet (slice 4c).** `propose` /
    `execute_approved` are a library; nothing decides *when* rollback beats
    restart, or polls `awaiting_approval` incidents for a fresh approval.
