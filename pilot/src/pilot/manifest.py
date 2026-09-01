"""Map a watched target to its Sconix project — manifest + execution cwd.

`ManifestExecutor(resolve=resolve_target)` calls this. A target in the active
``targets.yaml`` names its manifest and root explicitly:

    - name: relnotes
      url: https://...
      manifest: ~/systems/apps/relnotes/sconix.yaml
      root: ~/systems/apps/relnotes

Absent an explicit `manifest:`, it falls back to
``~/systems/apps/<name>/sconix.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from pilot import act

APPS = Path.home() / "systems" / "apps"


def _targets_path() -> Path:
    if act.TARGETS_PATH is None:
        raise RuntimeError("act.TARGETS_PATH is not set — run via pilot.run")
    return act.TARGETS_PATH


def resolve_target(target: str) -> tuple[Mapping[str, Any], Path]:
    """Return ``(manifest, root)`` for a watched target. Raises KeyError if the
    target isn't in the active targets file or has no manifest."""
    targets = yaml.safe_load(_targets_path().read_text()).get("targets", [])
    entry = next((t for t in targets if t.get("name") == target), None)
    if entry is None:
        raise KeyError(f"no target named {target!r} in {_targets_path()}")

    base = _targets_path().parent
    manifest_path = entry.get("manifest")
    if manifest_path:
        mpath = Path(manifest_path).expanduser()
        if not mpath.is_absolute():
            mpath = base / mpath
        root_val = entry.get("root")
        root = Path(root_val).expanduser() if root_val else mpath.parent
        if not root.is_absolute():
            root = base / root
    else:
        mpath = APPS / target / "sconix.yaml"
        root = APPS / target

    if not mpath.exists():
        raise KeyError(f"no manifest for target {target!r} at {mpath}")
    return yaml.safe_load(mpath.read_text()), root.resolve()
