# Pilot Slice 4 — the executor seam

How Pilot consumes a manifest-declared action, and the one-line change that
swaps today's local shim for `sconixcore`'s `ManifestExecutor`.

## The seam — `pilot/executor.py`

```python
@dataclass(frozen=True)
class ExecResult:
    ok: bool
    argv: tuple[str, ...]
    output: str  # combined stdout+stderr, trimmed
    duration_ms: int


@runtime_checkable
class ActionExecutor(Protocol):
    def lookup(self, target: str, name: str) -> ActionSpec | None: ...
    async def execute(
        self,
        target: str,
        name: str,
        *,
        principal: Principal,
        decision: Decision | None = None,
        arguments: Mapping[str, str] | None = None,
    ) -> ExecResult: ...
```

- **`lookup(target, name)`** → the action as declared in `target`'s `sconix.yaml`
  (`commands.<name>`), or `None`. Pilot's gate calls this to reject undeclared
  actions and to read `risk` / `approval` / `verification`.
- **`execute(target, name, …)`** → resolve → authorize (scope + approval) → run
  the argv **with no shell**. Raises on undeclared action, bad arguments, scope
  violation, or a missing/denied approval.

Nothing in Pilot touches `_restart_cmd` or a hand-built `ActionSpec` any more —
`make_guard`, `run.tick`'s executor call, and `_verify_recovered` all go through
this Protocol. The action is named **`restart`** (matching the manifest command),
not `restart_app`.

## Today: `LocalExecutor` (throwaway)

One action (`restart`, from `pilot.act.RESTART`), argv from
`pilot.act._restart_cmd(target)` (honours a `restart:` override in
`targets.yaml`). No manifest, no scope/approval enforcement. ~20 lines.

## The swap — `ManifestExecutor` (Codex, `sconixcore`)

`pilot/executor.py` changes one binding:

```python
# from
DEFAULT: ActionExecutor = LocalExecutor()
# to
from sconixcore import ManifestExecutor
DEFAULT: ActionExecutor = ManifestExecutor(resolve=<target -> (manifest, root)>)
```

### What `ManifestExecutor` must do

It wraps `sconixcore`'s function API (`resolve_action` / `authorize_action` /
`execute_action`) behind the Protocol:

| Protocol method | implementation |
|---|---|
| `lookup(target, name)` | `manifest, _ = resolve(target)`; `try: return resolve_action(manifest, name)` `except ActionError: return None` |
| `execute(target, name, *, principal, decision, arguments)` | `manifest, root = resolve(target)`; run `execute_action(manifest=manifest, root=root, name=name, principal=principal, arguments=arguments, decision=decision)` **in a thread** (it's sync); wrap `ExecutionResult` → `ExecResult(ok=res.ok, argv=res.action.argv, output=(res.stdout + res.stderr).strip(), duration_ms=<measured around the runner>)` |

Details that matter:

1. **`resolve`** is a `target -> (manifest: Mapping, root: Path)` callable that
   **Pilot supplies** (from `targets.yaml`; see below). `ManifestExecutor` must
   not read Pilot's files itself.
2. **`decision`** passed to `execute` is already a `sconixcore.Decision` — it
   comes straight from Pilot's `make_guard`. Pass it through to `execute_action`.
   For `approval: policy` actions Pilot's gate produces an `ALLOW` decision; for
   `approval: never` `execute_action` needs no decision; `approval: always`
   Pilot's gate denies before `execute` is ever called.
3. **`arguments`** is the declared-argument map — `{}` for `restart`,
   `{"plan_id": …}` for `deploy`, `{"release": …, "plan_id": …}` for `rollback`.
   `resolve_action` already rejects missing/extra/embedded-placeholder args.
4. **async** — `execute_action`'s `runner` is sync `subprocess.run`. Either wrap
   the whole call in `asyncio.to_thread`, or add an async runner path.
   `duration_ms` is measured by `ManifestExecutor` around the runner call.
5. **No Pilot import.**

### `targets.yaml` gains a manifest pointer

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

## Test coverage → Codex's checklist

| asked for | test (`tests/`) |
|---|---|
| action lookup by name | `test_executor.py::test_lookup_by_name` |
| risk / approval from the manifest | `test_gate_reads_approval_never_and_allows`, `…_always_and_denies` |
| target bound into argv, no shell | `test_execute_binds_target_into_argv_without_a_shell` |
| principal scope enforced | `test_gate_enforces_principal_scope` |
| executor result → verification / audit | `test_executor_result_reaches_the_audit_row`, `test_drill.py::test_verify_recovered_retries_then_gives_up` |
| undeclared action denied / raises | `test_gate_denies_action_not_in_the_manifest`, `test_execute_undeclared_action_raises` |
| failed execution | `test_execute_failed_command_is_not_ok` |
| failed verification | `test_verify_recovered_retries_then_gives_up` |
| successful recovery | `test_memory.py::test_verify_and_resolve_after_action` |

## Deferred to the deploy / rollback integration (not `restart`)

- **Stale / expired approval, one-time consumption** — matters for
  `deploy` / `rollback` (`approval: always`), which Pilot holds until it consumes
  `sx deploy --plan` / `sx approve` / `sx rollback` through this same seam.
- **argv arguments beyond the implicit `{project}`** — `plan_id`, `release`.
