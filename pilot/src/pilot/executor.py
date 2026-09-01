"""The action-executor seam.

Pilot's gate, action call, and verify step route through `ActionExecutor` —
nothing touches `_restart_cmd` or a hand-built spec. The live implementation is
`sconixcore.ManifestExecutor`, wired in `run.py` with `resolve_target` (which
maps a watched target to its `sconix.yaml` + cwd). Tests pass a small fake.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from sconixcore import ActionSpec, Decision, ExecutionResult, Principal

# `sconixcore.ExecutionResult` already has .ok / .argv / .output / .duration_ms —
# that is the result contract Pilot reads.
ExecResult = ExecutionResult


@runtime_checkable
class ActionExecutor(Protocol):
    """What Pilot needs from an action source. `sconixcore.ManifestExecutor`
    satisfies this from a target's `sconix.yaml`."""

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
        shell. Raises `KeyError` for an undeclared target/action."""
        ...
