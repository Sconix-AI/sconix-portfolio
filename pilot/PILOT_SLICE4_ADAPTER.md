# Pilot Slice 4 — the executor seam (done)

Pilot consumes manifest-declared actions through `sconixcore.ManifestExecutor`
(Systems `5e92d6f`). `_restart_cmd` / `restart_app` are gone; the argv, risk,
approval, and verification for `restart` all come from each target's
`sconix.yaml`. Pilot keeps only its local policy.

## The seam — `pilot/executor.py`

```python
ExecResult = sconixcore.ExecutionResult   # .ok / .argv / .output / .duration_ms

@runtime_checkable
class ActionExecutor(Protocol):
    def lookup(self, target: str, name: str) -> ActionSpec | None: ...
    async def execute(
        self, target: str, name: str, *,
        principal: Principal,
        decision: Decision | None = None,
        arguments: Mapping[str, str] | None = None,
    ) -> ExecResult: ...
```

- **`lookup(target, name)`** → the action as declared in `target`'s `sconix.yaml`
  (`commands.<name>`), or `None`. The gate uses it to reject undeclared actions
  and to read `risk` / `approval` / `verification`.
- **`execute(target, name, …)`** → resolve → authorize (scope + approval/decision)
  → run the argv **with no shell**. Raises `KeyError` for an undeclared
  target/action; `ActionError` for scope / approval failure.

`sconixcore.ManifestExecutor` is the implementation; `tests/conftest.py`'s
`FakeExecutor` is a stub for gate tests. The action is named **`restart`**
(matching the manifest command).

## How it's wired

`run.py`:

```python
from sconixcore import ManifestExecutor
from pilot.manifest import resolve_target

EXECUTOR = ManifestExecutor(resolve=resolve_target)
```

- **`pilot/manifest.py:resolve_target(target) -> (manifest, root)`** — reads the
  active `targets.yaml` (`act.TARGETS_PATH`), loads the target's `manifest:` file
  (relative paths resolve against the targets file's dir), returns the parsed
  dict + `root:` cwd. Falls back to `~/systems/apps/<name>/sconix.yaml`.
- **`make_guard`** calls `executor.lookup(target, "restart")`, denies undeclared
  actions, branches on `spec.approval`, and stashes the `sconixcore.Decision` in
  a `decisions` dict.
- **`run.tick`** calls `EXECUTOR.execute(app, "restart", principal=principal,
  decision=decisions[app])`. `approval: policy` **requires** that Decision —
  `ManifestExecutor.authorize_action` raises `ActionError` without it.
- **`_verify_recovered`** reads `EXECUTOR.lookup(target, "restart").verification`
  for the retry/grace cadence.

### `targets.yaml` — the manifest pointer

```yaml
targets:
  - name: relnotes
    url: https://relnotes.<...>.sslip.io
    manifest: ~/systems/apps/relnotes/sconix.yaml   # or default: ~/systems/apps/<name>/sconix.yaml
    root: ~/systems/apps/relnotes                   # cwd for the argv
```

The drill demo generates a temporary `sconix.yaml` (a `restart` command whose
`run` heals the drill server) alongside its temporary targets file.

## Stays local to Pilot (per checkpoint 1)

- the **cooldown** and the per-run **allow-set** (`ApprovalMode.POLICY` rule);
- the **incident lifecycle** (`pilot/memory.py`);
- the gate rule "`ApprovalMode.ALWAYS` → deny" — Pilot can't self-approve
  `deploy` / `rollback`; those need `sx approve` by a human.

## Test coverage (`tests/`)

| behaviour | test |
|---|---|
| `ManifestExecutor` satisfies the Protocol | `test_executor.py::test_manifest_executor_satisfies_the_protocol` |
| lookup reads the manifest | `test_lookup_reads_the_manifest` |
| declared argv runs, no shell | `test_execute_runs_declared_argv_with_no_shell` |
| undeclared action → `KeyError` / gate deny | `test_execute_undeclared_action_raises`, `test_gate_denies_action_not_in_the_manifest` |
| principal scope enforced (executor + gate) | `test_execute_enforces_principal_scope`, `test_gate_enforces_principal_scope` |
| `approval` from the manifest drives the gate | `test_gate_reads_approval_never_and_allows`, `…_always_and_denies` |
| executor result → audit row | `test_executor_result_reaches_the_audit_row` |
| retry/grace verification gives up | `test_drill.py::test_verify_recovered_retries_then_gives_up` |
| verified recovery resolves the incident | `test_memory.py::test_verify_and_resolve_after_action` |
| cooldown blocks a 2nd action | `test_memory.py::test_restart_cooldown_blocks_second_attempt` |

## Deferred to the deploy / rollback integration (not `restart`)

- **Stale / expired approval, one-time consumption** — matters for
  `deploy` / `rollback` (`approval: always`), which Pilot holds until it consumes
  `sx deploy --plan` / `sx approve` / `sx rollback` through this same seam.
- **argv arguments beyond the implicit `{project}`** — `plan_id`, `release`.
