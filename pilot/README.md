# pilot

An agent that watches a small fleet of deployed apps and says what it sees.

This is flagship **F1** of the Sconix portfolio — an agentic system with real
side effects, built on the Sconix Systems engine (`sconixapp`, `sx`, the shared
Caddy edge). It grows one slice at a time; each slice that hurts feeds a fix back
into the engine.

## Where it is now — slice 2: observe, assess, and (gated) act

```
targets.yaml ─▶ probe() ─raw─▶ assess (haiku) ─▶ verdict ─┐
 (the fleet)    /healthz       sconixapp.agent   severity  │
               /readyz                                     ▼
                                        warn/down + --fix ?
                                                     │ yes
                                                     ▼
                          fix (sonnet) ── restart_app ──▶ guarded_tool
                                                          policy gate  ──▶ allow → sx restart
                                                          (audited)     ──▶ deny  → model gets reason
```

- `probe()` — read-only. Hits `/healthz` + `/readyz`, times them. Never raises.
- `assess` — `run_agent` (haiku), one turn, no tools → `{severity, headline, detail}`.
- `fix` — only with `--fix`, only for `warn`/`down` targets. `run_agent` (sonnet)
  with **one** mutating tool, `restart_app`, wrapped by
  `sconixapp.agent.guarded_tool`. The gate (`pilot/act.py:make_guard`) allows a
  restart only for a target this run assessed as unhealthy; everything the model
  proposes — allowed or denied — is written to the `actions` table in `pilot.db`.
- Every `run_agent` call still writes an `AgentRun` row (turns, tokens, cost, ms).

`restart_app` is the only real side effect, and it cannot fire unless policy says so.

## Run

```bash
task setup
# key comes from ~/systems/secrets.env (SOPS+age); or export ANTHROPIC_API_KEY
task pilot                          # assess all targets
task pilot -- relnotes              # just one
task pilot -- --fix                 # arm restart_app (policy-gated, audited)
task test                           # no network
```

## Roadmap

| slice | adds | forced into the engine |
|-------|------|------------------------|
| 1 ✅ | probe + assess + per-run accounting | (nothing — proved the seam) |
| 2 ✅ | mutating tool (`restart_app`) + **policy gate** + audit trail | `sconixapp.agent.guarded_tool`; `sx restart` verb |
| 3 | autonomous loop + incident memory | a scheduled runner; a first-class service principal |
| 4 | canary deploy + auto-rollback | `sx canary` / `sx rollback` verbs |
| 5 | public status page | an incident/status package |

## Friction log (→ engine backlog)

Kept in `NOTES.md` as slices land.
