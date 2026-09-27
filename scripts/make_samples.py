"""Render the mock documents in samples/ (images, a 2-page PDF, and manifest.json of expected values).

  python scripts/make_samples.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.evals.samples import write_samples  # noqa: E402

if __name__ == "__main__":
    for path in write_samples(ROOT / "samples"):
        print(f"wrote {path.relative_to(ROOT)}")
