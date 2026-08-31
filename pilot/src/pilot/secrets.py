"""Load a shared, system-level secret for tooling that isn't tied to one app.

The pilot has no app ``secrets.env`` of its own — it reads Sconix Systems'
shared store at ``~/systems/secrets.env`` (SOPS+age). An already-set environment
variable always wins, so CI / a one-off ``export`` still work.

    from pilot.secrets import load

    load("ANTHROPIC_API_KEY")   # -> puts it in os.environ if not already there

(Backlog: this belongs in ``sconixapp`` as the service-principal counterpart to
per-app secrets — see NOTES.md.)
"""

from __future__ import annotations

import os
import subprocess
from functools import cache
from pathlib import Path

SHARED = Path.home() / "systems" / "secrets.env"
AGE_KEY = Path.home() / "systems" / "os" / ".age" / "keys.txt"
SOPS_CONFIG = Path.home() / "systems" / ".sops.yaml"


@cache
def _decrypt() -> dict[str, str]:
    if not SHARED.exists():
        return {}
    out = subprocess.run(
        ["sops", "-d", "--config", str(SOPS_CONFIG), str(SHARED)],
        capture_output=True,
        text=True,
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(AGE_KEY)},
        check=True,
    ).stdout
    pairs: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        pairs[k.strip()] = v.strip()
    return pairs


def load(name: str, *, required: bool = True) -> str | None:
    """Ensure ``name`` is in ``os.environ`` (from the shared store if needed)."""
    if os.environ.get(name):
        return os.environ[name]
    val = _decrypt().get(name)
    if val:
        os.environ[name] = val
        return val
    if required:
        raise RuntimeError(
            f"{name} not in env and not in {SHARED} — "
            f"export it, or add it via: sx secrets (system-level)"
        )
    return None
