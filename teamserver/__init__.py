"""Marketing Studio team server: one small stdlib-only server per team (see PROTOCOL.md)."""
import os as _os

PACKAGE_VERSION = "0.1.0"  # used when the repository's VERSION file is not next to the package
PROTOCOL = 1


def _release_version():
    """The release this server was shipped with: the repository's VERSION file (also copied into the image)."""
    try:
        with open(_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "VERSION")) as f:
            v = f.read().strip()
    except OSError:
        return PACKAGE_VERSION
    parts = v.split(".")
    return v if len(parts) == 3 and all(p.isdigit() for p in parts) else PACKAGE_VERSION


__version__ = _release_version()
