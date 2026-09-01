"""The seam where Sconix's shared action executor plugs in.

Pilot's gate and verify step never touch `_restart_cmd` or an `ActionSpec`
directly — they go through the `ActionExecutor` Protocol below. Today it's
satisfied by `LocalExecutor` (one action, from `pilot.act`). Slice 4 swaps in
`sconixcore`'s manifest-backed executor in one line — see
`PILOT_SLICE4_ADAPTER.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sconixcore import ActionSpec

from pilot import act


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    argv: tuple[str, ...]
    output: str
    duration_ms: int


@runtime_checkable
class ActionExecutor(Protocol):
    """What Pilot needs from an action source. `sconixcore` will satisfy this
    from a project's `sconix.yaml`; `LocalExecutor` satisfies it from `pilot.act`
    until then."""

    def lookup(self, name: str) -> ActionSpec | None:
        """The declared action spec, or None if the project doesn't declare it."""
        ...

    async def execute(self, name: str, *, target: str, **args: Any) -> ExecResult:
        """Bind args into the action's argv (no shell) and run it. Raises
        `KeyError` if the action isn't declared."""
        ...


class LocalExecutor:
    """Throwaway shim: one action (`restart_app`) from `pilot.act.RESTART`, argv
    from `pilot.act._restart_cmd` (honours a target's `restart:` override).
    Replaced by `sconixcore`'s manifest executor at slice 4."""

    def __init__(self) -> None:
        self._actions = {act.RESTART.name: act.RESTART}

    def lookup(self, name: str) -> ActionSpec | None:
        return self._actions.get(name)

    async def execute(self, name: str, *, target: str, **args: Any) -> ExecResult:
        if self.lookup(name) is None:
            raise KeyError(f"undeclared action {name!r}")
        argv = tuple(act._restart_cmd(target))
        out = await act.restart_app(target)
        return ExecResult(
            ok=not out.startswith("restart failed"),
            argv=argv,
            output=out,
            duration_ms=0,  # LocalExecutor doesn't split this out; sconixcore will
        )


DEFAULT: ActionExecutor = LocalExecutor()
