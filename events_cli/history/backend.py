"""The stored record, the read page, and the backend seam.

This module holds everything a *replacement* backend would have to satisfy, and
nothing about how the shipped one works. The split is deliberate: the store
evaluation (``docs/decisions/2026-07-24-history-store-evaluation.md``) rejected
both sibling stores on capability grounds and recorded revisit triggers, so the
one thing this arc must not do is make adopting a sibling later a rewrite.
Everything above this line talks to :class:`HistoryBackend`; everything below
it is :mod:`events_cli.history.jsonl` and is swappable.

The record
----------
A stored record wraps an envelope with three things the envelope cannot carry
itself, because they are facts about *storage*, not about the event:

* ``seq`` — the store-assigned, per-subscription, monotonic cursor. It is
  never derived from the event id: ``evt_`` ULIDs sort only to millisecond
  granularity and carry no intra-millisecond monotonicity, so two events minted
  in the same millisecond have an arbitrary relative order. The sequence is the
  store's, which is why replay from a cursor is exact.
* ``subscription`` — which durable subscription's log this record belongs to.
  The same event may legitimately occupy a sequence in several of them.
* ``recordedAt`` — when the store took delivery, which is not ``event.time``
  (that is when the producer minted it, possibly long before a drain ran).

And ``storeFormatVersion``, written into **every** record from the first write,
so a future migration has something to key on per record rather than having to
infer a format from the shape of what it finds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from events_cli.core.envelope import Envelope
from events_cli.core.errors import EnvelopeValidationError
from events_cli.history.errors import HistoryCorruptError, HistoryFormatError

__all__ = [
    "STORE_FORMAT_VERSION",
    "SUPPORTED_STORE_FORMATS",
    "HistoryBackend",
    "HistoryPage",
    "HistoryRecord",
]

#: The on-disk record format. Bumped only when a record's *shape* changes in a
#: way an older reader would misread; adding an ignored key does not qualify.
STORE_FORMAT_VERSION = "1"

#: Formats this build can read. A record outside this set raises
#: :class:`HistoryFormatError` rather than being silently reinterpreted.
SUPPORTED_STORE_FORMATS: frozenset[str] = frozenset({STORE_FORMAT_VERSION})

_WIRE_VERSION = "storeFormatVersion"
_WIRE_SEQ = "seq"
_WIRE_SUBSCRIPTION = "subscription"
_WIRE_RECORDED_AT = "recordedAt"
_WIRE_EVENT = "event"


_REPAIR_HINT = (
    "the store on disk is damaged; inspect the log under the history directory "
    "(EVENTS_HISTORY_DIR, or $XDG_CONFIG_HOME/events-cli/history by default)"
)


@dataclass(frozen=True)
class HistoryRecord:
    """One event as the store holds it: an envelope plus its cursor position."""

    seq: int
    subscription: str
    recorded_at: str
    envelope: Envelope = field(hash=False)
    store_format_version: str = STORE_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        """The on-disk / ``--json`` form: camelCase, version first."""
        return {
            _WIRE_VERSION: self.store_format_version,
            _WIRE_SEQ: self.seq,
            _WIRE_SUBSCRIPTION: self.subscription,
            _WIRE_RECORDED_AT: self.recorded_at,
            _WIRE_EVENT: self.envelope.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, origin: str = "record") -> "HistoryRecord":
        """Parse a stored record, or raise :class:`HistoryCorruptError`.

        This is the store's trust boundary and mirrors
        :meth:`Envelope.from_dict`: everything read back from disk comes
        through here, and a record that cannot be interpreted is an
        environment fault naming the file it came from — never a traceback and
        never a silently skipped line, because a skipped line would shift every
        later cursor.
        """
        if not isinstance(payload, Mapping):
            raise HistoryCorruptError(
                f"{origin}: a stored record must be a JSON object, "
                f"got {type(payload).__name__}",
                remediation=_REPAIR_HINT,
            )

        version = payload.get(_WIRE_VERSION)
        if version not in SUPPORTED_STORE_FORMATS:
            raise HistoryFormatError(
                f"{origin}: unsupported storeFormatVersion {version!r} "
                f"(this build reads {', '.join(sorted(SUPPORTED_STORE_FORMATS))})",
                remediation=(
                    "the store was written by a newer events-cli; upgrade it "
                    "(pip install -U events-cli) or point EVENTS_HISTORY_DIR at a fresh store"
                ),
            )

        try:
            seq = payload[_WIRE_SEQ]
            subscription = payload[_WIRE_SUBSCRIPTION]
            recorded_at = payload[_WIRE_RECORDED_AT]
            envelope = Envelope.from_dict(payload[_WIRE_EVENT])
        except KeyError as exc:
            raise HistoryCorruptError(
                f"{origin}: stored record is missing {exc.args[0]!r}",
                remediation=_REPAIR_HINT,
            ) from exc
        except EnvelopeValidationError as exc:
            raise HistoryCorruptError(
                f"{origin}: stored record holds an invalid envelope ({exc})",
                remediation=_REPAIR_HINT,
            ) from exc

        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise HistoryCorruptError(
                f"{origin}: stored record has a non-positive sequence {seq!r}",
                remediation=_REPAIR_HINT,
            )
        return cls(
            seq=seq,
            subscription=str(subscription),
            recorded_at=str(recorded_at),
            envelope=envelope,
            store_format_version=str(version),
        )


@dataclass(frozen=True)
class HistoryPage:
    """A bounded batch plus the cursor to resume from.

    ``cursor`` is what the caller passes back as ``since`` next time. On an
    empty page it is the ``since`` that was asked for, so a drain loop that
    stores the cursor unconditionally never moves backwards.
    """

    records: tuple[HistoryRecord, ...]
    cursor: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
            "cursor": self.cursor,
            "hasMore": self.has_more,
        }


@runtime_checkable
class HistoryBackend(Protocol):
    """What a history backend must do. The whole seam, in five methods.

    Argument validation (subscription names, ``max`` bounds) happens *above*
    this in :class:`events_cli.history.HistoryStore`, so a backend may assume
    its inputs are well formed and no two backends can disagree about policy.
    """

    root: Path

    def append(self, envelope: Envelope, sub: str) -> int:
        """Store ``envelope`` under ``sub``; return its sequence.

        Idempotent on :attr:`Envelope.id` within the subscription: a repeat
        returns the sequence already assigned and writes nothing.
        """

    def read(self, sub: str, since: int, max: int) -> HistoryPage:
        """Up to ``max`` records of ``sub`` with a sequence greater than ``since``."""

    def get(self, id: str) -> HistoryRecord | None:
        """The record for event ``id`` from any subscription, or ``None``."""

    def list(self, type: str | None, max: int) -> tuple[HistoryRecord, ...]:
        """The most recent ``max`` events, newest first, optionally by type."""

    def subscriptions(self) -> tuple[str, ...]:
        """Every subscription the store holds a log for, sorted."""
