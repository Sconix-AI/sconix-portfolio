<!-- goes to sconix-systems/README.md (currently has none) -->

# Sconix Systems

An **agent-native platform** for taking a web app from `new` to production and
operating it safely — by a human or a coding/ops agent, through the same
contracts.

Not a framework. A thin coordination layer over tools that already work
(FastAPI, Next.js, Docker Compose, Postgres, Caddy, Hetzner).

## What it gives you

- **`sconixcore`** — the contracts: a versioned `sconix.yaml` project manifest;
  typed `Principal` (human / agent / service / ci), `ActionSpec`
  (risk · approval · verification · rollback), `Decision`, and a
  `ManifestExecutor` that resolves + authorizes + runs an action's argv with no
  shell.
- **`sconixapp`** — shared backend batteries: config, async DB lifecycle,
  cookie auth, Stripe billing, an agent-run accounting loop, structured logging,
  health/readiness.
- **`sx`** — one CLI: `new` · `dev` · `inspect` · `deploy --plan` ·
  `approve` · `deploy --approve` · `canary` · `promote` · `rollback` ·
  `teardown`. Every mutation is an immutable, attributably-approved, one-time
  plan.
- **the shared edge** — one Caddy per box terminates TLS for every app; apps
  join a network and drop a site file. Many apps, one €6.50/mo VPS.

## Deploy safety

```
sx deploy <app> --plan            # immutable plan: git SHA + host + domain, pinned
sx approve <plan-id> "<reason>"   # a human; one-time, attributable
sx deploy <app> --approve <id>    # rejected if stale / already consumed; verifies health
```

`rollback` preserves the previous route. `canary` runs an isolated stack;
`promote` needs its **own** approval bound to a verified canary; `teardown`
refuses while the canary serves production traffic.

## Proven

Three apps deployed on it with TLS + Postgres + Redis + migrations —
`relnotes` (with the full Stripe billing loop, verified in production),
`skillforge`, `trim`. First real deploy surfaced and fixed 5 template bugs;
each became a test.

## Layout

| path | what |
|---|---|
| `os/sconixcore/` | the contracts + manifest tooling (installable wheel) |
| `os/sconixapp/` | the backend batteries |
| `os/bin/sx` | the CLI |
| `os/install.sh` | clean-machine install |
| `docs/CONSTITUTION.md`, `GLOSSARY.md` | the platform contract |
| `schemas/sconix.project.v1.schema.json` | the manifest schema |
| `apps/` | generated apps (git-ignored) |

The agent that operates apps built with this lives in a separate repo:
**[pilot](https://github.com/Sconix-AI/pilot)**.
