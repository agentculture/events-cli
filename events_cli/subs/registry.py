"""Where subscription records live, and how they are written.

Standard library only, and no broker: the registry is the half of a
subscription that survives every process, and its tests must run on a machine
with nothing installed.

Where it lives
--------------
``<history dir>/registry/`` — *beside* the drained event log rather than in a
directory of its own. The history store already answers "where does this host
keep its event-fabric state?" (``$XDG_CONFIG_HOME/events-cli/history``,
overridable with ``EVENTS_HISTORY_DIR``), and a subscription's record and its
history are two views of the same thing: point ``EVENTS_HISTORY_DIR`` at a
fresh directory and you get a fresh registry with the fresh log it belongs to,
rather than a registry describing subscriptions whose history has vanished.
This is why :func:`default_registry_dir` *calls*
:func:`events_cli.history.default_history_dir` rather than mirroring its logic:
one resolution, so the two can never drift.

Like the store, it is per-host machine state and never CWD-relative — ``events
sub list`` answers the same from any directory.

On-disk layout
--------------
One file per subscription, ``<root>/<name>.json``, with the name as the key::

    <history dir>/registry/robot.json
    <history dir>/registry/reachy-mini.json

Deliberately not one shared file. A shared registry would make every ``sub
add`` a read-modify-write of a document holding *other* subscriptions, so two
concurrent adds could lose one another — and a single damaged byte would take
the whole registry down instead of one record. One file per name makes an add a
single atomic create, a remove a single unlink, and a corrupt record a fault
that names exactly one subscription.

Writes go through a temp sibling plus :func:`os.replace`, the same idiom
:mod:`events_cli.history.jsonl` uses for its sidecars: the rename is atomic, so
a crash mid-write leaves either the previous record or none — never half of
one. The name is validated before it is ever joined onto the root, so a
caller-supplied string cannot address a path outside the registry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from events_cli.history import default_history_dir
from events_cli.subs.errors import (
    DuplicateSubscriptionError,
    RegistryCorruptError,
    UnknownSubscriptionError,
)
from events_cli.subs.record import SubscriptionRecord, check_subscription_name

__all__ = [
    "REGISTRY_DIRNAME",
    "SubscriptionRegistry",
    "default_registry_dir",
    "open_registry",
]

#: The registry's directory name inside the history store root.
REGISTRY_DIRNAME = "registry"

_SUFFIX = ".json"


def default_registry_dir() -> Path:
    """Where subscription records live when no root is given.

    Inside the history store's directory, so ``EVENTS_HISTORY_DIR`` moves the
    registry and the log it describes together. See the module docstring.
    """
    return default_history_dir() / REGISTRY_DIRNAME


class SubscriptionRegistry:
    """The durable set of subscriptions, one JSON file per name.

    ``root`` is the registry directory itself (not the history root), and
    defaults to :func:`default_registry_dir`. Nothing is created until the
    first write: listing an empty registry must not leave a directory behind on
    a host that never registered anything.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(default_registry_dir() if root is None else root).expanduser()

    # -- paths -------------------------------------------------------------

    def _path(self, name: str) -> Path:
        """The record file for ``name``, validated before it touches the root.

        Validation is not decoration here: the name is joined onto a directory
        path, so this is the containment boundary that keeps ``../`` and ``/``
        out of it.
        """
        return self.root / f"{check_subscription_name(name)}{_SUFFIX}"

    # -- reads -------------------------------------------------------------

    def get(self, name: str) -> SubscriptionRecord | None:
        """The record registered under ``name``, or ``None``.

        Raises :class:`SubscriptionValidationError` for an unusable name (which
        could never have been registered) and :class:`RegistryCorruptError` if
        a record exists but cannot be read back.
        """
        path = self._path(name)
        if not path.is_file():
            return None
        return self._load(path)

    def list(self) -> tuple[SubscriptionRecord, ...]:
        """Every registered subscription, sorted by name.

        Sorted rather than in directory order so ``events sub list`` is stable
        between calls and diffable between hosts.
        """
        if not self.root.is_dir():
            return ()
        return tuple(
            self._load(path) for path in sorted(self.root.glob(f"*{_SUFFIX}"), key=lambda p: p.name)
        )

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted — without parsing the records."""
        if not self.root.is_dir():
            return ()
        return tuple(sorted(path.name[: -len(_SUFFIX)] for path in self.root.glob(f"*{_SUFFIX}")))

    def _load(self, path: Path) -> SubscriptionRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RegistryCorruptError(
                f"{path.name}: record is not valid JSON ({exc})",
                remediation=_repair_hint(path),
            ) from exc
        except OSError as exc:
            raise RegistryCorruptError(
                f"{path.name}: record could not be read ({exc})",
                remediation=_repair_hint(path),
            ) from exc
        record = SubscriptionRecord.from_dict(payload, origin=path.name)
        if record.name != path.name[: -len(_SUFFIX)]:
            # The filename is the key `get` looks up by, so a disagreement means
            # a lookup would answer with a record for a different subscription.
            raise RegistryCorruptError(
                f"{path.name}: record names subscription {record.name!r}, "
                "which disagrees with its filename",
                remediation=_repair_hint(path),
            )
        return record

    # -- writes ------------------------------------------------------------

    def add(self, record: SubscriptionRecord) -> SubscriptionRecord:
        """Persist ``record``, refusing a name that is already registered.

        Refused rather than overwritten: the existing record names a broker
        session that is already queueing events under a possibly different
        filter, and silently replacing the record would leave the two
        describing different things.
        """
        path = self._path(record.name)
        if path.exists():
            raise DuplicateSubscriptionError(
                f"subscription {record.name!r} is already registered",
                remediation=(
                    f"choose another name, or remove the existing one first: "
                    f"'events sub remove {record.name}'"
                ),
            )
        self.root.mkdir(parents=True, exist_ok=True)
        _replace_file(
            path,
            (json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return record

    def remove(self, name: str) -> SubscriptionRecord:
        """Delete ``name``'s record and return it.

        Returns the record rather than ``None`` so the caller still holds the
        client id and filter of the session it has just orphaned — which is
        exactly what :func:`events_cli.subs.remove_subscription` needs to
        destroy that session.
        """
        path = self._path(name)
        if not path.is_file():
            raise UnknownSubscriptionError(
                f"no subscription named {name!r}",
                remediation="list what is registered with 'events sub list'",
            )
        record = self._load(path)
        path.unlink()
        return record


def _replace_file(path: Path, blob: bytes) -> None:
    """Write ``blob`` to ``path`` via a temp sibling and :func:`os.replace`.

    The same atomic-write idiom :mod:`events_cli.history.jsonl` uses for its
    sidecars. The rename is atomic, so a crash mid-write leaves the previous
    record intact rather than a truncated one.
    """
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _repair_hint(path: Path) -> str:
    return (
        f"inspect {path}; the record is damaged on disk. Delete the file to drop the "
        "subscription from the registry, then re-add it with 'events sub add'"
    )


def open_registry(root: Path | str | None = None) -> SubscriptionRegistry:
    """Open the registry at ``root``, defaulting to :func:`default_registry_dir`."""
    return SubscriptionRegistry(root)
