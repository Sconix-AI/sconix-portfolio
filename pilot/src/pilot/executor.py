"""The seam where Sconix's manifest executor plugs in.

Pilot's gate, action call, and verify step route through the `ActionExecutor`
Protocol below — nothing touches `_restart_cmd` or a hand-built `ActionSpec`
directly. Today it's `LocalExecutor` (one action, from `pilot.act`); the swap is
`DEFAULT = <sconixcore ManifestExecutor>` once Codex ships it. See
`PILOT_SLICE4_ADAPTER.md`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sconixcore import ActionSpec, Decision, Principal

from pilot import act


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    argv: tuple[str, ...]
    output: str
    duration_ms: int


@runtime_checkable
class ActionExecutor(Protocol):
    """What Pilot needs from an action source. `sconixcore` satisfies this from
    a target's `sconix.yaml` (`resolve_action` / `execute_action`);
    `LocalExecutor` satisfies it from `pilot.act` until then."""

    def lookup(self, target: str, name: str) -> ActionSpec | None:
        """The action as declared in ``target``'s manifest, or None."""
        ...

    async def execute(
        self,
        target: str,
        name: str,
        *,
        principal: Principal,
        decision: Decision | None = None,
        arguments: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Resolve → authorize (scope + approval/decision) → run the argv with no
        shell. Raises on undeclared action, bad arguments, scope violation, or a
        missing/denied approval."""
        ...


class LocalExecutor:
    """Throwaway shim: one action (`restart`) from `pilot.act.RESTART`, argv from
    `pilot.act._restart_cmd` (honours a target's `restart:` override in
    `targets.yaml`). No manifest, no principal/decision enforcement — that's the
    real `sconixcore` executor's job at slice 4."""

    def lookup(self, target: str, name: str) -> ActionSpec | None:
        return act.RESTART if name == act.RESTART.name else None

    async def execute(
        self,
        target: str,
        name: str,
        *,
        principal: Principal,
        decision: Decision | None = None,
        arguments: Mapping[str, str] | None = None,
    ) -> ExecResult:
        if self.lookup(target, name) is None:
            raise KeyError(f"undeclared action {name!r}")
        argv = tuple(act._restart_cmd(target))
        out = await act.restart_app(target)
        return ExecResult(
            ok=not out.startswith("restart failed"),
            argv=argv,
            output=out,
            duration_ms=0,  # the shim doesn't split this out; sconixcore will
        )


DEFAULT: ActionExecutor = LocalExecutor()
