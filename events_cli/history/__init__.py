"""Durable event history — the store-assigned cursor the drain replays from.

*Retained messages are not history.* A retained MQTT message gives a new
subscriber the last value on a topic; it is not a replayable log. This package
is the log: what a drain persists before it acknowledges, and what ``events
get`` / ``events list`` read back.

The cursor
----------
The unit of replay is a **store-assigned, per-subscription, monotonic
sequence** returned by :meth:`HistoryStore.append`. It is deliberately *not*
derived from the event id. ``evt_`` ids are Crockford ULIDs: sortable to
millisecond granularity, with 80 random bits below that and **no
intra-millisecond monotonicity**. Two events published in the same millisecond
therefore have an arbitrary relative id order, so any cursor built on comparing
ids would silently reorder or skip events under exactly the load that matters —
a 50 Hz producer. The sequence is the store's, assigned under a lock at append
time, and that is what makes ``read(sub, since=cursor)`` exact.

Where it lives
--------------
Beside the per-host stack state, never relative to the working directory:
``$XDG_CONFIG_HOME/events-cli/history`` (or ``~/.config/…``), overridable with
``EVENTS_HISTORY_DIR`` — the same shape as
:func:`events_cli.stack.default_stack_dir` and ``EVENTS_STACK_DIR``. History is
machine state that belongs to one broker, so ``events list`` answers the same
from any directory. The convention is mirrored rather than imported: the store
has nothing to do with how the broker is deployed, and
``tests/test_history.py`` pins the two paths together so they cannot drift.

What is guaranteed
------------------
* **Ordered.** ``read`` returns records in append order for the subscription,
  and cursor resume is exact.
* **Idempotent on ``id``.** Delivery is at-least-once (QoS 1), so a redelivered
  event returns the sequence it already holds and writes nothing. Dedupe is per
  subscription: one event matching two subscriptions occupies a sequence in
  both, because each owns its own cursor.
* **Bounded.** Every read takes a ``max``, defaulting to :data:`DEFAULT_MAX`.
  There is no "unbounded" sentinel — ``max=0`` is an error, not "everything" —
  because an unbounded read is what hangs an agent turn.
* **Versioned.** Every record carries ``storeFormatVersion`` from the first
  write, so a future migration has something to key on.
* **Stdlib only.** Nothing here imports a transport client, docker, or
  :mod:`events_cli.cli`, so the store keeps its coverage in the default
  dockerless test selection and the no-install introspection lane is untouched.

The seam
--------
:class:`HistoryStore` is a thin façade that validates arguments and delegates
to a :class:`~events_cli.history.backend.HistoryBackend`. Argument policy lives
here so no two backends can disagree about it, and swapping the shipped
:class:`~events_cli.history.jsonl.JsonlBackend` for a sibling store later is a
substitution rather than a rewrite — which is what the store evaluation
(``docs/decisions/2026-07-24-history-store-evaluation.md``) reserved room for.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from events_cli.core.envelope import Envelope
from events_cli.history.backend import (
    STORE_FORMAT_VERSION,
    SUPPORTED_STORE_FORMATS,
    HistoryBackend,
    HistoryPage,
    HistoryRecord,
)
from events_cli.history.errors import (
    HistoryCorruptError,
    HistoryError,
    HistoryFormatError,
    InvalidSubscriptionError,
)
from events_cli.history.jsonl import JsonlBackend

__all__ = [
    "DEFAULT_MAX",
    "HISTORY_DIR_ENV",
    "STORE_FORMAT_VERSION",
    "SUPPORTED_STORE_FORMATS",
    "HistoryBackend",
    "HistoryCorruptError",
    "HistoryError",
    "HistoryFormatError",
    "HistoryPage",
    "HistoryRecord",
    "HistoryStore",
    "InvalidSubscriptionError",
    "append",
    "default_history_dir",
    "get",
    "list_events",
    "open_store",
    "read",
]

#: Environment override for the store root, mirroring ``EVENTS_STACK_DIR``.
HISTORY_DIR_ENV = "EVENTS_HISTORY_DIR"

#: Default page size for every read. Finite by policy: the agent-facing verbs
#: this backs (``events watch --max``, ``events list --max``) must never
#: default to work that has no end.
DEFAULT_MAX = 100

#: A subscription name is also a directory name. Lowercase slug, no separators,
#: no leading dot — which is what keeps a caller-supplied (or broker-supplied)
#: name from addressing anything outside the store.
_SUB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_SUB_NAME_HINT = (
    "use a lowercase slug of at most 64 characters: letters, digits, '.', '_' "
    "and '-', starting with a letter or digit (e.g. 'reachy-mini')"
)


def default_history_dir() -> Path:
    """Where history lives when no root is given.

    Under ``$XDG_CONFIG_HOME`` (or ``~/.config``) beside the generated broker
    stack, never the current working directory: one broker per host is the
    deployment model, and its history is the same kind of machine state.
    ``EVENTS_HISTORY_DIR`` overrides it.
    """
    override = os.environ.get(HISTORY_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "events-cli" / "history"


def _require_sub(sub: str) -> str:
    if not isinstance(sub, str) or not _SUB_NAME_RE.match(sub):
        raise InvalidSubscriptionError(
            f"invalid subscription name: {sub!r}", remediation=_SUB_NAME_HINT
        )
    return sub


def _require_max(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HistoryError(
            f"max must be a positive integer, got {value!r}",
            remediation=f"pass a bound, e.g. max={DEFAULT_MAX}; there is no unbounded read",
        )
    return value


def _require_since(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoryError(
            f"since must be a non-negative cursor, got {value!r}",
            remediation="pass 0 to read from the beginning, or the cursor a previous read returned",
        )
    return value


class HistoryStore:
    """The history store: append, drain from a cursor, and read back by id/type.

    Thin by design. Everything it does beyond delegating is argument policy —
    subscription-name safety and the read bounds — which belongs above the
    backend seam so that every backend enforces exactly the same contract.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        backend: HistoryBackend | None = None,
        fsync: bool = False,
    ) -> None:
        if backend is None:
            backend = JsonlBackend(default_history_dir() if root is None else root, fsync=fsync)
        self._backend = backend

    @property
    def root(self) -> Path:
        """The store's root directory on disk."""
        return self._backend.root

    @property
    def backend(self) -> HistoryBackend:
        """The backend this store delegates to (the swappable half)."""
        return self._backend

    def append(self, envelope: Envelope, sub: str) -> int:
        """Store ``envelope`` under subscription ``sub``; return its sequence.

        The sequence is store-assigned and monotonic within ``sub``. Appending
        an event whose ``id`` the subscription already holds is a no-op that
        returns the sequence already assigned, so an at-least-once redelivery
        never duplicates a fact or advances a cursor twice.

        The envelope is validated first: the store is a persistence boundary,
        and a log it cannot read back is worse than a rejected write.
        """
        return self._backend.append(envelope, _require_sub(sub))

    def read(self, sub: str, since: int = 0, max: int = DEFAULT_MAX) -> HistoryPage:
        """Up to ``max`` records of ``sub`` after cursor ``since``.

        ``since=0`` reads from the beginning. The returned
        :attr:`HistoryPage.cursor` is what to pass back next time; on an empty
        page it is ``since`` unchanged, so a drain loop never moves backwards.
        """
        return self._backend.read(_require_sub(sub), _require_since(since), _require_max(max))

    def get(self, id: str) -> HistoryRecord | None:
        """The record for event ``id``, from whichever subscription holds it."""
        return self._backend.get(id)

    def list(self, type: str | None = None, max: int = DEFAULT_MAX) -> tuple[HistoryRecord, ...]:
        """The most recent ``max`` events, newest first, optionally by type.

        An event view rather than a delivery view: an event held by several
        subscriptions is reported once.
        """
        return self._backend.list(type, _require_max(max))

    def subscriptions(self) -> tuple[str, ...]:
        """Every subscription this store holds history for, sorted."""
        return self._backend.subscriptions()


def open_store(
    root: Path | str | None = None,
    *,
    backend: HistoryBackend | None = None,
    fsync: bool = False,
) -> HistoryStore:
    """Open the store at ``root``, defaulting to :func:`default_history_dir`."""
    return HistoryStore(root, backend=backend, fsync=fsync)


# --- module-level convenience ---------------------------------------------
#
# These resolve the per-host default store on every call, so they answer
# identically from any working directory (and pick up an EVENTS_HISTORY_DIR
# change without a restart). Callers doing more than one operation should hold
# an `open_store()` handle instead: it caches the dedupe set between appends.


def append(envelope: Envelope, sub: str) -> int:
    """Append to the per-host default store. See :meth:`HistoryStore.append`."""
    return open_store().append(envelope, sub)


def read(sub: str, since: int = 0, max: int = DEFAULT_MAX) -> HistoryPage:
    """Read from the per-host default store. See :meth:`HistoryStore.read`."""
    return open_store().read(sub, since, max)


def get(id: str) -> HistoryRecord | None:
    """Look up an event in the per-host default store by id."""
    return open_store().get(id)


def list_events(type: str | None = None, max: int = DEFAULT_MAX) -> tuple[HistoryRecord, ...]:
    """List recent events in the per-host default store.

    Named ``list_events`` rather than ``list`` only because a module-level
    ``list`` would shadow the builtin for every reader of this file; the method
    on :class:`HistoryStore`, where no shadowing is possible, is ``list``.
    """
    return open_store().list(type, max)
