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

__all__ = [
    "ConnectionState",
    "EventClient",
    "MqttDependencyError",
    "PublishResult",
    "Will",
    "__version__",
]

# The publish client lives in :mod:`events_cli.client` and depends on paho-mqtt.
# It is re-exported here lazily via ``__getattr__`` (PEP 562) so that merely
# importing ``events_cli`` never imports the client module — and therefore never
# imports paho. The introspection verbs must keep working from a bare checkout
# with nothing installed, which this preserves: paho is pulled in only when a
# caller actually touches ``events_cli.EventClient`` (and even then only when the
# client is constructed). See events_cli/client.py for the boundary in full.
_CLIENT_EXPORTS = frozenset(
    {"ConnectionState", "EventClient", "MqttDependencyError", "PublishResult", "Will"}
)


def __getattr__(name: str) -> object:
    if name in _CLIENT_EXPORTS:
        from events_cli import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
