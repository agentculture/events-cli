"""The shipped backend: an append-only JSONL log per subscription.

Chosen in ``docs/decisions/2026-07-24-history-store-evaluation.md`` after both
sibling stores were measured and rejected — neither has a store-assigned
sequence, neither has a bounded read-since-cursor, and the files backend's
read-modify-write put is O(N) per append (O(N^2) to ingest), which a 50 Hz
producer crosses within minutes. This is deliberately an *ordered log*, not a
database, and it stays small so that adopting a sibling later is a substitution
behind :class:`~events_cli.history.backend.HistoryBackend`.

On-disk layout
--------------
Per subscription, under ``<root>/subs/<sub>/``::

    log.jsonl   one record per line — the only source of truth
    index.bin   16 bytes per committed record: <QQ (start, end) byte span
    ids.txt     one event id per committed record, in sequence order
    .lock       advisory lock, held only across an append

Both sidecars are **derived and disposable**: delete them and the next write
rebuilds them from the log. What they buy:

* ``index.bin`` is fixed width, so ``read(sub, since, max)`` seeks straight to
  ``16 * since`` and reads exactly the bytes of the requested batch. Resuming
  from a cursor never scans the log, however long it has grown, and appending
  never rewrites a byte that is already on disk.
* ``ids.txt`` holds only ids, so restoring the dedupe set costs one bulk read
  of a compact file rather than parsing every stored envelope.

Commit protocol and crash safety
--------------------------------
An append writes, in order: the log line (flush), the id line, then the index
entry. **The index entry is the commit marker** — a record is visible to
readers only once its span is in ``index.bin``, and the span is written after
the bytes it describes. A reader therefore cannot see a half-written record,
even while a writer is appending, without taking any lock at all.

Recovery runs in the write path, under the lock, and only ever decides the fate
of the *uncommitted* tail; committed records are never touched. A complete,
parseable record found beyond the index is committed (it was fully written; the
crash merely beat the marker). Anything left over — a torn line, or bytes that
do not parse — is truncated away, because nothing has ever read it. Sidecar
rewrites go through a temp sibling plus :func:`os.replace`, so a crash during a
rebuild leaves the previous sidecar intact.

Concurrency
-----------
Appends take an advisory ``flock`` on ``.lock`` for the whole assign-and-write
critical section, and the sequence is read from the on-disk index inside it —
never from a counter cached in memory. Two processes (or two handles, or two
threads) appending to one subscription therefore cannot be assigned the same
sequence, and each refreshes its dedupe set from disk under the lock.

The honest limits of that:

* ``flock`` is **advisory and POSIX-only**. On a platform without :mod:`fcntl`
  the lock degrades to a no-op and the store falls back to the assumption the
  arc is designed around anyway — *one writer per subscription* — which the
  broker enforces in practice, because a second drainer of the same durable
  subscription takes the MQTT session over rather than running alongside it.
* ``flock`` semantics over NFS and some network filesystems are unreliable; the
  store is per-host machine state (it lives beside the stack), so that is out
  of scope rather than solved.
* Readers take no lock. That is safe for *data* by the commit-marker rule
  above, but a read concurrent with an append may miss records committed
  microseconds later — a cursor drain re-reads from its cursor, so nothing is
  lost, only deferred.
"""

from __future__ import annotations

import json
import os
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from events_cli.core.envelope import Envelope, now_rfc3339
from events_cli.history.backend import HistoryPage, HistoryRecord
from events_cli.history.errors import HistoryCorruptError

try:  # pragma: no cover - the fallback only runs off POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]

__all__ = ["JsonlBackend"]

LOG_FILENAME = "log.jsonl"
INDEX_FILENAME = "index.bin"
IDS_FILENAME = "ids.txt"
LOCK_FILENAME = ".lock"
SUBS_DIRNAME = "subs"

#: One index entry: the start and end byte offsets of a record in the log.
_ENTRY_FORMAT = "<QQ"
_ENTRY_WIDTH = struct.calcsize(_ENTRY_FORMAT)

#: How many records a whole-log walk pulls per batch. Bounds the memory a
#: reverse ``list`` scan or a sidecar rebuild needs, whatever the log's size.
_SCAN_CHUNK = 64


class JsonlBackend:
    """An append-only JSONL history log rooted at ``root``.

    ``fsync`` is off by default. Every write is ``flush``ed, so records survive
    the *process* dying — which is the failure this arc actually designs for
    (an MQTT session takeover kills the drainer, not the machine). Surviving a
    power cut means an ``fsync`` per append, which a 50 Hz producer cannot
    afford, so it is the caller's explicit choice rather than a default cost.
    """

    def __init__(self, root: Path | str, *, fsync: bool = False) -> None:
        self.root = Path(root).expanduser()
        self._fsync = fsync
        # Guards this handle's caches. Cross-process exclusion is the flock.
        self._guard = threading.RLock()
        self._ids: dict[str, dict[str, int]] = {}
        self._ids_offset: dict[str, int] = {}

    # -- paths -------------------------------------------------------------

    def _sub_dir(self, sub: str) -> Path:
        return self.root / SUBS_DIRNAME / sub

    def _log(self, sub: str) -> Path:
        return self._sub_dir(sub) / LOG_FILENAME

    def _index(self, sub: str) -> Path:
        return self._sub_dir(sub) / INDEX_FILENAME

    def _ids_file(self, sub: str) -> Path:
        return self._sub_dir(sub) / IDS_FILENAME

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def _count(self, sub: str) -> int:
        """How many records are **committed** for ``sub``."""
        return self._size(self._index(sub)) // _ENTRY_WIDTH

    # -- the append path ---------------------------------------------------

    def append(self, envelope: Envelope, sub: str) -> int:
        envelope.validate()
        directory = self._sub_dir(sub)
        directory.mkdir(parents=True, exist_ok=True)
        with self._guard, self._lock(directory):
            self._recover(sub)
            known = self._dedupe_map(sub)
            existing = known.get(envelope.id)
            if existing is not None:
                # At-least-once delivery: a redelivered event is the same fact
                # and must not take a second sequence.
                return existing

            seq = self._count(sub) + 1
            record = HistoryRecord(
                seq=seq,
                subscription=sub,
                recorded_at=now_rfc3339(),
                envelope=envelope,
            )
            blob = (
                json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            start = self._size(self._log(sub))

            self._append_bytes(self._log(sub), blob)
            self._append_bytes(self._ids_file(sub), f"{envelope.id}\n".encode("utf-8"))
            # The commit marker, written last and after the bytes it describes.
            self._append_bytes(
                self._index(sub), struct.pack(_ENTRY_FORMAT, start, start + len(blob))
            )

            known[envelope.id] = seq
            self._ids_offset[sub] = self._size(self._ids_file(sub))
            return seq

    def _append_bytes(self, path: Path, blob: bytes) -> None:
        with path.open("ab") as handle:
            handle.write(blob)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())

    @contextmanager
    def _lock(self, directory: Path) -> Iterator[None]:
        """Hold the per-subscription advisory lock for one append."""
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        handle = os.open(directory / LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

    # -- dedupe ------------------------------------------------------------

    def _dedupe_map(self, sub: str) -> dict[str, int]:
        """``{event id: seq}`` for ``sub``, refreshed from disk incrementally.

        Only the bytes appended to ``ids.txt`` since this handle last looked
        are read, so a long-lived writer pays the full read once and O(1)
        afterwards — while still seeing ids another writer committed.
        """
        known = self._ids.setdefault(sub, {})
        lines, offset = self._read_ids(sub, self._ids_offset.get(sub, 0))
        for line in lines:
            known.setdefault(line, len(known) + 1)
        self._ids_offset[sub] = offset

        count = self._count(sub)
        if len(known) != count:
            # ids.txt and the commit log disagree (a crash between the two
            # writes, or a sidecar someone edited). The log is the truth.
            self._rebuild_ids(sub)
            known = self._ids[sub]
        return known

    def _read_ids(self, sub: str, offset: int) -> tuple[list[str], int]:
        """Ids appended after ``offset``, plus the new end offset."""
        path = self._ids_file(sub)
        size = self._size(path)
        if size <= offset:
            return [], size
        with path.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read(size - offset)
        text = blob.decode("utf-8", errors="replace")
        # A trailing fragment is an id whose line is not yet complete; leave it
        # for the next refresh rather than reading half an id.
        complete, _, remainder = text.rpartition("\n")
        lines = [line for line in complete.split("\n") if line]
        return lines, size - len(remainder.encode("utf-8"))

    def _rebuild_ids(self, sub: str) -> None:
        """Re-derive ``ids.txt`` from the committed records, atomically.

        Walked in chunks rather than read whole: the sidecar is disposable and
        may have to be rebuilt over a log of any size, so the repair path must
        not need the entire history resident to run.
        """
        count = self._count(sub)
        ids: list[str] = []
        first = 1
        while first <= count:
            take = min(_SCAN_CHUNK, count - first + 1)
            ids.extend(record.envelope.id for record in self._records(sub, first, take))
            first += take
        self._replace_file(
            self._ids_file(sub), "".join(f"{event_id}\n" for event_id in ids).encode("utf-8")
        )
        self._ids[sub] = {event_id: seq for seq, event_id in enumerate(ids, start=1)}
        self._ids_offset[sub] = self._size(self._ids_file(sub))

    @staticmethod
    def _replace_file(path: Path, blob: bytes) -> None:
        """Write ``blob`` to ``path`` via a temp sibling and :func:`os.replace`.

        The rename is atomic, so a crash mid-rebuild leaves the *previous*
        sidecar in place rather than a half-written one.
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

    # -- recovery ----------------------------------------------------------

    def _recover(self, sub: str) -> None:
        """Reconcile the log with its index. Called under the lock, on write.

        Committed records are never touched. Everything here is about the tail
        the index does not yet describe.
        """
        index = self._index(sub)
        size = self._size(index)
        ragged = size % _ENTRY_WIDTH
        if ragged:  # half an index entry survived the crash
            os.truncate(index, size - ragged)
            size -= ragged

        committed_end = self._entry(sub, size // _ENTRY_WIDTH - 1)[1] if size else 0
        log_size = self._size(self._log(sub))
        if log_size < committed_end:
            # The index describes bytes that are not there. Only the log can be
            # trusted; rebuild the index over it from scratch.
            self._replace_file(index, b"")
            self._invalidate(sub)
            committed_end = 0
        if log_size == committed_end:
            return
        self._commit_tail(sub, committed_end)

    def _commit_tail(self, sub: str, committed_end: int) -> None:
        """Index every complete record past ``committed_end``; drop the rest."""
        with self._log(sub).open("rb") as handle:
            handle.seek(committed_end)
            tail = handle.read()

        entries = bytearray()
        offset = committed_end
        # The piece after the final newline is either empty (the tail ended
        # cleanly) or a torn line that was cut short — never a record.
        for chunk in tail.split(b"\n")[:-1]:
            try:
                payload = json.loads(chunk)
            except ValueError:
                break  # uncommitted garbage; nothing has ever read it
            if not isinstance(payload, dict) or "event" not in payload:
                break
            entries += struct.pack(_ENTRY_FORMAT, offset, offset + len(chunk) + 1)
            offset += len(chunk) + 1

        if entries:
            self._append_bytes(self._index(sub), bytes(entries))
        if offset < committed_end + len(tail):
            os.truncate(self._log(sub), offset)
        self._invalidate(sub)

    def _invalidate(self, sub: str) -> None:
        self._ids.pop(sub, None)
        self._ids_offset.pop(sub, None)

    # -- the read path -----------------------------------------------------

    def read(self, sub: str, since: int, max: int) -> HistoryPage:
        count = self._count(sub)
        if since >= count:
            return HistoryPage(records=(), cursor=since, has_more=False)
        wanted = count - since
        if wanted > max:
            wanted = max
        records = self._records(sub, since + 1, wanted)
        cursor = records[-1].seq if records else since
        return HistoryPage(records=records, cursor=cursor, has_more=cursor < count)

    def _entry(self, sub: str, position: int) -> tuple[int, int]:
        """The (start, end) span of the record at zero-based ``position``."""
        with self._index(sub).open("rb") as handle:
            handle.seek(position * _ENTRY_WIDTH)
            blob = handle.read(_ENTRY_WIDTH)
        return struct.unpack(_ENTRY_FORMAT, blob)  # type: ignore[return-value]

    def _spans(self, sub: str, first_seq: int, wanted: int) -> list[tuple[int, int]]:
        if wanted <= 0:
            return []
        with self._index(sub).open("rb") as handle:
            handle.seek((first_seq - 1) * _ENTRY_WIDTH)
            blob = handle.read(wanted * _ENTRY_WIDTH)
        return [
            struct.unpack_from(_ENTRY_FORMAT, blob, offset)
            for offset in range(0, len(blob) - len(blob) % _ENTRY_WIDTH, _ENTRY_WIDTH)
        ]

    def _records(self, sub: str, first_seq: int, wanted: int) -> tuple[HistoryRecord, ...]:
        """Parse ``wanted`` records starting at ``first_seq``, in one log read."""
        spans = self._spans(sub, first_seq, wanted)
        if not spans:
            return ()
        base = spans[0][0]
        with self._log(sub).open("rb") as handle:
            handle.seek(base)
            blob = handle.read(spans[-1][1] - base)

        log = self._log(sub)
        records = []
        for offset, (start, end) in enumerate(spans):
            seq = first_seq + offset
            chunk = blob[start - base : end - base]
            origin = f"{log}:{seq}"
            try:
                payload = json.loads(chunk)
            except ValueError as exc:
                raise HistoryCorruptError(
                    f"{origin}: record is not valid JSON ({exc})",
                    remediation=_repair_hint(log),
                ) from exc
            record = HistoryRecord.from_dict(payload, origin=origin)
            if record.seq != seq:
                raise HistoryCorruptError(
                    f"{origin}: record claims sequence {record.seq}, index says {seq}",
                    remediation=_repair_hint(log),
                )
            records.append(record)
        return tuple(records)

    def _reverse(self, sub: str) -> Iterator[HistoryRecord]:
        """Every record of ``sub``, newest first, read in bounded chunks."""
        high = self._count(sub)
        while high > 0:
            low = high - _SCAN_CHUNK + 1
            if low < 1:
                low = 1
            yield from reversed(self._records(sub, low, high - low + 1))
            high = low - 1

    # -- cross-store views -------------------------------------------------

    def subscriptions(self) -> tuple[str, ...]:
        directory = self.root / SUBS_DIRNAME
        if not directory.is_dir():
            return ()
        return tuple(
            sorted(child.name for child in directory.iterdir() if (child / LOG_FILENAME).is_file())
        )

    def get(self, id: str) -> HistoryRecord | None:
        for sub in self.subscriptions():
            count = self._count(sub)
            ids, _ = self._read_ids(sub, 0)
            for position, event_id in enumerate(ids[:count]):
                if event_id == id:
                    return self._records(sub, position + 1, 1)[0]
        return None

    def list(self, type: str | None, max: int) -> tuple[HistoryRecord, ...]:
        # The global newest-N is always a subset of the union of the per-sub
        # newest-N, so bounding each subscription's walk is safe.
        candidates: list[HistoryRecord] = []
        for sub in self.subscriptions():
            taken = 0
            for record in self._reverse(sub):
                if type is not None and record.envelope.type != type:
                    continue
                candidates.append(record)
                taken += 1
                if taken >= max:
                    break
        candidates.sort(key=lambda r: (r.recorded_at, r.subscription, r.seq), reverse=True)

        # One event may occupy a sequence in several subscriptions; `list` is an
        # event view, not a delivery view, so it reports each event once.
        seen: set[str] = set()
        unique: list[HistoryRecord] = []
        for record in candidates:
            if record.envelope.id in seen:
                continue
            seen.add(record.envelope.id)
            unique.append(record)
            if len(unique) >= max:
                break
        return tuple(unique)


def _repair_hint(log: Path) -> str:
    return (
        f"inspect {log}; the record is damaged on disk. Delete the subscription's "
        "directory to discard its history, or restore the file from a backup"
    )
