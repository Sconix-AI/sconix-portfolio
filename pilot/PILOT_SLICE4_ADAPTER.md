# Pilot Slice 4 — the executor seam

How Pilot consumes a manifest-declared action, and the one-line change that
swaps today's local shim for `sconixcore`'s manifest-backed executor once Codex
ships it.

## The seam

`pilot/executor.py` defines everything Pilot's gate and verify step need from an
action source:

```python
@runtime_checkable
class ActionExecutor(Protocol):
    def lookup(self, name: str) -> ActionSpec | None: ...
    async def execute(self, name: str, *, target: str, **args) -> ExecResult: ...


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    argv: tuple[str, ...]
    output: str
    duration_ms: int
```

- **`lookup(name)`** → the declared `sconixcore.ActionSpec` (risk, approval,
  verification, side-effects), or `None` if the project doesn't declare it.
- **`execute(name, target=...)`** → bind arguments into the action's argv
  (**no shell**) and run it; `KeyError` if the action isn't declared.

Nothing in Pilot reaches into `_restart_cmd` or an `ActionSpec` directly any
more — it all goes through this Protocol.

## What flows through it

| call site | uses | for |
|---|---|---|
| `act.make_guard` | `executor.lookup(tool)` | deny undeclared actions; branch on `spec.approval` (`never` → allow, `policy` → Pilot's local rule, `always` → deny) |
| `run.tick._restart_and_record` | `executor.execute("restart_app", target=app)` | run the action, audit `done`/`failed` from `ExecResult.ok` |
| `run._verify_recovered` | `executor.lookup("restart_app").verification` | retry/grace re-probe cadence |

## Today: `LocalExecutor` (throwaway)

- One action: `restart_app`, from `pilot.act.RESTART` (a `sconixcore.ActionSpec`).
- argv from `pilot.act._restart_cmd(target)` — honours a target's `restart:`
  override in `targets.yaml`; otherwise `["sx", "restart", <target>]`.
- ~20 lines, clearly marked. **Not** a general registry or argv engine — Codex
  owns those in `sconixcore`.

## The swap (slice 4)

When Codex's manifest executor lands, `pilot/executor.py` changes one binding:

```python
# from
DEFAULT: ActionExecutor = LocalExecutor()
# to
from sconixcore import ManifestExecutor

DEFAULT: ActionExecutor = ManifestExecutor(project_dir=...)  # reads sconix.yaml
```

For that to be a clean swap, `sconixcore`'s executor must:

1. `lookup(name)` returns the `ActionSpec` built from `sconix.yaml`'s
   `commands.<name>` (`run` → argv, `risk`, `approval`, `verify` →
   `Verification`).
2. `execute(name, target=..., **args)` performs **safe argv substitution** (no
   `shell=True`, no `eval`), runs the argv, returns an `ExecResult` with the
   real `argv`, combined stdout/stderr tail, and `duration_ms`.
3. `execute` raises `KeyError` for an undeclared action (Pilot's gate already
   denies before calling, but `execute` must be safe on its own).
4. No dependency on Pilot — it reads a project directory / manifest object.

Pilot keeps, unchanged and local: the **cooldown**, the per-run **allow-set**,
and the **incident lifecycle** (`pilot/memory.py`).

## Test coverage → Codex's checklist

| Codex asked for | test |
|---|---|
| action lookup by name | `test_executor.py::test_lookup_by_name` |
| risk / approval read from the manifest | `test_gate_reads_approval_never_and_allows`, `test_gate_reads_approval_always_and_denies` |
| target bound safely into argv (no shell) | `test_execute_binds_target_into_argv_without_a_shell` |
| principal scope enforced | `test_gate_enforces_principal_scope` |
| executor result → verification / audit | `test_executor_result_reaches_the_audit_row`, `test_drill.py::test_verify_recovered_retries_then_gives_up` |
| missing / undeclared action denied | `test_gate_denies_action_not_in_the_manifest`, `test_execute_undeclared_action_raises` |
| failed execution | `test_execute_failed_command_is_not_ok` |
| failed verification | `test_verify_recovered_retries_then_gives_up` |
| successful recovery | `test_memory.py::test_verify_and_resolve_after_action` |
| plan denial (undeclared / policy) | `test_gate_denies_action_not_in_the_manifest` |

## Deferred to the deploy/rollback integration (not restart)

- **Stale / expired approval, one-time consumption** — `restart_app` is
  `approval: policy` (the gate *is* the approval). These matter for
  `deploy --plan` / `sx approve` / `rollback`, which Pilot holds until Codex's
  Phase 3 executor commit.
- **`ActionSpec.argv` is still a placeholder** in `pilot.act.RESTART` (NOTES
  #16); the real argv comes from `_restart_cmd`. The manifest executor makes the
  spec's argv authoritative.
- **argv substitution beyond `target`** — deploy/rollback will bind
  `release`, `plan-id`, etc.
