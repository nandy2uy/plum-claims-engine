"""Run the 12 test cases and print a pass/fail table.

  python scripts/run_evals.py                      # in-process (no server needed)
  python scripts/run_evals.py --url http://127.0.0.1:8000   # against a running API
                                                   # (server needs ALLOW_FAULT_INJECTION=true for TC011)
Exit code is non-zero if any case fails, so this can gate CI.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.evals.runner import run_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="base URL of a running API; omit for in-process")
    args = parser.parse_args()
    results = asyncio.run(run_all(http_url=args.url))
    for r in results:
        res = r.result
        print(f"{r.case['case_id']}  {'PASS' if r.passed else 'FAIL'}  {str(res.get('decision') or res.get('status')):<20} "
              f"approved={res.get('approved_amount'):<9} conf={res.get('confidence_score')}")
        for c in r.checks:
            if not c.passed:
                print(f"      ✗ {c.name}: {c.detail}")
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
