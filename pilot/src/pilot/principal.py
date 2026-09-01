"""The acting principal — now a thin adapter over `sconixcore.Principal`.

Pilot proved the shape (kind + role + intent + scope); Codex extracted it into
`sconixcore` at Phase 2. This module keeps the `PILOT` default and the helpers
Pilot needs on top: a stable audit label and a scope check.
"""

from __future__ import annotations

from sconixcore import Principal, PrincipalKind

__all__ = ["Principal", "PrincipalKind", "PILOT", "label", "may_touch"]

# the unattended loop: an operational agent, no human in the loop
PILOT = Principal(
    kind=PrincipalKind.AGENT,
    id="pilot",
    role="ops",
    intent="keep the fleet healthy",
)


def label(p: Principal) -> str:
    """Stable string for audit rows — e.g. ``ops-agent:pilot``, ``human:yusuf``."""
    if p.role:
        return f"{p.role}-{p.kind.value}:{p.id}"
    return f"{p.kind.value}:{p.id}"


def may_touch(p: Principal, target: str) -> bool:
    return not p.scope or target in p.scope
