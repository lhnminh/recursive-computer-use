"""Create the MongoDB collections, validators and indexes used by the harness.

All definitions live in ``recursive_computer_use.schema``. This script is a
thin entry point kept for the README's setup step.
"""

from __future__ import annotations

from recursive_computer_use.schema import main


if __name__ == "__main__":
    main()
