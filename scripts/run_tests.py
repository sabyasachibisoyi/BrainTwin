"""One-shot test runner — run the whole tests/ suite from one place.

We accumulated a bunch of test files (test_storage_*.py,
test_migrate_jsonl_to_sql.py, test_main_wiring.py, test_enrichment.py,
…) and running each separately is a chore. This script runs them all
through pytest with sensible defaults, and lets you pass any extra
pytest flags through.

Usage:
    python scripts/run_tests.py                  # run everything verbose
    python scripts/run_tests.py -k storage       # filter by name
    python scripts/run_tests.py -x               # stop on first failure
    python scripts/run_tests.py --tb=short       # shorter tracebacks
    python scripts/run_tests.py tests/test_storage_sync.py  # one file
    python scripts/run_tests.py -s               # show prints / log output

Exit code = pytest's exit code (0 = all green, non-zero = failures).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `backend.*` and `scripts.*` importable so test files don't need
# their own sys.path tweaks. Mirrors what conftest.py does for tests/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import pytest
    except ImportError:
        print("pytest is not installed. Run: pip install -r requirements.txt",
              file=sys.stderr)
        return 2

    args = list(sys.argv[1:])

    # If the user didn't specify any path / file, default to the whole
    # tests/ directory.
    if not any(
        a == "tests" or a.startswith("tests/") or a.startswith("tests\\")
        for a in args
    ):
        args.insert(0, "tests/")

    # Default to verbose unless the user passed -q / --quiet.
    if not any(a in ("-v", "-vv", "-q", "--quiet", "--verbose") for a in args):
        args.append("-v")

    print(f"$ pytest {' '.join(args)}\n", flush=True)
    return pytest.main(args)


if __name__ == "__main__":
    sys.exit(main())
