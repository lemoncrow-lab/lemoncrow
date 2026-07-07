"""Refresh src/atelier/infra/model_prices.json from the installed litellm package.

Run this whenever you bump litellm in uv.lock to pull in new model entries:

    uv run --with litellm python scripts/refresh_model_prices.py

The build script (scripts/build.sh) runs this automatically before every
wheel build so released bundles always ship the freshest pricing snapshot.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    import importlib.metadata as _meta

    import litellm

    _litellm_version = _meta.version("litellm")
except ImportError:
    print(
        "litellm is not installed; run: uv run --with litellm python scripts/refresh_model_prices.py", file=sys.stderr
    )
    sys.exit(1)

_SRC = Path(litellm.__file__).parent / "model_prices_and_context_window_backup.json"
_DST = Path(__file__).parent.parent / "src" / "atelier" / "infra" / "model_prices.json"

if not _SRC.exists():
    print(f"Source not found: {_SRC}", file=sys.stderr)
    sys.exit(1)

shutil.copy(_SRC, _DST)
print(f"Updated model_prices.json from litellm {_litellm_version} ({_SRC.stat().st_size // 1024} KB)")
