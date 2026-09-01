"""Who is acting, and under what authority.

`run_agent` wants a `user_id` string; an unattended ops loop has no user. Pilot
models the actor explicitly instead — a `Principal` with a kind, an id, the
intent for this run, and the scope it may touch. Every incident and every action
records the principal that caused it.

This lives in `pilot` on purpose: it's the concrete shape Pilot needs, logged so
Codex can extract the reusable model into Sconix (see PILOT_REQUIREMENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["human", "coding-agent", "ops-agent", "ci"]


@dataclass(frozen=True)
class Principal:
    kind: Kind
    id: str
    intent: str = ""
    scope: tuple[str, ...] = field(default_factory=tuple)  # target names it may act on

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"

    def may_touch(self, target: str) -> bool:
        return not self.scope or target in self.scope


# the default actor for `pilot watch` — an operational agent, no human in the loop
PILOT = Principal(
    kind="ops-agent",
    id="pilot",
    intent="keep the fleet healthy",
)
