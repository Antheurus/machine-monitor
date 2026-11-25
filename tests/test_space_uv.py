"""--space must find the uv cache wherever uv itself says it lives.

The list hardcoded ~/Library/Caches/uv, but uv on macOS defaults to ~/.cache/uv,
so an 18 GB cache was absent from the report on the machine it was built for.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import collect  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    uv_dir = Path(tmp) / "elsewhere" / "uv"
    uv_dir.mkdir(parents=True)
    (uv_dir / "archive.bin").write_bytes(b"\1" * (60 * 2**20))

    collect.shutil.which = lambda name: "/usr/bin/uv" if name == "uv" else None
    real_run = collect.run
    collect.run = lambda cmd, timeout=5.0: f"{uv_dir}\n" if cmd[:3] == ["uv", "cache", "dir"] else real_run(cmd, timeout)

    items = collect.reclaimable({"project_roots": []})
    hit = [i for i in items if i.path == str(uv_dir)]

ok = len(hit) == 1 and hit[0].bytes >= 60 * 2**20 and hit[0].note == "uv cache prune"
print(f"  {'PASS' if ok else 'FAIL'}  uv cache found at the path `uv cache dir` reports: {[(i.path, i.bytes, i.note) for i in hit]}")
sys.exit(0 if ok else 1)
