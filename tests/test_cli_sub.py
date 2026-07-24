"""Behaviour tests for ``events sub add/list/show/remove`` — the CLI translation layer.

**Nothing here calls the real domain functions.** ``add_subscription`` and
``remove_subscription`` open a real MQTT persistent session against
``127.0.0.1:1883`` when given no ``client_factory`` — and this machine runs a
**production broker for a live robot** on that exact port. Every test below
replaces ``add_subscription`` / ``remove_subscription`` / ``list_subscriptions``
/ ``get_subscription`` as imported into ``events_cli.cli._commands.sub`` with a
hand-written fake, so no test here can ever open a socket. That is also the
right boundary for what this file should prove: the domain layer (session
lifecycle, registry persistence) is already exhaustively covered by
``tests/test_subs.py`` with real dependency-injected fakes; this file proves
only the *translation* — argv parsing, rendering, and the
``SubsError`` -> exit-code mapping.
"""

from __future__ import annotations

import json

import pytest

from events_cli.cli import _build_parser, main
from events_cli.cli._commands import sub as sub_module
from events_cli.core import FieldError
from events_cli.explain import known_paths
from events_cli.subs import (
    DuplicateSubscriptionError,
    RegistryCorruptError,
    SessionError,
    SubscriptionRecord,
    SubscriptionValidationError,
    UnknownSubscriptionError,
)
from events_cli.subs.errors import BrokerUnreachableError, RegistryFormatError

# --- structure: registration, catalog, --json ------------------------------


def _choices(parser) -> dict:
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def _subparser(parser, *path):
    node = parser
    for name in path:
        node = _choices(node)[name]
    return node


def test_sub_and_every_subverb_are_registered_with_json():
    parser = _build_parser()
    for path in (("sub",), ("sub", "add"), ("sub", "list"), ("sub", "show"), ("sub", "remove")):
        node = _subparser(parser, *path)
        assert "--json" in node._option_string_actions, path


def test_sub_catalog_entries_exist():
    paths = known_paths()
    for path in (("sub",), ("sub", "add"), ("sub", "list"), ("sub", "show"), ("sub", "remove")):
        assert path in paths


def test_sub_remove_has_a_force_flag_that_documents_its_risk():
    parser = _build_parser()
    remove = _subparser(parser, "sub", "remove")
    action = remove._option_string_actions["--force"]
    assert "broker" in action.help.lower()


def test_sub_unknown_subverb_is_a_structured_error_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sub", "bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_sub_add_missing_positional_args_is_a_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sub", "add"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


# --- add --------------------------------------------------------------


def test_sub_add_success_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    monkeypatch.setattr(sub_module, "add_subscription", lambda name, pattern, *, owner=None: record)
    rc = main(["sub", "add", "robot", "task.*"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "registered subscription 'robot'" in out
    assert "next: events watch robot" in out
    assert capsys.readouterr().err == ""


def test_sub_add_success_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    monkeypatch.setattr(sub_module, "add_subscription", lambda name, pattern, *, owner=None: record)
    rc = main(["sub", "add", "robot", "task.*", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == record.to_dict()


def test_sub_add_owner_flag_reaches_the_domain_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_add(name, pattern, *, owner=None):
        seen["args"] = (name, pattern, owner)
        return SubscriptionRecord.new(name, pattern, owner=owner or "fallback")

    monkeypatch.setattr(sub_module, "add_subscription", fake_add)
    rc = main(["sub", "add", "robot", "task.*", "--owner", "reachy-mini-cli"])
    assert rc == 0
    assert seen["args"] == ("robot", "task.*", "reachy-mini-cli")


def test_sub_add_without_owner_passes_none_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--owner`` must reach the domain layer as ``None``, not a CLI-invented default.

    ``add_subscription`` resolves the culture.yaml default itself
    (``SubscriptionRecord.new`` -> ``resolve_owner``); the CLI must not shadow
    that by inventing its own fallback.
    """
    seen: dict[str, object] = {}

    def fake_add(name, pattern, *, owner=None):
        seen["owner"] = owner
        return SubscriptionRecord.new(name, pattern, owner="whatever-the-domain-resolved")

    monkeypatch.setattr(sub_module, "add_subscription", fake_add)
    main(["sub", "add", "robot", "task.*"])
    assert seen["owner"] is None


# --- list ----------------------------------------------------------------


def test_sub_list_text_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records = (
        SubscriptionRecord.new("a", "task.*", owner="builder"),
        SubscriptionRecord.new("b", "heartbeat", owner="builder"),
    )
    monkeypatch.setattr(sub_module, "list_subscriptions", lambda: records)
    rc = main(["sub", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a  pattern=task.*  owner=builder" in out
    assert "b  pattern=heartbeat  owner=builder" in out

    monkeypatch.setattr(sub_module, "list_subscriptions", lambda: records)
    rc = main(["sub", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"subscriptions": [r.to_dict() for r in records]}


def test_sub_list_empty_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sub_module, "list_subscriptions", lambda: ())
    rc = main(["sub", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no subscriptions registered" in out
    assert "events sub add" in out


def test_bare_sub_aliases_to_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sub_module, "list_subscriptions", lambda: ())
    rc = main(["sub"])
    assert rc == 0
    assert "no subscriptions registered" in capsys.readouterr().out


# --- show ------------------------------------------------------------------


def test_sub_show_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    monkeypatch.setattr(sub_module, "get_subscription", lambda name: record)
    rc = main(["sub", "show", "robot"])
    assert rc == 0
    assert "subscription 'robot'" in capsys.readouterr().out


def test_sub_show_found_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    monkeypatch.setattr(sub_module, "get_subscription", lambda name: record)
    rc = main(["sub", "show", "robot", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == record.to_dict()


def test_sub_show_missing_is_exit_1_with_a_hint_naming_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sub_module, "get_subscription", lambda name: None)
    rc = main(["sub", "show", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "sub list" in err
    assert "Traceback" not in err


# --- remove ------------------------------------------------------------------


def test_sub_remove_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    seen: dict[str, object] = {}

    def fake_remove(name, *, force=False):
        seen["args"] = (name, force)
        return record

    monkeypatch.setattr(sub_module, "remove_subscription", fake_remove)
    rc = main(["sub", "remove", "robot"])
    assert rc == 0
    assert seen["args"] == ("robot", False)
    out = capsys.readouterr().out
    assert "removed subscription 'robot'" in out
    assert "--force" not in out


def test_sub_remove_force_flag_reaches_the_domain_call_and_is_noted_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    seen: dict[str, object] = {}

    def fake_remove(name, *, force=False):
        seen["force"] = force
        return record

    monkeypatch.setattr(sub_module, "remove_subscription", fake_remove)
    rc = main(["sub", "remove", "robot", "--force"])
    assert rc == 0
    assert seen["force"] is True
    assert "--force was set" in capsys.readouterr().out


# --- error -> exit-code mapping, across every sub verb ----------------------

_ADD = ["sub", "add", "robot", "task.*"]
_LIST = ["sub", "list"]
_SHOW = ["sub", "show", "robot"]
_REMOVE = ["sub", "remove", "robot"]


@pytest.mark.parametrize(
    "argv, attr",
    [
        (_ADD, "add_subscription"),
        (_LIST, "list_subscriptions"),
        (_SHOW, "get_subscription"),
        (_REMOVE, "remove_subscription"),
    ],
)
@pytest.mark.parametrize(
    "make_exc, expected_code",
    [
        (
            lambda: SubscriptionValidationError([FieldError("name", "malformed", "bad name")]),
            1,
        ),
        (lambda: DuplicateSubscriptionError("already there", remediation="pick another"), 1),
        (lambda: UnknownSubscriptionError("no such sub", remediation="events sub list"), 1),
        (lambda: RegistryCorruptError("damaged", remediation="inspect the file"), 2),
        (lambda: RegistryFormatError("too new", remediation="upgrade events-cli"), 2),
        (lambda: SessionError("refused", remediation="check the broker log"), 2),
        (lambda: BrokerUnreachableError("unreachable", remediation="events up"), 2),
    ],
)
def test_sub_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    attr: str,
    make_exc,
    expected_code: int,
) -> None:
    exc = make_exc()

    def raiser(*args: object, **kwargs: object):
        raise exc

    monkeypatch.setattr(sub_module, attr, raiser)
    rc = main(list(argv))
    assert rc == expected_code
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err
