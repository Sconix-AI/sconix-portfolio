# Pilot needs from Sconix

_Checkpoint artifact. Pair with `systems/docs/CONSTITUTION.md`,
`systems/docs/GLOSSARY.md`, `systems/schemas/sconix.project.v1.schema.json`._

Pilot is F1 of the portfolio and the first real operational agent on the Sconix
fleet: it watches the deployed apps, diagnoses failures, and — under policy —
restarts them. Everything below is grounded in code that exists and ran, not in
speculation. Each item says what Pilot built locally, why it belongs in the
platform, and the smallest shape that would work.

## What Pilot has proven works

The agent operating loop, end to end, against a real failure (`task demo`):

```
probe → assess (diagnosis + confidence) → open incident → propose action
      → policy gate → act → verify (re-probe) → resolve   |   escalate
```

- Every LLM call is accounted (`AgentRun`: turns, tokens, cost, ms).
- Every proposed action is audited (`Action`: principal, decision, reason, result).
- Incidents carry an explicit, forward-only state machine.
- A cooldown stops the agent from restart-looping a wedged app.
- A `down` (dependency outage) vs `wedged` (stuck process) distinction — the
  agent restarts the second and declines the first on its own.

## Requirements

### R1 — A `Principal`, not a `user_id` string
`run_agent(user_id=...)` assumes a human SaaS user. An ops loop has none; Pilot
passes the literal `"ops-agent:pilot"`. Pilot models it in `pilot/principal.py`:
`kind ∈ {human, coding-agent, ops-agent, ci}`, `id`, `intent`, `scope`.

**Needs:** `run_agent` accepts a `Principal`. `AgentRun` / `Incident` / `Action`
record it. Budget / rate ceilings attach to the principal, not to a per-user
calendar month (`pick_model`'s current model).

### R2 — Guarded execution as a platform contract
`sconixapp.agent.guarded_tool` already exists (extracted at slice 2, one
consumer). The **policy** behind it (`pilot/act.py:make_guard` — allow-set +
cooldown + audit) is Pilot-specific and should **not** be extracted yet.

**Needs:** a tool declares a risk level; before any mutating tool runs,
`run_agent` calls `approve(principal, action, args) -> Decision` where
`Decision ∈ {allow, allow_once, deny(reason), defer}`. Pilot today only needs
`allow | deny`; `allow_once` / `defer` are likely but unproven.

### R3 — Typed actions with metadata
Pilot's one action, `restart_app`, has an implicit contract worth making explicit:

| field | restart_app |
|---|---|
| risk | production-write |
| side effect | in-place container restart, no rebuild |
| preconditions | target assessed warn/down this run; cooldown clear |
| verification | re-probe `/healthz` + `/readyz`, must be 200/ok |
| rollback | n/a (idempotent) |
| idempotent | yes |

**Needs:** an action registry where these are declared, so the gate and the
audit trail read them instead of Pilot hardcoding.

### R4 — An incident lifecycle primitive
`pilot/memory.py` implements: `detected → diagnosed → proposed → approved →
acted → verified → resolved` (+ `escalated`), transitions validated, timestamps
stamped (`acted_at`, `verified_at`, `closed_at`), plus `principal`, `diagnosis`,
`confidence`, `resolution`, `ticks`.

**Needs:** this as a reusable primitive. **Open question:** does it live in
`sconixapp`, a new prod-ops engine, or stay in Pilot until a second operator
exists? The constitution keeps research and production engines separate — an
incident is a production-ops concept, not a research one.

### R5 — Structured memory, sources of truth first
Pilot's memory is a SQL table you can query — not a vector store. Incident
history feeds the assessor as plain text context.

**Needs:** `sconix memory query` / `sconix context --task` read structured
stores (incidents, deploys, decisions, tests) first; semantic search is an index
over that, never the source.

### R6 — Structured (`--json`) output from one service layer
`tick()` returns a list of dicts; the CLI is a thin printer over it. Pilot will
want to consume `sconix inspect --json`, `sconix deploy --plan --json`,
`sconix incidents list --json`. The logic must not live only in the CLI.

### R7 — Verification as a first-class action step
Pilot re-probes the target in the same tick after a restart and only resolves
the incident if it comes back healthy; otherwise it stays `acted` and the next
tick escalates. This is the reference implementation for the "verification"
field in R3.

### R8 — Deploy-safety primitives (slice 4 — not yet built)
Pilot has `sx restart`. It will need `sx canary`, `sx rollback`, and
`sx deploy --plan/--approve <id>`. Per Codex's Phase 4 rule, **Pilot will not
ship autonomous production mutation beyond restart until rollback + a plan/approve
record exist.**

### R9 — A shared secret store for non-app principals
Pilot added `~/systems/secrets.env` (SOPS+age, same rule as apps) +
`pilot/secrets.py` because it has no app `secrets.env`. Sconix should own
`sconix secrets --system` and `sconixapp.secrets.load`.

## Cautions (keep contracts small)

- **Don't extract `make_guard`.** One policy, one action, one consumer. Wait for
  a second operational agent to show the real interface.
- **Don't build an agent-provider adapter layer.** Anthropic is the only
  provider; `run_agent` is enough. Earn it with a second provider.
- **Don't freeze the incident state set** on Pilot alone. Validate against a
  second operator (a deploy pilot, a data-pipeline pilot) first.
- **`Principal.scope`** is a flat target allow-list today. Real RBAC / org
  policy is premature.

## Open questions for the checkpoint

1. Incident lifecycle home: `sconixapp`, new `sconix.ops`, or Pilot for now?
2. `Decision` for v1: `allow | deny` only, or include `allow_once` / `defer`?
3. Budget ceilings attach to: principal, project, or org?
4. Should `sconix.yaml`'s `commands: { deploy: { risk, approval } }` be the
   source the gate reads, replacing Pilot's hardcoded allow-set?
5. Does `AgentRun` (cost/tokens) belong with the incident lifecycle, or stay a
   separate concern that any principal's activity rolls up into?

## Friction log

Full detail in `NOTES.md` (items #1–13). The load-bearing ones for the platform:
#1 (principal), #4 (gate shape), #7 (policy+audit reusability), #9 (system
secrets), #12 (incident close/flap policy).
