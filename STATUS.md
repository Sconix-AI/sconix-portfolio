# Status — 2026-09-01

## Done

**F1 `pilot/`** — autonomous ops agent, slices 1 → 4d complete, **autonomous
behaviour frozen**. 45 tests, lint clean, pushed to
`github.com/YusufRM/sconix-portfolio`.

- probe → assess (LLM diagnosis + confidence) → incident state machine
- `restart` — policy-gated + 10-min cooldown, verified recovery, then resolve/escalate
- restart exhausted → propose `rollback_plan` → `awaiting_approval` → human
  `sx approve` → execute. Pilot never approves anything.
- `canary` / `promote` / `canary_teardown` — operator-initiated only
  (`AUTONOMOUS_KINDS = {"rollback"}`); Pilot relays the approved execution.
- everything runs from each app's `sconix.yaml` via `sconixcore.ManifestExecutor`
- `pilot report` — read-only history (`--json`); `RUNBOOK.md` — operator guide
- `tests/test_integration.py` — full chain offline (no live server, no LLM)

## In flight (Codex, `~/systems`)

- ✅ installer, `sx` portable, local `task -d os` factory-acceptance gate
  (`73fa712`, template v0.7.1)
- ⏳ **disposable-server lifecycle test** — deploy → canary → promote → rollback
  → teardown on a throwaway box. This is the last factory gate.

## Waiting on Yusuf

1. **First official factory product** — Codex suggests **Relnotes**. Confirm or name another.
2. **GitHub reorg** (decided, deferred until one clean factory-acceptance run):
   - new Sconix GitHub org; extract `pilot` to its own repo; `sconix-portfolio`
     → thin index
   - audit the 39 repos (public today = SNHU coursework only) →
     keep-pin / polish / archive
   - profile README
   - ⚠️ `annotated_deep_learning_paper_im…` is labml.ai's repo — never public
     under Yusuf's name
3. **F2** (post-training on the 5090) — not started; deferred until after F2
   packaging + the disposable-server dry run.

## Next actions when unblocked

- Codex passes the disposable-server gate → publish a versioned Sconix release +
  install guide.
- Then: GitHub reorg, then F2.
- Optional now: draft `FACTORY_ACCEPTANCE.md` from Pilot's side (what an
  acceptance run should assert about an agent-ready generated project).
