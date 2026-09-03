#!/usr/bin/env python3
"""Quick lifelog parser using the lifelog module.  Prints sorted lifelog entries."""

import sys

from lifelog import Lifelog, format_entry, read_input


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python parse_lifelog.py <log_file> [...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        label, annotations, data = read_input(arg)
        log = Lifelog.parse(data)

        print(f"Payload timestamp: {log.timestamp}")
        print(
            f"Daily total steps @374: {log.total_steps}  "
            f"distance @378: {log.total_distance}m  "
            f"BCD @396: {log.bcd_total}"
        )

        if log.warnings:
            for w in log.warnings:
                print(f"Warning: {w}")

        if log.committed_distances:
            print(
                f"Committed distance stack @246 (meters, newest first): "
                f"{list(log.committed_distances)}"
            )

        pending = log.pending_intensity
        pending_sum = sum(p for p in pending if p != 0xFFFE)
        if pending_sum:
            print(
                f"Pending walk @382: {list(pending)} (sum {pending_sum})  "
                f"pending distance @392: {log.pending_distance}m"
            )

        entries = log.lifelog_entries()
        if entries:
            print(f"\n--- LIFELOG ENTRIES (sorted, {len(entries)} total) ---")
            for entry in entries:
                print(format_entry(entry))
        print()


if __name__ == "__main__":
    main()
