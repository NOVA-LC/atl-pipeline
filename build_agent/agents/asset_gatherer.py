"""Asset gatherer — pulls real prospect images + extracts brand palette.

Hard rules per SPEC §3:
- Real-asset ratio must be ≥ 60% (track per file in assets_manifest.json).
- FLUX (Replicate) only as last resort for slots with no real asset.
- Cost ceiling: $0.50 per build.

Step 1 STATUS: stub. Implemented in Step 3.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def gather(research_brief: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Returns assets_manifest dict; writes images to out_dir/."""
    raise NotImplementedError("Step 3 deliverable")
