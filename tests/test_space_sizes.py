"""--space must still size a directory holding one entry it cannot read.

du exits 1 in that case but prints the total, and dropping the total hides the
whole item from the report — the direction that makes a big cache look absent.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import collect  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    (base / "big").write_bytes(b"\0" * (8 * 2**20))
    locked = base / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        size = collect._dir_size(base)
    finally:
        locked.chmod(0o700)

ok = size >= 8 * 2**20
print(f"  {'PASS' if ok else 'FAIL'}  sized despite an unreadable child: {size} bytes")
sys.exit(0 if ok else 1)
