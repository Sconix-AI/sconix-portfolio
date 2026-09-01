# Pilot — operator runbook (F1)

Pilot watches a fleet of deployed Sconix apps, diagnoses failures, and — under
policy — recovers them. This is the reference for running it and for what to do
when it hands an incident back to you.

## The safety contract (frozen)

| Pilot **may** do autonomously | Pilot **never** does autonomously |
|---|---|
| Probe health, assess, open/track incidents | Approve anything (`sx approve` is a human) |
| `restart` an unhealthy app — policy-gated, cooldowned | `deploy` a new release |
| Propose a `rollback_plan` once a restart is exhausted (with `--allow-rollback`) | `rollback` without a human approval |
| Execute a `rollback` **after** a human `sx approve` | `canary`, `promote`, or `canary_teardown` — ever |
| Verify recovery, resolve or escalate the incident | Switch production traffic |

`AUTONOMOUS_KINDS = {"rollback"}` in `pilot/deploy.py` is the whole list of
plan kinds the loop will ever propose on its own. Everything mutating is either
policy-gated (`restart`) or human-approved (`rollback` execution, and all of
canary/promote/teardown).

Two flags arm autonomy; both are **off by default**:

- `--fix` — allow `restart`.
- `--allow-rollback` — allow proposing a `rollback_plan` (execution still needs
  a human).

## Setup

```
cd ~/portfolio/pilot
task setup

# The pilot's own Anthropic key: from ~/systems/secrets.env (SOPS+age), or
export ANTHROPIC_API_KEY=...

# targets.yaml — one block per watched app:
#   name / url / manifest (its sconix.yaml) / root (cwd for actions)
#   rollback_to:  (optional) the release id an autonomous rollback targets
```

Plan/approval/execution records live in Sconix's plan store —
`$SCONIX_STATE_DIR/deploy/{plans,approvals,executions,completions}` (default
`~/.local/state/sconix`). Pilot **reads** it; it never writes there.

## Running

```
task pilot                                  # one read-only pass over the fleet
task pilot -- relnotes                       # one target
task pilot -- --fix                          # one pass, restart armed
task pilot -- watch --fix --every 60         # unattended, restart armed
task pilot -- watch --fix --allow-rollback   # + propose rollback when restart fails
task demo                                    # end-to-end drill (no real infra)
```

`watch` prints one line per tick and exits cleanly on `Ctrl-C`.

## Incident lifecycle

Every non-healthy target gets one open `Incident` (in `pilot.db`). It moves
forward only; `escalated` means "a human is needed".

```
detected ─▶ diagnosed ─▶ proposed ─▶ approved ─▶ acted ─▶ verified ─▶ resolved
                │            │  \                   │
                │            │   ─▶ awaiting_approval ─▶ approved   (human said yes)
                └─────────── escalated ◀────────────┘
                     (escalated ─▶ proposed  when Pilot proposes a rollback plan)
```

| state | meaning | who moves it |
|---|---|---|
| `detected` | first non-ok probe opened the incident | Pilot |
| `diagnosed` | assessor produced severity + headline + `detail` + `confidence` | Pilot |
| `proposed` | the fix agent picked an action (restart, or a plan) | Pilot |
| `awaiting_approval` | a `*_plan` exists; `resolution` names the `sx approve <id>` command | Pilot parks it; **you** approve |
| `approved` | policy gate cleared a restart, **or** a human approval was found | Pilot / plan store |
| `acted` | the action ran | Pilot |
| `verified` | a follow-up probe confirms healthy (retry/grace from the manifest's `verify`) | Pilot |
| `resolved` | closed — verified, or recovered on its own | Pilot |
| `escalated` | gate refused, plan went stale/failed, or no safe action — **needs a human** | Pilot |

An app that recovers while `acted` closes as `resolved` ("recovered after
action"). An app that recovers on its own closes as "recovered without action".
A fresh failure after `resolved` opens a **new** incident.

## The autonomous loop, per tick

1. **Probe** every target (`/healthz` + `/readyz`, timed). Never raises.
2. **Assess** with the incident history as context → `severity` (`ok`/`warn`/`down`),
   `headline`, `detail`, `confidence`. `warn` also covers "up now but flapping".
3. **Track** — open / bump / verify+resolve the incident.
4. **Restart** (only with `--fix`, only `warn`/`down`): the fix agent may call
   `restart(app)` once. The gate then decides:
   - action must be **declared** in the target's `sconix.yaml` (else deny);
   - `approval: policy` → target unhealthy this run **and** principal in scope
     **and** no restart in the last 10 min (cooldown);
   - `approval: never` → allowed; `approval: always` → denied (needs a human).
   - allowed → run the manifest's argv (no shell) → **verify** (re-probe,
     retry/grace from `verify:`) → `resolved`, else stays `acted`.
5. **Resume** — any `awaiting_approval` incident whose plan a human has since
   approved is executed this tick.
6. **Rollback proposal** (only with `--allow-rollback`): if `should_rollback` —
   a restart already **ran this incident and did not recover it** — Pilot runs
   `rollback_plan` (needs `rollback_to:` in `targets.yaml`), records the plan id,
   and parks the incident on `awaiting_approval`.

Restart is always tried before rollback. Rollback is never the first response.

## The approval boundary

Every mutating plan follows one path:

```
<kind>_plan   (approval: never)   → prints a 20-hex plan id, writes plans/<id>.json
sx approve <id> "<reason>"        → a HUMAN; writes approvals/<id>.json (allow-once)
<kind>        (approval: always)  → verifies the plan is fresh + approved + unconsumed,
                                    runs it, writes executions/<id>.json
```

Pilot performs step 1 (propose) and step 3 (execute, relaying the human's
`allow-once` decision, naming the approver from the record). **Step 2 is always
you.** Pilot has no path to `sx approve`.

## Operator playbook

### Recover a wedged app — nothing to do

The loop restarts it (policy-gated). If the restart clears it, the incident
resolves. If not, and `--allow-rollback` is set, Pilot proposes a rollback and
parks it (below).

### Approve a rollback Pilot proposed

```
# incident is awaiting_approval; its resolution line has the id
sx approve <plan-id> "confirmed bad release <sha>, rolling back to <release>"
# next tick, Pilot executes `rollback --approve <plan-id>` and verifies
```

If you decide *not* to roll back, do nothing — the plan expires; resolve the
incident by fixing forward and letting the next tick see it healthy.

### Canary a fix, then promote it (fully operator-driven)

Pilot does **not** initiate these. You run each `*_plan`, approve it, then run
the apply step (or hand the plan id to Pilot's `execute_approved` if you're
scripting).

```
# 1. stand up an isolated canary
sx canary <app> --plan
sx approve <canary-plan-id> "canary <sha> for incident <n>"
sx canary <app> --approve <canary-plan-id>
#    verify the canary route by hand / point a one-off `pilot ... --targets` at it

# 2. promote — needs its OWN fresh approval, bound to the verified canary
sx promote <app> <canary-plan-id> --plan
sx approve <promote-plan-id> "canary healthy N min, promoting"
sx promote <app> <canary-plan-id> --approve <promote-plan-id>
#    the previous production route is preserved for rollback

# 3. tear the canary down (destructive; refused while it serves prod traffic)
sx teardown <app> <canary-plan-id> --plan
sx approve <teardown-plan-id> "promoted, cleaning up"
sx teardown <app> <canary-plan-id> --approve <teardown-plan-id>
```

## Failure cases

| symptom | what Pilot does | what you do |
|---|---|---|
| transient / wedged process | restart (gated) → verify → `resolved` | nothing |
| restart didn't recover it | next tick: cooldown denies a 2nd restart → `escalated`; with `--allow-rollback`, proposes a rollback plan | approve the rollback, or fix forward |
| `should_rollback` but no `rollback_to:` | `escalated` — "rollback needs a target release" | set `rollback_to:` in `targets.yaml`, or handle by hand |
| dependency outage (readyz reports a dead dep) | assessor calls it `down`; the fix agent **declines** to restart ("a restart won't help") → `escalated` | fix the dependency; a restart/rollback won't help |
| plan approved but stale (SHA/host/domain changed) | `deploy`/`rollback` exits non-zero → recorded `failed` → `escalated` | re-plan against current state |
| plan already consumed | `execute_approved` records `denied` → `escalated` "plan already consumed" | plans are single-use; make a new one |
| approval never comes | incident sits in `awaiting_approval`; no action, no audit spam | approve, or leave it — it will not self-execute |
| flapping (up, but repeated recent incidents) | `warn`; incident stays open across the flap | investigate the underlying instability |
| target outside the principal's scope | gate denies "scope does not include \<target\>" | widen `Principal.scope`, or run with an unscoped principal |

## Database limitations

**A canary runs an isolated data stack. Production database state is NOT copied
into it automatically.** Consequences:

- A canary's `/readyz` passing says the canary's *own* DB/Redis are up — it says
  nothing about production data.
- Do not treat "canary healthy" as "safe against production data". Migrations
  that are destructive or that assume production rows must be validated
  separately before `promote`.
- `promote` switches the edge route to the canary's stack; it does not migrate
  or merge data. `sx teardown` refuses to remove a canary that is currently
  serving production traffic.
- `rollback` restores application code and containers; **database migrations are
  not reversed**.

## Where to look

| what | where |
|---|---|
| incidents + their state, plan id, resolution | `pilot.db` → `incidents` |
| every proposal / allow / deny / done / failed, with principal + plan id | `pilot.db` → `actions` |
| per-LLM-call cost / tokens / duration | `pilot.db` → `agent_runs` |
| plans, approvals, executions, completions | `$SCONIX_STATE_DIR/deploy/*` (`sx ... show`) |
| deploy / restart history for an app | `~/systems/ledger.md` |
