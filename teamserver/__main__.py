"""Entry point: python3 -m teamserver <command>."""
import sys

from .cli import main

sys.exit(main())
