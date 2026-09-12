"""Create the digital-twin schema. Idempotent -- safe to re-run."""

import sys

from twin.memory.db import apply_schema
from twin.memory.store import get_profile


def main() -> int:
    apply_schema()
    profile = get_profile()
    print("Schema applied.")
    print(
        "Profile row ready (staleness threshold: {0} days, onboarded: {1}).".format(
            profile["staleness_threshold_days"],
            "yes" if profile["onboarded_at"] else "no",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
