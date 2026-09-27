"""Regenerate EVAL_REPORT.md (decision, checks and full trace for all 12 cases).

  python scripts/generate_eval_report.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.evals.runner import render_markdown, run_all  # noqa: E402


def main() -> int:
    results = asyncio.run(run_all())
    (ROOT / "EVAL_REPORT.md").write_text(render_markdown(results), encoding="utf-8")
    passed = sum(r.passed for r in results)
    print(f"EVAL_REPORT.md written: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
