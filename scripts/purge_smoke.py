"""Delete the smoke-test rows already sitting in the live database.

Every run of ``scripts/smoke.py`` registers a device, posts a sighting and queues
a pending product, and nothing ever removed them: the crowd leaderboard's top
three were "smoke-test" devices and the review queue held the "Smoke Test" can.
The markers and the deletion live next to the writes in ``scripts/smoke.py``;
this is the one-off entry point for that backlog, while ``smoke.py --cleanup`` is
what to run after a normal smoke run.

Dry run by default; read the counts, then pass --apply.

    python scripts/purge_smoke.py
    python scripts/purge_smoke.py --apply
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from scripts import smoke  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete the smoke test's rows")
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    args = parser.parse_args(argv)
    return smoke.main(["--cleanup"] + (["--apply"] if args.apply else []))


if __name__ == "__main__":
    raise SystemExit(main())
