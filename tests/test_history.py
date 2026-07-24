"""Contract tests for the history store — the cursor correctness core.

The store is what turns "events were delivered" into "events can be replayed
from an exact point". Everything asserted here is a promise the drain (#7) and
the ``events get``/``events list`` verbs are built on, so these tests are the
executable form of the store contract rather than incidental coverage.

Every test runs with **no docker and no broker**, which is enforced
mechanically by ``test_history_imports_only_the_standard_library`` — the store
is stdlib-only by design, so it keeps its coverage in the default (dockerless)
pytest selection.

Two properties get disproportionate attention because they are the ones a
plausible-looking implementation gets wrong:

* **ordering is never derived from comparing event ids.** ``evt_`` ids are
  Crockford ULIDs, sortable only to millisecond granularity with no
  intra-millisecond monotonicity, so two events minted in the same millisecond
  have an arbitrary relative sort order. The cursor is a *store-assigned*
  sequence; the tests below mint ids in one frozen millisecond and prove read
  order equals append order even when the ULID order is the exact reverse.
* **the store is per-host, never CWD-relative.** ``events list`` must answer
  identically from any directory.
"""

from __future__ import annotations

import ast
import json
import os
import struct
import sys
import threading
from pathlib import Path

import pytest

from events_cli.core import Envelope, EnvelopeValidationError, EventsError, new_event_id
from events_cli.history import (
    DEFAULT_MAX,
    HISTORY_DIR_ENV,
    STORE_FORMAT_VERSION,
    HistoryCorruptError,
    HistoryError,
    HistoryFormatError,
    HistoryPage,
    HistoryRecord,
    HistoryStore,
    InvalidSubscriptionError,
    append,
    default_history_dir,
    get,
    list_events,
    open_store,
    read,
)
from events_cli.stack import STACK_DIR_ENV, default_stack_dir

# --- helpers ---------------------------------------------------------------

# A millisecond to freeze the id clock at. Any fixed value works; this one is
# simply a readable 2026 timestamp in milliseconds.
FROZEN_MS = 1_785_000_000_000
FROZEN_NS = FROZEN_MS * 1_000_000

#: A ULID's first 10 Crockford characters carry the 48-bit millisecond stamp
#: (10 * 5 = 50 bits: two zero pad bits plus the 48). Slicing past the ``evt_``
#: prefix therefore isolates "which millisecond was this id minted in".
_ID_PREFIX_LEN = len("evt_")
_ULID_TIME_CHARS = 10


def timestamp_part(event_id: str) -> str:
    """The millisecond-stamp portion of a generated event id."""
    return event_id[_ID_PREFIX_LEN : _ID_PREFIX_LEN + _ULID_TIME_CHARS]


def event(n: int = 0, *, type: str = "task.requested", id: str | None = None) -> Envelope:
    """A valid envelope, distinguishable by ``data.n``."""
    return Envelope.new(type, "agent://builder", data={"n": n}, id=id)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history")


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the id clock so every minted id lands in one millisecond."""
    monkeypatch.setattr("events_cli.core.envelope.time_ns", lambda: FROZEN_NS)


def sub_dir(store: HistoryStore, sub: str) -> Path:
    return store.root / "subs" / sub


def log_path(store: HistoryStore, sub: str) -> Path:
    return sub_dir(store, sub) / "log.jsonl"


def index_path(store: HistoryStore, sub: str) -> Path:
    return sub_dir(store, sub) / "index.bin"


def ids_path(store: HistoryStore, sub: str) -> Path:
    return sub_dir(store, sub) / "ids.txt"


def index_entries(store: HistoryStore, sub: str) -> list[tuple[int, int]]:
    """The committed (start, end) byte spans, straight off the index file."""
    raw = index_path(store, sub).read_bytes()
    width = struct.calcsize("<QQ")
    return [struct.unpack_from("<QQ", raw, off) for off in range(0, len(raw), width)]


# --- location: per-host, never CWD-relative --------------------------------


def test_default_history_dir_follows_the_stack_xdg_convention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(HISTORY_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_history_dir() == tmp_path / "events-cli" / "history"


def test_default_history_dir_falls_back_to_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(HISTORY_DIR_ENV, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_history_dir() == tmp_path / ".config" / "events-cli" / "history"


def test_default_history_dir_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(HISTORY_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_history_dir() == tmp_path / "elsewhere"


def test_the_env_override_expands_a_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(HISTORY_DIR_ENV, "~/store")
    assert default_history_dir() == tmp_path / "store"


def test_the_default_store_sits_beside_the_per_host_stack_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """History and stack are siblings under one per-host events-cli root.

    Pinned against ``default_stack_dir`` itself rather than a literal path, so
    that moving the stack's convention moves the store's with it — the store
    deliberately mirrors the pattern instead of importing the broker layer.
    """
    monkeypatch.delenv(HISTORY_DIR_ENV, raising=False)
    monkeypatch.delenv(STACK_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_history_dir().parent == default_stack_dir().parent
    assert default_history_dir() != default_stack_dir()


def test_default_history_dir_is_absolute_and_not_under_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.delenv(HISTORY_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    resolved = default_history_dir()
    assert resolved.is_absolute()
    assert not resolved.is_relative_to(workdir)


def test_the_store_answers_identically_from_two_different_cwds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The store-level form of "``events list`` answers the same anywhere".

    The CLI verb lands in a later task; what makes it CWD-independent is this:
    the module-level entry points resolve the same per-host root regardless of
    where the process happens to be standing, and drop nothing into the CWD.
    """
    monkeypatch.setenv(HISTORY_DIR_ENV, str(tmp_path / "store"))
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    monkeypatch.chdir(first)
    seq = append(event(1), "builder")
    assert seq == 1
    from_first = list_events(type="task.requested")
    page_first = read("builder", since=0, max=10)
    by_id_first = get(from_first[0].envelope.id)

    monkeypatch.chdir(second)
    assert list_events(type="task.requested") == from_first
    assert read("builder", since=0, max=10) == page_first
    assert get(from_first[0].envelope.id) == by_id_first

    # And nothing was written beside either working directory.
    assert list(first.iterdir()) == []
    assert list(second.iterdir()) == []


def test_open_store_without_a_root_uses_the_per_host_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(HISTORY_DIR_ENV, str(tmp_path / "store"))
    assert open_store().root == tmp_path / "store"


# --- the store-format marker ----------------------------------------------


def test_every_record_carries_the_store_format_version_from_the_first_write(
    store: HistoryStore,
) -> None:
    store.append(event(1), "builder")
    line = log_path(store, "builder").read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    assert payload["storeFormatVersion"] == STORE_FORMAT_VERSION
    assert store.read("builder").records[0].store_format_version == STORE_FORMAT_VERSION


def test_the_stored_record_keeps_the_envelope_in_its_wire_form(store: HistoryStore) -> None:
    envelope = event(1)
    store.append(envelope, "builder")
    payload = json.loads(log_path(store, "builder").read_text(encoding="utf-8").splitlines()[0])
    assert payload["event"] == envelope.to_dict()
    assert payload["seq"] == 1
    assert payload["subscription"] == "builder"
    assert payload["recordedAt"]


def test_a_record_written_by_a_newer_store_format_is_rejected(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    payload = json.loads(log_path(store, "builder").read_bytes()[start:end])
    payload["storeFormatVersion"] = "9"
    _overwrite_record(store, "builder", start, end, payload)

    with pytest.raises(HistoryFormatError) as exc:
        store.read("builder")
    assert "'9'" in str(exc.value)
    assert exc.value.remediation


def _overwrite_record(store: HistoryStore, sub: str, start: int, end: int, payload: object) -> None:
    """Replace a committed record in place, padding to its exact byte span.

    In place and same-length so the index keeps pointing at it — that is what
    makes these corruption tests exercise the *read* path rather than the
    recovery path.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    raw = text.encode("utf-8")
    span = end - start - 1  # the record's bytes, excluding its newline
    assert len(raw) <= span, "test payload does not fit the original record"
    blob = raw + b" " * (span - len(raw)) + b"\n"
    with log_path(store, sub).open("r+b") as handle:
        handle.seek(start)
        handle.write(blob)


# --- the store-assigned cursor --------------------------------------------


def test_append_returns_a_monotonic_per_subscription_sequence(store: HistoryStore) -> None:
    assert [store.append(event(n), "builder") for n in range(5)] == [1, 2, 3, 4, 5]


def test_sequences_are_independent_per_subscription(store: HistoryStore) -> None:
    assert store.append(event(1), "builder") == 1
    assert store.append(event(2), "watcher") == 1
    assert store.append(event(3), "builder") == 2
    assert store.append(event(4), "watcher") == 2


def test_the_sequence_continues_across_a_reopen(tmp_path: Path) -> None:
    root = tmp_path / "history"
    HistoryStore(root).append(event(1), "builder")
    assert HistoryStore(root).append(event(2), "builder") == 2


def test_two_ids_minted_in_the_same_millisecond_keep_append_order_and_exact_resume(
    store: HistoryStore, frozen_clock: None
) -> None:
    """The honesty condition, verbatim: same millisecond, order and resume exact.

    Two ids minted inside one frozen millisecond share their whole timestamp
    prefix, so their relative sort order is decided by 80 random bits — i.e. it
    is arbitrary. Read order must still be append order, and resuming from the
    returned cursor must deliver exactly the second event and nothing else.
    """
    first, second = event(1), event(2)
    assert timestamp_part(first.id) == timestamp_part(second.id)

    store.append(first, "builder")
    store.append(second, "builder")

    page = store.read("builder", since=0, max=10)
    assert [record.envelope.id for record in page.records] == [first.id, second.id]
    assert [record.seq for record in page.records] == [1, 2]

    resumed = store.read("builder", since=1, max=10)
    assert [record.envelope.id for record in resumed.records] == [second.id]
    assert resumed.cursor == 2
    assert store.read("builder", since=resumed.cursor, max=10).records == ()


def test_read_order_is_append_order_even_when_the_ulid_order_is_reversed(
    store: HistoryStore, frozen_clock: None
) -> None:
    """The load-bearing negative: no ordering may be derived from id comparison.

    Ten ids are minted in one millisecond and appended in the exact reverse of
    their lexicographic order. An implementation that sorted, ranged or
    binary-searched on the id would return them backwards; the store-assigned
    sequence returns them as appended.
    """
    envelopes = sorted((event(n) for n in range(10)), key=lambda e: e.id, reverse=True)
    assert len({timestamp_part(e.id) for e in envelopes}) == 1
    assert [e.id for e in envelopes] != sorted(e.id for e in envelopes)

    for envelope in envelopes:
        store.append(envelope, "builder")

    page = store.read("builder", since=0, max=10)
    assert [record.envelope.id for record in page.records] == [e.id for e in envelopes]


def test_cursor_resume_is_exact_at_every_point(store: HistoryStore, frozen_clock: None) -> None:
    """Draining one at a time from the returned cursor visits each event once."""
    envelopes = [event(n) for n in range(6)]
    for envelope in envelopes:
        store.append(envelope, "builder")

    seen: list[str] = []
    cursor = 0
    while True:
        page = store.read("builder", since=cursor, max=2)
        if not page.records:
            break
        seen.extend(record.envelope.id for record in page.records)
        cursor = page.cursor
    assert seen == [envelope.id for envelope in envelopes]
    assert cursor == 6


def test_read_is_bounded_by_max(store: HistoryStore) -> None:
    for n in range(10):
        store.append(event(n), "builder")
    page = store.read("builder", since=0, max=3)
    assert len(page.records) == 3
    assert page.cursor == 3
    assert page.has_more is True


def test_the_last_page_reports_no_more(store: HistoryStore) -> None:
    for n in range(3):
        store.append(event(n), "builder")
    page = store.read("builder", since=0, max=100)
    assert len(page.records) == 3
    assert page.has_more is False
    assert page.cursor == 3


def test_read_defaults_to_a_finite_max(store: HistoryStore) -> None:
    assert DEFAULT_MAX == 100
    for n in range(DEFAULT_MAX + 5):
        store.append(event(n), "builder")
    page = store.read("builder")
    assert len(page.records) == DEFAULT_MAX
    assert page.has_more is True


def test_read_rejects_a_non_positive_max(store: HistoryStore) -> None:
    """There is deliberately no "unbounded" sentinel — that is the bound."""
    for bad in (0, -1):
        with pytest.raises(HistoryError):
            store.read("builder", since=0, max=bad)


def test_read_rejects_a_negative_cursor(store: HistoryStore) -> None:
    with pytest.raises(HistoryError):
        store.read("builder", since=-1)


def test_read_of_an_unknown_subscription_is_an_empty_page(store: HistoryStore) -> None:
    page = store.read("never-used", since=0, max=10)
    assert page == HistoryPage(records=(), cursor=0, has_more=False)


def test_a_cursor_past_the_end_returns_an_empty_page(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    page = store.read("builder", since=99, max=10)
    assert page.records == ()
    assert page.cursor == 99
    assert page.has_more is False


# --- dedupe on the event id -----------------------------------------------


def test_append_dedupes_on_event_id(store: HistoryStore) -> None:
    envelope = event(1)
    assert store.append(envelope, "builder") == 1
    assert store.append(envelope, "builder") == 1
    assert len(store.read("builder", since=0, max=10).records) == 1


def test_redelivery_with_a_bumped_delivery_attempt_still_dedupes(store: HistoryStore) -> None:
    """QoS 1 redelivery is the case this exists for: same id, changed metadata."""
    import dataclasses

    envelope = event(1)
    store.append(envelope, "builder")
    store.append(dataclasses.replace(envelope, delivery_attempt=2), "builder")
    assert len(store.read("builder", since=0, max=10).records) == 1


def test_dedupe_keys_on_id_not_on_payload(store: HistoryStore) -> None:
    """Two facts with identical payloads are two facts, not a duplicate.

    This is the failure mode that disqualified the content-hash dedupe in the
    evaluated sibling store: it destroyed a distinct event.
    """
    first = Envelope.new("sensor.read", "app://reachy", data={"temp": 42})
    second = Envelope.new("sensor.read", "app://reachy", data={"temp": 42})
    assert first.id != second.id
    store.append(first, "builder")
    store.append(second, "builder")
    ids = [record.envelope.id for record in store.read("builder", since=0, max=10).records]
    assert ids == [first.id, second.id]


def test_dedupe_is_scoped_to_the_subscription(store: HistoryStore) -> None:
    """One event matching two subscriptions belongs in both logs.

    Each subscription owns its own cursor, so an event delivered to both must
    occupy a sequence in both — deduping globally would silently starve the
    second subscriber.
    """
    envelope = event(1)
    assert store.append(envelope, "builder") == 1
    assert store.append(envelope, "watcher") == 1
    assert len(store.read("builder", since=0, max=10).records) == 1
    assert len(store.read("watcher", since=0, max=10).records) == 1


def test_dedupe_survives_reopening_the_store(tmp_path: Path) -> None:
    root = tmp_path / "history"
    envelope = event(1)
    HistoryStore(root).append(envelope, "builder")
    assert HistoryStore(root).append(envelope, "builder") == 1
    assert len(HistoryStore(root).read("builder", since=0, max=10).records) == 1


def test_append_validates_the_envelope(store: HistoryStore) -> None:
    """The store is a persistence boundary; it never writes an invalid fact."""
    bad = Envelope(id="evt_1", type="Task.Requested", source="builder", time="yesterday", data={})
    with pytest.raises(EnvelopeValidationError):
        store.append(bad, "builder")
    assert not log_path(store, "builder").exists()


# --- get / list across the store ------------------------------------------


def test_get_returns_the_stored_record_by_id(store: HistoryStore) -> None:
    envelope = event(1)
    store.append(envelope, "builder")
    record = store.get(envelope.id)
    assert record is not None
    assert record.envelope == envelope
    assert record.seq == 1
    assert record.subscription == "builder"


def test_get_is_none_for_an_unknown_id(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    assert store.get("evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4") is None


def test_get_finds_an_event_stored_under_any_subscription(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    envelope = event(2)
    store.append(envelope, "watcher")
    record = store.get(envelope.id)
    assert record is not None
    assert record.subscription == "watcher"


def test_list_filters_by_type(store: HistoryStore) -> None:
    wanted = event(1, type="task.requested")
    store.append(wanted, "builder")
    store.append(event(2, type="scope.completed"), "builder")
    records = store.list(type="task.requested", max=10)
    assert [record.envelope.id for record in records] == [wanted.id]


def test_list_without_a_type_returns_everything(store: HistoryStore) -> None:
    for n in range(3):
        store.append(event(n), "builder")
    assert len(store.list(max=10)) == 3


def test_list_is_newest_first_and_bounded(store: HistoryStore) -> None:
    envelopes = [event(n) for n in range(5)]
    for envelope in envelopes:
        store.append(envelope, "builder")
    records = store.list(max=2)
    assert [record.envelope.id for record in records] == [envelopes[4].id, envelopes[3].id]


def test_list_defaults_to_a_finite_max(store: HistoryStore) -> None:
    for n in range(DEFAULT_MAX + 3):
        store.append(event(n), "builder")
    assert len(store.list()) == DEFAULT_MAX


def test_list_rejects_a_non_positive_max(store: HistoryStore) -> None:
    with pytest.raises(HistoryError):
        store.list(max=0)


def test_list_returns_an_event_once_even_when_two_subscriptions_hold_it(
    store: HistoryStore,
) -> None:
    envelope = event(1)
    store.append(envelope, "builder")
    store.append(envelope, "watcher")
    records = store.list(max=10)
    assert [record.envelope.id for record in records] == [envelope.id]


def test_list_spans_subscriptions(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    store.append(event(2), "watcher")
    assert len(store.list(max=10)) == 2


def test_list_is_deterministic_across_repeated_calls(store: HistoryStore) -> None:
    """Determinism must survive a *fresh reader*, not just a repeated call.

    ``store.list() == store.list()`` is nearly vacuous — nothing mutates
    between the two calls, so it passes even when the order is an accident of
    in-memory state. Reading the same root through a second store instance is
    the assertion that bites: it fails if ordering ever depends on anything but
    what is on disk (a raw ``glob`` order, say, rather than the sequence).
    """
    for n in range(4):
        store.append(event(n), "builder" if n % 2 else "watcher")

    first = store.list(max=10)
    reopened = HistoryStore(store.root).list(max=10)

    assert [(r.subscription, r.seq) for r in reopened] == [(r.subscription, r.seq) for r in first]
    assert len(first) == 4


def test_subscriptions_are_reported_sorted(store: HistoryStore) -> None:
    store.append(event(1), "watcher")
    store.append(event(2), "builder")
    assert store.subscriptions() == ("builder", "watcher")


def test_an_empty_store_lists_nothing(store: HistoryStore) -> None:
    assert store.list(max=10) == ()
    assert store.subscriptions() == ()
    assert store.get("evt_01JZ8QK3W6X7Y8Z9A0B1C2D3E4") is None


# --- subscription names ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape", "a/b", "", ".", "..", "UPPER", "with space", "-leading", "x" * 65],
)
def test_a_subscription_name_that_could_escape_the_store_is_rejected(
    store: HistoryStore, name: str
) -> None:
    envelope = event(1)
    with pytest.raises(InvalidSubscriptionError):
        store.append(envelope, name)
    with pytest.raises(InvalidSubscriptionError):
        store.read(name)


def test_a_rejected_subscription_name_creates_nothing_on_disk(store: HistoryStore) -> None:
    envelope = event(1)
    with pytest.raises(InvalidSubscriptionError):
        store.append(envelope, "../escape")
    assert not (store.root.parent / "escape").exists()


@pytest.mark.parametrize("name", ["builder", "b", "reachy-mini", "run.42", "a_b", "x" * 64])
def test_ordinary_subscription_names_are_accepted(store: HistoryStore, name: str) -> None:
    assert store.append(event(1), name) == 1


# --- append-only, atomic, crash-safe --------------------------------------


def test_the_log_is_append_only(store: HistoryStore) -> None:
    """A later append never rewrites earlier bytes — the O(1)-append property.

    The evaluated sibling store failed here: every put was a read-modify-write
    of the whole file, which is what produced its quadratic ingest curve.
    """
    store.append(event(1), "builder")
    store.append(event(2), "builder")
    before = log_path(store, "builder").read_bytes()
    store.append(event(3), "builder")
    after = log_path(store, "builder").read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


def test_an_interrupted_append_leaves_the_log_readable(store: HistoryStore) -> None:
    """A torn trailing record is invisible to readers and repaired on write.

    The index is the commit marker and is written last, so a record only
    becomes visible once it is complete on disk. A half-written tail can
    therefore never be read as data.
    """
    store.append(event(1), "builder")
    store.append(event(2), "builder")
    with log_path(store, "builder").open("ab") as handle:
        handle.write(b'{"storeFormatVersion":"1","seq":3,"sub')  # power cut, mid-line

    reopened = HistoryStore(store.root)
    assert len(reopened.read("builder", since=0, max=10).records) == 2

    third = event(3)
    assert reopened.append(third, "builder") == 3
    page = reopened.read("builder", since=0, max=10)
    assert [record.seq for record in page.records] == [1, 2, 3]
    assert page.records[2].envelope.id == third.id
    assert b'"sub\n' not in log_path(store, "builder").read_bytes()


def test_a_deleted_index_is_rebuilt_from_the_log(store: HistoryStore) -> None:
    """The log is the truth; every sidecar is derived and disposable."""
    envelopes = [event(n) for n in range(3)]
    for envelope in envelopes:
        store.append(envelope, "builder")
    index_path(store, "builder").unlink()
    ids_path(store, "builder").unlink()

    reopened = HistoryStore(store.root)
    assert reopened.append(envelopes[0], "builder") == 1  # rebuilt index restores dedupe
    page = reopened.read("builder", since=0, max=10)
    assert [record.envelope.id for record in page.records] == [e.id for e in envelopes]


def test_a_complete_record_that_missed_its_commit_marker_is_recovered(
    store: HistoryStore,
) -> None:
    """A crash between writing a record and committing it must not lose it.

    The record's bytes are whole — only the index entry is missing — so
    recovery adopts it rather than discarding it. Either choice is *correct*
    (an unacknowledged event is redelivered and would be re-appended), but
    adopting it is strictly safer: it also holds when redelivery never comes.
    Until recovery runs it stays invisible, because the index is the commit
    marker and readers never look past it.
    """
    envelopes = [event(n) for n in range(3)]
    for envelope in envelopes:
        store.append(envelope, "builder")
    with index_path(store, "builder").open("r+b") as handle:
        handle.truncate(struct.calcsize("<QQ") * 2)  # crash before committing #3

    reopened = HistoryStore(store.root)
    assert len(reopened.read("builder", since=0, max=10).records) == 2

    later = event(99)
    assert reopened.append(later, "builder") == 4  # 3 was recovered, not reused
    page = reopened.read("builder", since=0, max=10)
    assert [record.envelope.id for record in page.records] == [
        *[envelope.id for envelope in envelopes],
        later.id,
    ]


def test_a_recovered_record_is_still_deduped(store: HistoryStore) -> None:
    """Recovery restores dedupe, so a redelivery of the orphan is a no-op."""
    envelopes = [event(n) for n in range(2)]
    for envelope in envelopes:
        store.append(envelope, "builder")
    with index_path(store, "builder").open("r+b") as handle:
        handle.truncate(struct.calcsize("<QQ"))

    reopened = HistoryStore(store.root)
    assert reopened.append(envelopes[1], "builder") == 2  # redelivered, already there
    assert len(reopened.read("builder", since=0, max=10).records) == 2


def test_a_torn_index_entry_is_discarded(store: HistoryStore) -> None:
    """Half an index entry is not a commit; the record behind it is unread."""
    store.append(event(1), "builder")
    store.append(event(2), "builder")
    with index_path(store, "builder").open("r+b") as handle:
        handle.truncate(struct.calcsize("<QQ") + 3)  # half an entry survived the crash
    reopened = HistoryStore(store.root)
    assert len(reopened.read("builder", since=0, max=10).records) == 1
    assert reopened.append(event(3), "builder") == 3  # #2 recovered, then the new one
    assert index_path(store, "builder").stat().st_size % struct.calcsize("<QQ") == 0


def test_a_stale_ids_sidecar_is_rebuilt(store: HistoryStore) -> None:
    envelope = event(1)
    store.append(envelope, "builder")
    store.append(event(2), "builder")
    ids_path(store, "builder").write_text("evt_bogus\n", encoding="utf-8")
    reopened = HistoryStore(store.root)
    assert reopened.append(envelope, "builder") == 1  # dedupe restored from the log
    assert ids_path(store, "builder").read_text(encoding="utf-8").splitlines()[0] == envelope.id


def test_get_never_answers_from_a_stale_sidecar(store: HistoryStore) -> None:
    """A read must validate the sidecar, not trust the position it reports.

    ``ids.txt`` is derived state. The *append* path already notices when it has
    gone stale and rebuilds it — but ``get`` used to take the position on faith
    and return whatever record sat there, which for a shifted sidecar is a
    **different event than the one asked for**. That is the worst shape of
    wrong: a confident answer, not an error.

    Here the sidecar is rewritten so the two ids are transposed. A trusting
    ``get`` hands back the wrong record; the fixed one detects the disagreement,
    rebuilds from the log, and answers correctly.
    """
    first, second = event(1), event(2)
    store.append(first, "builder")
    store.append(second, "builder")

    # Transposed: position 0 now claims to hold `second`, and vice versa.
    ids_path(store, "builder").write_text(f"{second.id}\n{first.id}\n", encoding="utf-8")

    found = HistoryStore(store.root).get(first.id)
    assert found is not None
    assert found.envelope.id == first.id, "get answered from the stale sidecar"
    assert found.envelope.data == {"n": 1}
    # And the sidecar was repaired on the way, so the next read is clean.
    assert ids_path(store, "builder").read_text(encoding="utf-8").splitlines() == [
        first.id,
        second.id,
    ]


def test_read_ids_offset_survives_an_undecodable_byte(store: HistoryStore) -> None:
    """The incremental-refresh offset is byte arithmetic, not decoded-text arithmetic.

    Measuring the trailing fragment by re-encoding decoded text is not
    byte-stable: an undecodable byte in a corrupt sidecar decodes to U+FFFD and
    re-encodes to three bytes, so the offset drifts and the next refresh starts
    mid-id. Splitting on the raw bytes keeps the boundary exact.

    The undecodable bytes must sit in the **trailing fragment** for the drift
    to show: a fragment of pure ASCII (or none at all) re-encodes to its own
    length, and there the old arithmetic happened to agree.
    """
    envelope = event(1)
    store.append(envelope, "builder")
    path = ids_path(store, "builder")
    # A complete line, then a partial one carrying two undecodable bytes. Each
    # decodes to U+FFFD, which re-encodes to three bytes — so measuring the
    # fragment through decoded text overstates it by four and pulls the offset
    # back into the middle of the completed id.
    path.write_bytes(f"{envelope.id}\n".encode("utf-8") + b"\xff\xfe")

    backend = HistoryStore(store.root)._backend  # type: ignore[attr-defined]
    lines, offset = backend._read_ids("builder", 0)

    assert lines == [envelope.id]
    # The boundary is exactly past the last newline — not the file size (there
    # is an incomplete fragment after it) and not size-minus-re-encoded-length.
    assert offset == path.stat().st_size - 2
    # Resuming from it yields nothing new rather than re-reading a half id.
    assert backend._read_ids("builder", offset) == ([], offset)


def test_repairing_the_sidecars_leaves_no_temporary_files_behind(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    ids_path(store, "builder").unlink()
    HistoryStore(store.root).append(event(2), "builder")
    leftovers = [p.name for p in sub_dir(store, "builder").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_a_log_shorter_than_the_index_claims_is_reindexed(store: HistoryStore) -> None:
    """When the two disagree about how much data exists, the log wins."""
    envelopes = [event(n) for n in range(3)]
    for envelope in envelopes:
        store.append(envelope, "builder")
    entries = index_entries(store, "builder")
    os.truncate(log_path(store, "builder"), entries[1][0] + 5)  # lost the tail

    reopened = HistoryStore(store.root)
    assert reopened.append(event(9), "builder") == 2  # only record 1 survived
    page = reopened.read("builder", since=0, max=10)
    assert [record.envelope.id for record in page.records][0] == envelopes[0].id
    assert len(page.records) == 2


@pytest.mark.parametrize("junk", [b"not json at all\n", b"[1,2,3]\n", b'{"no":"event"}\n'])
def test_uncommitted_junk_at_the_tail_is_discarded(store: HistoryStore, junk: bytes) -> None:
    """Bytes past the commit marker have never been read, so they are not data."""
    store.append(event(1), "builder")
    committed = index_entries(store, "builder")[-1][1]
    with log_path(store, "builder").open("ab") as handle:
        handle.write(junk)

    reopened = HistoryStore(store.root)
    assert reopened.append(event(2), "builder") == 2
    assert index_entries(store, "builder")[1][0] == committed
    assert len(reopened.read("builder", since=0, max=10).records) == 2


def test_fsync_mode_still_round_trips(tmp_path: Path) -> None:
    """The durability opt-in changes when bytes hit the platter, not what they are."""
    durable = HistoryStore(tmp_path / "history", fsync=True)
    envelope = event(1)
    assert durable.append(envelope, "builder") == 1
    assert durable.read("builder").records[0].envelope == envelope


def test_a_corrupt_committed_record_is_an_environment_fault(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    _overwrite_record(store, "builder", start, end, "}not json{")
    with pytest.raises(HistoryCorruptError) as exc:
        store.read("builder")
    assert "log.jsonl" in str(exc.value)
    assert exc.value.remediation


def test_a_record_whose_sequence_disagrees_with_the_index_is_rejected(
    store: HistoryStore,
) -> None:
    """The index and the record must agree, or the cursor means nothing."""
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    payload = json.loads(log_path(store, "builder").read_bytes()[start:end])
    payload["seq"] = 7
    _overwrite_record(store, "builder", start, end, payload)
    with pytest.raises(HistoryCorruptError):
        store.read("builder")


def test_a_stored_record_that_is_not_an_object_is_rejected(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    _overwrite_record(store, "builder", start, end, "[1,2]")
    with pytest.raises(HistoryCorruptError):
        store.read("builder")


def test_a_stored_record_missing_a_field_is_rejected(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    payload = json.loads(log_path(store, "builder").read_bytes()[start:end])
    del payload["seq"]
    _overwrite_record(store, "builder", start, end, payload)
    with pytest.raises(HistoryCorruptError) as exc:
        store.read("builder")
    assert "seq" in str(exc.value)


def test_a_stored_record_with_a_zero_sequence_is_rejected(store: HistoryStore) -> None:
    """Sequences start at 1; zero would make "since=0" ambiguous."""
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    payload = json.loads(log_path(store, "builder").read_bytes()[start:end])
    payload["seq"] = 0
    _overwrite_record(store, "builder", start, end, payload)
    with pytest.raises(HistoryCorruptError):
        store.read("builder")


def test_a_record_carrying_an_invalid_envelope_is_rejected_on_read(store: HistoryStore) -> None:
    store.append(event(1), "builder")
    start, end = index_entries(store, "builder")[0]
    payload = json.loads(log_path(store, "builder").read_bytes()[start:end])
    payload["event"]["type"] = "not.a.type "
    _overwrite_record(store, "builder", start, end, payload)
    with pytest.raises(HistoryCorruptError):
        store.read("builder")


# --- the backend seam ------------------------------------------------------


class RecordingBackend:
    """A stand-in backend, to prove the seam is a substitution point."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def append(self, envelope: Envelope, sub: str) -> int:
        self.calls.append(("append", (envelope.id, sub)))
        return 42

    def read(self, sub: str, since: int, max: int) -> HistoryPage:
        self.calls.append(("read", (sub, since, max)))
        return HistoryPage(records=(), cursor=since, has_more=False)

    def get(self, id: str) -> HistoryRecord | None:
        self.calls.append(("get", (id,)))
        return None

    def list(self, type: str | None, max: int) -> tuple[HistoryRecord, ...]:
        self.calls.append(("list", (type, max)))
        return ()

    def subscriptions(self) -> tuple[str, ...]:
        self.calls.append(("subscriptions", ()))
        return ()


def test_the_store_delegates_every_verb_to_its_backend(tmp_path: Path) -> None:
    """The façade adds argument policy and nothing else.

    A replacement backend has to satisfy five methods and receives already
    validated arguments — which is what keeps swapping the shipped JSONL log
    for a sibling store a substitution rather than a rewrite.
    """
    backend = RecordingBackend(tmp_path)
    store = HistoryStore(backend=backend)
    assert store.root == tmp_path
    assert store.backend is backend

    assert store.append(event(1), "builder") == 42
    store.read("builder", since=3, max=7)
    store.get("evt_x")
    store.list(type="task.requested", max=5)
    store.subscriptions()
    assert [name for name, _ in backend.calls] == [
        "append",
        "read",
        "get",
        "list",
        "subscriptions",
    ]
    assert backend.calls[1][1] == ("builder", 3, 7)


def test_a_backend_is_recognised_structurally() -> None:
    from events_cli.history import HistoryBackend
    from events_cli.history.jsonl import JsonlBackend

    assert isinstance(RecordingBackend(Path(".")), HistoryBackend)
    assert isinstance(JsonlBackend(Path(".")), HistoryBackend)


def test_the_facade_validates_before_the_backend_sees_anything(tmp_path: Path) -> None:
    backend = RecordingBackend(tmp_path)
    store = HistoryStore(backend=backend)
    envelope = event(1)
    with pytest.raises(InvalidSubscriptionError):
        store.append(envelope, "../escape")
    with pytest.raises(HistoryError):
        store.read("builder", since=0, max=0)
    assert backend.calls == []


def test_history_errors_are_events_errors() -> None:
    """One domain-error root, so a caller catches ``EventsError`` and is done."""
    for error in (HistoryError, HistoryCorruptError, HistoryFormatError, InvalidSubscriptionError):
        assert issubclass(error, EventsError)
    assert issubclass(HistoryFormatError, HistoryCorruptError)


# --- concurrent writers ----------------------------------------------------


def test_two_store_handles_never_reuse_a_sequence(tmp_path: Path) -> None:
    """The sequence comes off disk under a lock, not from an in-memory counter.

    Two handles are the in-process stand-in for two processes: each keeps its
    own cached state, so a counter held in memory would hand out 1, 1, 2, 2.
    """
    root = tmp_path / "history"
    first, second = HistoryStore(root), HistoryStore(root)
    seqs = []
    for n in range(10):
        seqs.append(first.append(event(n), "builder"))
        seqs.append(second.append(event(100 + n), "builder"))
    assert seqs == list(range(1, 21))
    assert len(second.read("builder", since=0, max=100).records) == 20


def test_concurrent_appends_assign_every_sequence_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "history"
    workers = 4
    per_worker = 25
    results: list[list[int]] = [[] for _ in range(workers)]

    def worker(index: int) -> None:
        handle = HistoryStore(root)
        for n in range(per_worker):
            results[index].append(handle.append(event(index * 1000 + n), "builder"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assigned = sorted(seq for chunk in results for seq in chunk)
    assert assigned == list(range(1, workers * per_worker + 1))
    page = HistoryStore(root).read("builder", since=0, max=1000)
    assert [record.seq for record in page.records] == assigned


# --- record shapes ---------------------------------------------------------


def test_history_record_round_trips_through_its_wire_form() -> None:
    envelope = event(1)
    record = HistoryRecord(
        seq=3, subscription="builder", recorded_at="2026-07-24T10:00:00Z", envelope=envelope
    )
    assert HistoryRecord.from_dict(record.to_dict()) == record


def test_history_record_wire_form_is_camel_case() -> None:
    record = HistoryRecord(
        seq=1, subscription="builder", recorded_at="2026-07-24T10:00:00Z", envelope=event(1)
    )
    assert set(record.to_dict()) == {
        "storeFormatVersion",
        "seq",
        "subscription",
        "recordedAt",
        "event",
    }


def test_history_page_carries_the_batch_the_cursor_and_the_more_flag() -> None:
    page = HistoryPage(records=(), cursor=7, has_more=False)
    assert page.to_dict() == {"records": [], "cursor": 7, "hasMore": False}


# --- module-level entry points --------------------------------------------


def test_the_module_level_helpers_share_one_default_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(HISTORY_DIR_ENV, str(tmp_path / "store"))
    envelope = event(1)
    assert append(envelope, "builder") == 1
    assert read("builder", since=0, max=10).records[0].envelope == envelope
    assert get(envelope.id) is not None
    assert [record.envelope.id for record in list_events(type="task.requested")] == [envelope.id]


# --- layering / dependency posture ----------------------------------------


def history_source_files() -> list[Path]:
    package = Path(__file__).resolve().parent.parent / "events_cli" / "history"
    files = sorted(package.rglob("*.py"))
    assert files, "history package not found"
    return files


def test_history_imports_only_the_standard_library() -> None:
    """No third-party import may reach the store.

    Static (AST) rather than dynamic, so it also catches imports on branches
    this suite never executes. A paho or docker import here would drag the
    store — and the coverage gate that depends on it — behind an install.
    """
    for path in history_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: use absolute imports"
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root == "events_cli":
                    assert name.startswith(
                        ("events_cli.core", "events_cli.history")
                    ), f"{path.name}: history may not import {name}"
                    continue
                assert (
                    root in sys.stdlib_module_names
                ), f"{path.name}: {name} is not in the standard library"


def test_the_introspection_lane_does_not_import_the_store() -> None:
    """``events doctor`` must still run from a bare checkout.

    The store is not part of the introspection lane, and importing it from the
    CLI package would be the first step toward it becoming so.
    """
    import subprocess

    code = (
        "import sys; import events_cli.cli;"
        " mods=[m for m in sys.modules if m.startswith('events_cli.history')];"
        " print(mods)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    assert result.stdout.strip() == "[]"


def test_the_new_event_id_helper_is_still_the_ids_under_test() -> None:
    """Guard the premise: the ids these tests reason about are the real ones."""
    assert timestamp_part(new_event_id()).isalnum()
