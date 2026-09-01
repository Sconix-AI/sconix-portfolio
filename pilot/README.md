# pilot

An agent that watches a small fleet of deployed apps and says what it sees.

This is flagship **F1** of the Sconix portfolio — an agentic system with real
side effects, built on the Sconix Systems engine (`sconixapp`, `sx`, the shared
Caddy edge). It grows one slice at a time; each slice that hurts feeds a fix back
into the engine.

## Where it is now — slice 3: an unattended loop with memory

```
          ┌──────────────────────  watch: every N s  ──────────────────────┐
          ▼                                                                │
targets ─▶ probe() ─raw─▶ assess (haiku) ─▶ verdict ─▶ memory.observe() ───┤
 fleet    /healthz +      + incident        severity   open / bump / close │
          /readyz         history           headline   the target's incident
                                               │                          │
                                    warn/down + --fix ?                    │
                                               │ yes                       │
                                               ▼                           │
                       fix (sonnet) ─ restart_app ─▶ guarded_tool ─────────┘
                                                     policy gate:
                                                       target unhealthy this run
                                                       + no restart in last 10 min
                                                     allow → sx restart   (audited)
                                                     deny  → reason back to model
```

- `probe()` — read-only. `/healthz` + `/readyz`, timed. Never raises.
- `assess` — `run_agent` (haiku), one turn, no tools. Now fed the target's
  **incident history** (`pilot/memory.py`), so "flapping for an hour" ≠ "just now".
- `memory.observe()` — opens an `Incident` when a target first goes non-ok,
  bumps `ticks` while it stays bad, closes it on recovery. A fresh failure after
  recovery is a *new* incident.
- `fix` — only with `--fix`, only for `warn`/`down`. `run_agent` (sonnet) with one
  mutating tool, `restart_app`, wrapped by `sconixapp.agent.guarded_tool`. The
  gate (`pilot/act.py:make_guard`) needs the target unhealthy *this run* **and**
  a clear 10-minute cooldown — a wedged app can't be restart-looped.
- `watch` — `tick()` on a timer, one line per tick, `Ctrl-C` clean.
- Every `run_agent` call writes an `AgentRun` row; every proposed action writes
  an `Action` row. Both in `pilot.db`.

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
tick 1  drill=ok                 baseline
tick 2  drill=down  ← RESTARTED   wedged → agent: "classic transient issue" →
                                  gate ALLOWED → heal ran (34 ms)
tick 3  drill=warn                recovered (pilot notes the flap)
tick 4  drill=down                wedged again, inside cooldown → gate DENIED:
                                  "already restarted within 600s — escalate"

actions (audit trail):  allowed → done (restarted drill) → denied (cooldown)
```

`down` mode (a real dependency outage — `readyz` reports `db: error`) is the
negative case: the agent reads it as an outage and declines to restart on its own.

## Roadmap

| slice | adds | forced into the engine |
|-------|------|------------------------|
| 1 ✅ | probe + assess + per-run accounting | (nothing — proved the seam) |
| 2 ✅ | mutating tool (`restart_app`) + **policy gate** + audit trail | `sconixapp.agent.guarded_tool`; `sx restart` verb |
| 3 ✅ | unattended `watch` loop + incident memory + restart cooldown | (none — proved in pilot; service principal still deferred) |
| 3.5 ✅ | drill harness + `task demo` — down→act→cooldown-deny, end to end | (none — pilot-only) |
| 4 | canary deploy + auto-rollback | `sx canary` / `sx rollback` verbs |
| 5 | public status page | an incident/status package |

## Friction log (→ engine backlog)

Kept in `NOTES.md` as slices land.
