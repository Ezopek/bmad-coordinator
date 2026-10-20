"""Portable entrypoint for an explicitly installed coordinator package."""

import sys

sys.dont_write_bytecode = True

from coordinator_core.__main__ import main


if __name__ == "__main__":
    sys.exit(main())
