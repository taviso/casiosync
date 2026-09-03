#!/usr/bin/env python3
"""Golden regression test for the lifelog parser.

Regenerates the canonical ``lifelog ...`` entry lines for every dump in
``logs/`` and diffs them against ``tests/golden.txt``. Run from the repo root:

    python tests/golden_test.py            # verify (exit 1 on any diff)
    python tests/golden_test.py --update   # rewrite the golden file
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifelog import Lifelog, format_entry, read_input  # noqa: E402

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
GOLDEN = Path(__file__).resolve().parent / "golden.txt"


def generate() -> str:
    """Return the canonical snapshot text for every dump in logs/."""
    lines = [
        "# Golden lifelog entry lines for logs/*.txt.",
        "# Regenerate with: python tests/golden_test.py --update",
    ]
    for path in sorted(LOGS_DIR.glob("*.txt")):
        _label, _annotations, data = read_input(str(path))
        log = Lifelog.parse(data)
        lines.append(f"===== {path.name} =====")
        lines.extend(format_entry(entry) for entry in log.lifelog_entries())
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true", help="rewrite the golden file"
    )
    args = parser.parse_args()

    current = generate()
    if args.update:
        GOLDEN.write_text(current, encoding="utf-8")
        print(f"wrote {GOLDEN} ({len(current.splitlines())} lines)")
        return 0

    expected = GOLDEN.read_text(encoding="utf-8")
    if current == expected:
        print(f"OK: {len(current.splitlines())} lines match {GOLDEN.name}")
        return 0

    diff = difflib.unified_diff(
        expected.splitlines(),
        current.splitlines(),
        fromfile=str(GOLDEN),
        tofile="current",
        lineterm="",
    )
    sys.stdout.write("\n".join(diff) + "\n")
    print("FAIL: output differs from golden (run with --update to accept)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
