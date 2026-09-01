# pilot

An agent that watches a small fleet of deployed apps and says what it sees.

This is flagship **F1** of the Sconix portfolio — an agentic system with real
side effects, built on the Sconix Systems engine (`sconixapp`, `sx`, the shared
Caddy edge). It grows one slice at a time; each slice that hurts feeds a fix back
into the engine.

**Operators:** see [`RUNBOOK.md`](RUNBOOK.md) — the safety contract, incident
states, the approval flow, the canary → promote → rollback → teardown playbook,
failure cases, and the canary/database limitations.

## Where it is now — the agent operating loop, end to end

```
                       ┌─────────  watch: every N s  ─────────┐
                       ▼                                      │
 targets ─▶ probe ─▶ assess ─▶ incident ─▶ propose ─▶ GATE ─▶ act ─▶ verify ─▶ resolve
  fleet    health   diagnosis   lifecycle   restart   policy   sx     re-probe    │
                    +confidence  (below)               +audit  restart          escalate
```

**Incident lifecycle** (`pilot/memory.py`, forward-only, transitions validated):

```
detected → diagnosed → proposed → approved → acted → verified → resolved
                |           |          |        |
                +---------- escalated <-+--------+     (needs a human)
```

- `probe()` — read-only. `/healthz` + `/readyz`, timed. Never raises.
- `assess` — `run_agent` (haiku) → `{severity, headline, detail, confidence}`,
  fed the target's **incident history** so a flap reads differently from a blip.
- `observe()` — opens an `Incident` (state `detected`) on the first non-ok; a
  later `ok` while state is `acted` → `verified` → `resolved`; a fresh failure
  after resolution is a **new** incident.
- `fix` — only with `--fix`, only for `warn`/`down`. `run_agent` (sonnet) with one
  mutating tool, `restart_app`, wrapped by `sconixapp.agent.guarded_tool`.
- the **gate** (`pilot/act.py:make_guard`) allows a restart only when the target
  is unhealthy this run, the **principal** is permitted, and no restart happened
  in the last 10 min. Allowed / denied both hit the `Action` audit table.
- **verify** — after an allowed restart, `tick()` re-probes the target the same
  pass; the incident resolves only if it's actually healthy again.
- every `run_agent` call → an `AgentRun` row (cost/tokens); every proposal →
  an `Action` row (principal, decision, reason). Both in `pilot.db`.

**Built on `sconixcore`** (the platform's Phase 2 contracts): `Principal`
(who caused it — the loop runs as agent/ops → `ops-agent:pilot`), `ActionSpec`
(`restart_app`'s declared risk / approval / verification / side-effects),
`Decision` (outcome + accountable principal, per gate check), `Verification`
(the retry/grace re-probe). Pilot keeps only its own **policy** (allow-set +
cooldown) local.

`restart_app` is the only real side effect, and it cannot fire unless policy says so.

## Run

```bash
task setup
# key comes from ~/systems/secrets.env (SOPS+age); or export ANTHROPIC_API_KEY
task pilot                          # one pass over all targets
task pilot -- relnotes              # just one
task pilot -- --fix                 # arm restart_app (policy-gated, audited)
task pilot -- watch --every 60      # unattended loop, read-only
task pilot -- watch --fix --for 900 # unattended, armed, for 15 min
task demo                           # end-to-end drill (see below)
task test                           # no network
```

## See it work — `task demo`

The drill (`pilot/drill.py`) is a fake app whose health you toggle. `wedged` =
`/healthz` 503s but dependencies are fine — a restart *should* clear it. The demo
runs the real `tick()` against it; the only side effect is healing the drill.

```
tick 1  drill=ok   [resolved]              baseline
tick 2  drill=down  [resolved] ← RESTARTED  wedged → agent: "classic transient" →
                                            gate ALLOWED → heal (33ms) → verified → resolved
tick 3  drill=ok   [resolved]              stays healthy
tick 4  drill=down  [escalated]             wedged again, inside cooldown →
                                            gate DENIED → incident escalated

incidents:  #1 resolved  · recovered after action
            #2 escalated · denied: already restarted within 600s
actions:    ops-agent:pilot · allowed · inc#1
            ops-agent:pilot · done    · inc#1  (restarted drill in 33ms)
            ops-agent:pilot · denied  · inc#2
```

`down` mode (a real dependency outage — `readyz` reports `db: error`) is the
negative case: the agent reads it as an outage and declines to restart on its own.

## Roadmap

| slice | adds | forced into the engine |
|-------|------|------------------------|
| 1 ✅ | probe + assess + per-run accounting | (nothing — proved the seam) |
| 2 ✅ | mutating tool (`restart_app`) + **policy gate** + audit trail | `sconixapp.agent.guarded_tool`; `sx restart` verb |
| 3 ✅ | unattended `watch` loop + incident memory + restart cooldown | (none — proved in pilot) |
| 3.5 ✅ | drill harness + `task demo` — down→act→cooldown-deny, end to end | (none — pilot-only) |
| 3.7 ✅ | incident **state machine** + **verify** step + **principal** on every incident/action + confidence | (none — logged in `PILOT_REQUIREMENTS.md` for Codex to extract) |
| 3.8 ✅ | consume `sconixcore` (`Principal` / `ActionSpec` / `Decision` / `Verification`); retry-aware verify | (none — Codex's Phase 2 contracts, now a real consumer) |
| 3.9 ✅ | the **executor seam** — `ActionExecutor` Protocol; gate reads `approval` from the action spec | (spec'd in `PILOT_SLICE4_ADAPTER.md`) |
| 4a ✅ | consume **`sconixcore.ManifestExecutor`** — `restart`'s argv/risk/approval/verify come from each target's `sconix.yaml`; `_restart_cmd` deleted | Codex's `ManifestExecutor` (Systems `5e92d6f`) |
| 4b ✅ | **deploy / rollback proposal + human-approval wait** — `propose` a plan → incident `awaiting_approval` → after a human `sx approve`, `execute_approved`; Pilot never approves | Codex's plan store (`sconixcore` `load_record`, `sx deploy --plan` / `approve`) |
| 4c ✅ | in the **watch loop** — `--allow-rollback` proposes a `rollback_plan` once a restart is exhausted (`should_rollback` policy); `resume_if_approved` executes it the tick a human approves | — |
| 4d ✅ | **canary lifecycle** — `propose`/`execute_approved` generalised to `canary` / `promote` / `canary_teardown` (operator-initiated, own fresh approval each); the autonomous loop still only ever proposes `rollback` (`AUTONOMOUS_KINDS`) | Codex's `sx promote` / `sx teardown` (Systems `7e33ac6`) |
| — ✅ | **autonomous behaviour frozen** + [`RUNBOOK.md`](RUNBOOK.md) + read-only `pilot report` (states, principals, plan ids, resolutions, cost; `--json`) | — |
| 5 | public status page | an incident/status package |

## Docs

- **`RUNBOOK.md`** — operator runbook: safety contract, incident states, approval
  flow, canary/promote/rollback/teardown playbook, failure cases, DB limits.
- `PILOT_REQUIREMENTS.md` — the "Pilot needs from Sconix" contract (checkpoint 1;
  pairs with the constitution / glossary / manifest schema).
- `PILOT_SLICE4_ADAPTER.md` — the `ActionExecutor` seam and how
  `sconixcore.ManifestExecutor` + the deploy/rollback/canary flow are wired in.
- `NOTES.md` — running friction log, items #1–22.
