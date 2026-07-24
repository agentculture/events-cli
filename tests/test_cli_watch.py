"""Behaviour tests for ``events watch`` — the CLI translation layer and its
``--since`` composition of persisted history with the live broker drain.

**No test here ever opens a broker session.** ``drain_subscription`` is always
replaced (as imported into ``events_cli.cli._commands.watch``) with a
hand-written fake before ``main()`` runs — this machine runs a **production
broker for a live robot** on ``127.0.0.1:1883``, and the real
``drain_subscription`` would connect to exactly that port. Where a test needs
"the broker delivered N new events", the fake persists them into the same
isolated history store the test already seeded and returns the resulting
:class:`~events_cli.subs.drain.DrainResult` — so what is exercised is the real
:meth:`~events_cli.history.HistoryStore.read` /
:meth:`~events_cli.history.HistoryStore.append` composition, never a network
call.

``EVENTS_HISTORY_DIR`` is pinned to a fresh ``tmp_path`` for every test in this
module (the ``isolated_history`` fixture, autouse), so nothing here can ever
read or write the real per-host history store either.

The design under test
----------------------
``events watch <name> --since S --max M --timeout T`` is designed (see
``events_cli/cli/_commands/watch.py``'s module docstring) to:

1. Replay persisted history first: ``HistoryStore.read(name, S, M)`` — no
   broker connection. If this alone returns ``M`` records, the broker is
   *never* touched (:func:`test_history_alone_can_fill_max_without_touching_the_broker`).
2. Drain the broker only for what remains, floored at the cursor the history
   read ended on — never the raw ``--since`` — so a redelivery already seen in
   history can never come back from the broker
   (:func:`test_since_floor_passed_to_the_broker_drain_is_the_history_cursor_not_the_raw_since`).
3. Concatenate the two batches, oldest first, and report which source(s)
   contributed via ``servedFrom``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from events_cli.cli import _build_parser, main
from events_cli.cli._commands import watch as watch_module
from events_cli.core import Envelope
from events_cli.explain import known_paths
from events_cli.history import HISTORY_DIR_ENV, HistoryCorruptError, HistoryStore
from events_cli.subs import (
    BrokerUnreachableError,
    SubscriptionValidationError,
    UnknownSubscriptionError,
)
from events_cli.subs.drain import (
    DEFAULT_DRAIN_MAX,
    DEFAULT_DRAIN_TIMEOUT,
    STOPPED_MAX,
    STOPPED_TIMEOUT,
    DrainResult,
)

# --- isolation: never touch the real store, never open a broker session ----


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "history"
    monkeypatch.setenv(HISTORY_DIR_ENV, str(root))
    return root


def _seed(root: Path, sub: str, count: int, *, event_type: str = "task.requested") -> None:
    store = HistoryStore(root)
    for i in range(count):
        store.append(Envelope.new(event_type, "agent://builder", data={"n": i}), sub)


def _poison_drain(*args: object, **kwargs: object) -> DrainResult:
    raise AssertionError(
        f"drain_subscription must not be called; got args={args!r} kwargs={kwargs!r}"
    )


# --- structure: registration, catalog, --json, defaults, no --follow --------


def _choices(parser) -> dict:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _subparser(parser, *path):
    node = parser
    for name in path:
        node = _choices(node)[name]
    return node


def test_watch_is_registered_with_json() -> None:
    parser = _build_parser()
    watch = _subparser(parser, "watch")
    assert "--json" in watch._option_string_actions


def test_watch_catalog_entry_exists() -> None:
    assert ("watch",) in known_paths()


def test_watch_defaults_are_max_100_timeout_30_since_0() -> None:
    parser = _build_parser()
    args = parser.parse_args(["watch", "robot"])
    assert args.max == 100
    assert args.timeout == 30
    assert args.since == 0
    # And the constants the CLI wires those defaults from — so a change to
    # the drain engine's own defaults cannot silently drift from what the CLI
    # advertises.
    assert DEFAULT_DRAIN_MAX == 100
    assert DEFAULT_DRAIN_TIMEOUT == 30


def test_watch_has_no_follow_flag_statically() -> None:
    parser = _build_parser()
    watch = _subparser(parser, "watch")
    assert "--follow" not in watch._option_string_actions


def test_watch_rejects_follow_flag_at_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["watch", "robot", "--follow"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


# --- the --since composition ------------------------------------------------


def test_history_alone_can_fill_max_without_touching_the_broker(
    monkeypatch: pytest.MonkeyPatch, isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(isolated_history, "robot", 5)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--max", "3", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servedFrom"] == "history"
    assert len(payload["records"]) == 3
    assert [r["seq"] for r in payload["records"]] == [1, 2, 3]
    assert payload["cursor"] == 3
    assert payload["hasMore"] is True
    assert payload["stopped"] == STOPPED_MAX
    assert payload["sessionPresent"] is None
    assert payload["consumed"] == 0
    assert payload["skipped"] == []


def test_since_floor_passed_to_the_broker_drain_is_the_history_cursor_not_the_raw_since(
    monkeypatch: pytest.MonkeyPatch, isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the composition: a drain must never re-return what history

    already replayed. Seed 2 persisted records (cursor lands at 2), ask for
    ``--max 10`` so the broker drain is reached, and assert it is called with
    ``since=2`` — the history cursor — never the raw ``--since 0`` the caller
    passed.
    """
    root = isolated_history
    _seed(root, "robot", 2)
    calls: list[dict[str, object]] = []

    def fake_drain(name: str, *, since: int, max: int, timeout: float, **kwargs: object):
        calls.append({"name": name, "since": since, "max": max, "timeout": timeout})
        store = HistoryStore(root)
        for i in range(2):
            store.append(Envelope.new("task.completed", "agent://builder", data={"i": i}), name)
        page = store.read(name, since, max)
        return DrainResult(
            subscription=name,
            records=page.records,
            cursor=page.cursor,
            has_more=False,
            session_present=True,
            consumed=len(page.records),
            skipped=(),
            stopped=STOPPED_TIMEOUT,
        )

    monkeypatch.setattr(watch_module, "drain_subscription", fake_drain)

    rc = main(["watch", "robot", "--max", "10", "--json"])

    assert rc == 0
    assert calls == [{"name": "robot", "since": 2, "max": 8, "timeout": 30.0}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["servedFrom"] == "history+broker"
    assert [r["seq"] for r in payload["records"]] == [1, 2, 3, 4]
    assert payload["cursor"] == 4
    assert payload["stopped"] == STOPPED_TIMEOUT
    assert payload["sessionPresent"] is True
    assert payload["consumed"] == 2


def test_served_from_broker_when_history_is_empty(
    monkeypatch: pytest.MonkeyPatch, isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = isolated_history
    calls: list[tuple[int, int, float]] = []

    def fake_drain(name: str, *, since: int, max: int, timeout: float, **kwargs: object):
        calls.append((since, max, timeout))
        store = HistoryStore(root)
        store.append(Envelope.new("task.requested", "agent://builder", data={}), name)
        page = store.read(name, since, max)
        return DrainResult(
            subscription=name,
            records=page.records,
            cursor=page.cursor,
            has_more=False,
            session_present=False,
            consumed=1,
            skipped=(),
            stopped=STOPPED_TIMEOUT,
        )

    monkeypatch.setattr(watch_module, "drain_subscription", fake_drain)

    rc = main(["watch", "robot", "--json"])

    assert rc == 0
    assert calls == [(0, 100, 30.0)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["servedFrom"] == "broker"
    assert payload["sessionPresent"] is False
    assert len(payload["records"]) == 1


def test_since_flag_floors_the_history_replay_itself(
    monkeypatch: pytest.MonkeyPatch, isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(isolated_history, "robot", 5)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--since", "2", "--max", "2", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["seq"] for r in payload["records"]] == [3, 4]
    assert payload["cursor"] == 4
    assert payload["servedFrom"] == "history"


def test_empty_batch_at_the_deadline_is_still_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_drain(name: str, *, since: int, max: int, timeout: float, **kwargs: object):
        return DrainResult(
            subscription=name,
            records=(),
            cursor=since,
            has_more=False,
            session_present=True,
            consumed=0,
            skipped=(),
            stopped=STOPPED_TIMEOUT,
        )

    monkeypatch.setattr(watch_module, "drain_subscription", fake_drain)

    rc = main(["watch", "robot"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "0 event(s)" in out
    assert "(no events)" in out


def test_text_mode_lists_each_event_and_the_next_hint(
    monkeypatch: pytest.MonkeyPatch, isolated_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(isolated_history, "robot", 2)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--max", "2"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "task.requested" in out
    assert "[1]" in out and "[2]" in out
    assert "next: events watch robot --since 2" in out


# --- error -> exit-code mapping ---------------------------------------------


def test_broker_unreachable_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raiser(*args: object, **kwargs: object):
        raise BrokerUnreachableError("down", remediation="events up")

    monkeypatch.setattr(watch_module, "drain_subscription", raiser)
    rc = main(["watch", "robot"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_unknown_subscription_is_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raiser(*args: object, **kwargs: object):
        raise UnknownSubscriptionError("no such sub", remediation="events sub list")

    monkeypatch.setattr(watch_module, "drain_subscription", raiser)
    rc = main(["watch", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_invalid_drain_bounds_from_the_domain_layer_is_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raiser(*args: object, **kwargs: object):
        raise SubscriptionValidationError([], summary="invalid drain bounds", remediation="fix it")

    monkeypatch.setattr(watch_module, "drain_subscription", raiser)
    rc = main(["watch", "robot"])
    assert rc == 1


def test_history_corrupt_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class PoisonStore:
        def read(self, name: str, since: int, max: int):
            raise HistoryCorruptError("damaged on disk", remediation="inspect the store")

    monkeypatch.setattr(watch_module, "open_store", lambda: PoisonStore())
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


# --- bound validation happens before any read or drain ----------------------


def _poison_store():
    raise AssertionError("open_store must not be called for an invalid bound")


def test_zero_max_is_rejected_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(watch_module, "open_store", _poison_store)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--max", "0"])

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_negative_since_is_rejected_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(watch_module, "open_store", _poison_store)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--since", "-1"])

    assert rc == 1


def test_non_positive_timeout_is_rejected_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(watch_module, "open_store", _poison_store)
    monkeypatch.setattr(watch_module, "drain_subscription", _poison_drain)

    rc = main(["watch", "robot", "--timeout", "0"])

    assert rc == 1
