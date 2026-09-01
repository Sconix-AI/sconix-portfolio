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
`run_agent` calls `approve(principal, action, args) -> Decision`. The GLOSSARY
`Policy` definition ("permits, denies, constrains, or requires approval") already
covers the four outcomes. Pilot today exercises only `allow | deny`; `constrain`
(allow-once) and `require-approval` (defer) are contract-reserved, unproven.

### R3 — Typed actions with metadata
Pilot's one action, `restart_app`, has an implicit contract worth making
explicit. Risk uses the GLOSSARY enum
(`read-only | local-write | external-write | destructive`):

| field | restart_app |
|---|---|
| risk | `external-write` |
| side effect | in-place container restart on a remote host, no rebuild |
| preconditions | target assessed warn/down this run; cooldown clear; principal in scope |
| verification | re-probe `/healthz` + `/readyz`, must be 200/ok |
| rollback | n/a (idempotent) |
| idempotent | yes |

**Needs:** an action registry where these are declared, so the gate and the
audit trail read them instead of Pilot hardcoding. The v1 `command` schema
already carries `risk` / `approval` / `verify`; it still needs `sideEffect`,
`preconditions`, `rollback`, `idempotent`, and a grace/retry shape on `verify`
(`{checks, within, attempts}` — Pilot's `_settle` re-probes once, immediately,
which is too eager for a slow restart). `run` should be an **argv array**, not a
shell string — `restart_app` already `shlex.split`s to avoid quoting bugs.

### R4 — An incident lifecycle primitive
`pilot/memory.py` implements: `detected → diagnosed → proposed → approved →
acted → verified → resolved` (+ `escalated`), transitions validated, timestamps
stamped (`acted_at`, `verified_at`, `closed_at`), plus `principal`, `diagnosis`,
`confidence`, `resolution`, `ticks`.

**Needs:** this as a reusable primitive. **Resolved at checkpoint 1:** it lives
in **Systems** (production-ops) — the constitution's "Systems handles persistent
products and operations", and it is not a research concept. `pilot/memory.py` is
the reference to extract; validate the state set against a second operator before
freezing it.

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

## Open questions

Resolved at checkpoint 1:

1. ~~Incident lifecycle home~~ → **Systems** (production-ops). See R4.
4. ~~Does `sconix.yaml` `commands.{risk,approval}` replace the hardcoded
   allow-set?~~ → **Yes.** Pilot migrates `make_guard` to read a loaded manifest
   (slice 4+).

Still open for Phase 2:

2. `Decision` for v1: ship `allow | deny` only, or land all four
   (`allow / constrain / deny / require-approval`) in the type now?
3. Budget / rate ceilings attach to: principal, project, or org?
5. Does `AgentRun` (cost/tokens) belong with the incident lifecycle, or stay a
   separate concern that any principal's activity rolls up into?
6. `Principal.kind` vocabulary: Pilot has `{human, coding-agent, ops-agent, ci}`;
   GLOSSARY has agent/service. Proposal: `kind ∈ {human, agent, service, ci}` +
   optional `role` (`coding`, `ops`).

## Friction log

Full detail in `NOTES.md` (items #1–15). The load-bearing ones for the platform:
#1 (principal), #4 (gate shape), #7 (policy+audit reusability), #9 (system
secrets), #12 (incident close/flap policy), #14 (acted-but-errored),
#15 (verify grace/retry).
