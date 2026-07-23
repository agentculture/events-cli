"""events_cli — the AgentCulture event fabric.

Three names, deliberately distinct:

* **distribution** ``events-cli`` — what ``pip install`` takes;
* **command** ``events`` — the installed console script (``[project.scripts]``);
* **import package** ``events_cli`` — this module.

The import package is *not* ``events``: the PyPI distribution ``Events`` already
owns that top-level module, so shipping it here would silently clobber one of
the two in any environment holding both.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("events-cli")
except PackageNotFoundError:  # pragma: no cover - editable install without metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
