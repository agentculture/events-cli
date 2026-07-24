"""Contract tests for the subscription registry and its MQTT session lifecycle.

A *durable subscription* in this arc is two things that must not drift apart:

* a **registry record** on disk — name, pattern, compiled filter, owner, client
  id, created — which is what ``events sub list`` reads and what #10's dynsec
  work will attach a real authenticated identity to; and
* an **MQTT persistent session** in the broker, addressed by the record's client
  id, which is what actually queues events while no drainer is connected.

Everything here runs with **no broker and no docker**. The session lifecycle is
driven against a fake paho client (:class:`FakePaho` below) that records the
exact CONNECT/SUBSCRIBE/DISCONNECT sequence, because the properties that matter
— ``clean_start=False``, an effectively infinite session expiry, QoS 1, and a
*graceful* disconnect that leaves the session live — are properties of the
packets we send, not of any broker's reply. The live proof that those packets
really do survive a broker restart is the stack-marked suite (t12); what is
pinned here is that we send them at all, and that we never send them by accident
on the ``remove`` path, where the whole point is to destroy the session.

The **client** is faked; paho's *constant and property* types are not. The
session-lifecycle tests below therefore need paho importable (never a broker),
while every test about the record schema, name/pattern validation, the client-id
derivation and the registry runs on a bare checkout with nothing installed —
the same line ``tests/test_client.py`` already draws. Keeping the real
``mqtt.Properties`` is deliberate: it *validates* property names and raises on
an unknown one, so ``SessionExpiryInterval`` being a name paho actually accepts
is something these tests prove. A faked Properties object would accept a typo
silently and assert nothing at all.

Two derivations get disproportionate attention because a plausible-looking
implementation gets them wrong:

* **the client id is the session's identity in the broker.** It must be a pure
  function of the subscription name — a per-process random id (which is what
  :class:`events_cli.client.EventClient` correctly defaults to) would mint a
  brand-new empty session on every ``sub add`` and silently orphan the old one.
* **names and patterns are attacker-shaped input.** A name becomes a filename
  *and* an MQTT client id; a pattern becomes a topic filter. ``#``, ``+``, ``/``
  and ``..`` are rejected with field-level errors so a subscription can never
  address a path outside the store or a topic outside the ``events/`` prefix.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from events_cli.core import ERROR_CODES, FieldError, filter_matches_topic
from events_cli.core import identity as core_identity
from events_cli.history import default_history_dir
from events_cli.subs import (
    CLIENT_ID_PREFIX,
    MAX_NAME_LENGTH,
    QOS_AT_LEAST_ONCE,
    REGISTRY_FORMAT_VERSION,
    SESSION_EXPIRY_DESTROY,
    SESSION_EXPIRY_INFINITE,
    SUPPORTED_REGISTRY_FORMATS,
    BrokerAddress,
    BrokerUnreachableError,
    DuplicateSubscriptionError,
    PersistentSession,
    RegistryCorruptError,
    RegistryFormatError,
    SessionError,
    SubscriptionRecord,
    SubscriptionRegistry,
    SubscriptionValidationError,
    SubsError,
    UnknownSubscriptionError,
    add_subscription,
    client_id_for,
    default_client_factory,
    default_registry_dir,
    get_subscription,
    list_subscriptions,
    open_registry,
    remove_subscription,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- the fake paho client --------------------------------------------------
#
# Deliberately a hand-written double rather than a MagicMock: the assertions
# below are about *call order* and the exact CONNECT properties, and a mock that
# accepts anything would pass whatever we wrote. This one only implements the
# five methods the session seam is allowed to use, so a session that reaches for
# anything else fails loudly instead of silently working against a real broker.


class FakeReasonCode:
    """Just enough of paho's ReasonCode for the CONNACK branch under test."""

    def __init__(self, name: str = "Success", *, is_failure: bool = False) -> None:
        self.name = name
        self.is_failure = is_failure

    def __str__(self) -> str:  # pragma: no cover - only used in error messages
        return self.name


class FakePaho:
    """A paho-shaped client that records what the session actually sends."""

    def __init__(
        self,
        client_id: str,
        *,
        manual_ack: bool = False,
        session_present: bool = False,
        connect_error: BaseException | None = None,
        refuse: bool = False,
        silent: bool = False,
        subscribe_rc: int = 0,
    ) -> None:
        self.client_id = client_id
        self.manual_ack = manual_ack
        self.session_present = session_present
        self.connect_error = connect_error
        self.refuse = refuse
        self.silent = silent  # never fires on_connect: the CONNACK-timeout case
        self.subscribe_rc = subscribe_rc
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self._connected = False

    # -- the five methods the seam may use ---------------------------------

    def connect(
        self,
        host: str,
        port: int = 1883,
        keepalive: int = 60,
        *,
        clean_start: Any = None,
        properties: Any = None,
    ) -> int:
        self.calls.append(
            (
                "connect",
                {
                    "host": host,
                    "port": port,
                    "keepalive": keepalive,
                    "clean_start": clean_start,
                    "properties": properties,
                },
            )
        )
        if self.connect_error is not None:
            raise self.connect_error
        return 0

    def loop_start(self) -> int:
        self.calls.append(("loop_start", {}))
        if self.silent:
            return 0
        self._connected = not self.refuse
        if self.on_connect is not None:
            self.on_connect(
                self,
                None,
                _FakeConnectFlags(self.session_present),
                FakeReasonCode(
                    "Not authorized" if self.refuse else "Success", is_failure=self.refuse
                ),
                None,
            )
        return 0

    def subscribe(self, topic: str, qos: int = 0, options: Any = None, properties: Any = None):
        self.calls.append(("subscribe", {"topic": topic, "qos": qos}))
        return (self.subscribe_rc, 1)

    def disconnect(self, reasoncode: Any = None, properties: Any = None) -> int:
        self.calls.append(("disconnect", {"reasoncode": reasoncode, "properties": properties}))
        self._connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self, None, None, FakeReasonCode(), None)
        return 0

    def loop_stop(self) -> int:
        self.calls.append(("loop_stop", {}))
        return 0

    def is_connected(self) -> bool:
        return self._connected

    # -- assertions helpers -------------------------------------------------

    @property
    def sequence(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs(self, name: str) -> dict[str, Any]:
        for called, payload in self.calls:
            if called == name:
                return payload
        raise AssertionError(f"{name!r} was never called; saw {self.sequence}")


class _FakeConnectFlags:
    def __init__(self, session_present: bool) -> None:
        self.session_present = session_present


def fake_factory(**client_kwargs: Any):
    """A ``client_factory`` that hands back a :class:`FakePaho` and remembers it."""
    made: list[FakePaho] = []

    def factory(mqtt: Any, client_id: str, *, manual_ack: bool) -> FakePaho:
        client = FakePaho(client_id, manual_ack=manual_ack, **client_kwargs)
        made.append(client)
        return client

    factory.made = made  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def registry(tmp_path: Path) -> SubscriptionRegistry:
    return SubscriptionRegistry(tmp_path / "registry")


def subs_source_files() -> list[Path]:
    package = Path(__file__).resolve().parent.parent / "events_cli" / "subs"
    return sorted(package.glob("*.py"))


# =========================================================================
# Criterion 1 — the record schema, its owner, and the listing API
# =========================================================================


def test_a_subscription_record_carries_name_pattern_owner_and_created() -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    assert record.name == "robot"
    assert record.pattern == "task.*"
    assert record.owner == "builder"
    assert record.created.endswith("Z")
    assert record.topic_filter == "events/task/+"
    assert record.client_id == client_id_for("robot")


def test_the_record_wire_form_is_exactly_the_documented_schema() -> None:
    """Pinned key-for-key: this is what ``events sub list --json`` will show."""
    payload = SubscriptionRecord.new("robot", "task.*", owner="builder").to_dict()
    assert set(payload) == {
        "registryFormatVersion",
        "name",
        "pattern",
        "filter",
        "owner",
        "clientId",
        "created",
    }
    assert payload["registryFormatVersion"] == REGISTRY_FORMAT_VERSION
    assert payload["filter"] == "events/task/+"
    assert payload["clientId"] == client_id_for("robot")


def test_a_record_round_trips_through_its_wire_form() -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="builder")
    assert SubscriptionRecord.from_dict(json.loads(json.dumps(record.to_dict()))) == record


def test_the_owner_defaults_to_the_culture_yaml_nick() -> None:
    """The agent's own identity, read by the culture.yaml line scanner."""
    record = SubscriptionRecord.new("robot", "task.*")
    assert record.owner == core_identity.read_agent_fields()["nick"] == "events-cli"


def test_the_owner_falls_back_to_the_client_id_without_a_culture_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel install ships no culture.yaml, so the session's own id is the owner."""
    monkeypatch.setattr(core_identity, "find_culture_yaml", lambda: None)
    record = SubscriptionRecord.new("robot", "task.*")
    assert record.owner == record.client_id == client_id_for("robot")


def test_an_explicit_owner_overrides_the_default() -> None:
    record = SubscriptionRecord.new("robot", "task.*", owner="reachy-mini-cli")
    assert record.owner == "reachy-mini-cli"


def test_agent_nick_reports_none_rather_than_a_plausible_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``whoami`` must print something; an owner field must not invent one."""
    monkeypatch.setattr(core_identity, "find_culture_yaml", lambda: None)
    assert core_identity.agent_nick() is None
    assert core_identity.read_agent_fields()["nick"] == core_identity.FALLBACK_NICK


def test_an_unreadable_culture_yaml_falls_back_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unreadable = tmp_path / "culture.yaml"
    unreadable.mkdir()  # a directory read as a file: OSError, not a crash
    monkeypatch.setattr(core_identity, "find_culture_yaml", lambda: unreadable)
    assert core_identity.read_agent_fields()["nick"] == core_identity.FALLBACK_NICK


def test_the_scanner_reads_only_the_first_agent_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A multi-agent culture.yaml resolves to *this* agent, not the last one listed."""
    cfg = tmp_path / "culture.yaml"
    cfg.write_text(
        "agents:\n"
        "- suffix: events-cli\n"
        "  backend: colleague\n"
        "  model: qwen\n"
        "- suffix: someone-else\n"
        "  backend: claude\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core_identity, "find_culture_yaml", lambda: cfg)
    fields = core_identity.read_agent_fields()
    assert (fields["nick"], fields["backend"], fields["model"]) == (
        "events-cli",
        "colleague",
        "qwen",
    )


def test_the_owner_field_is_the_only_identity_field_so_dynsec_needs_no_migration() -> None:
    """#10's dynsec identity name *is* the owner name — same key, same version.

    A record written today by an unauthenticated loopback client and a record
    written after dynsec lands differ only in the *value* of ``owner``: same
    schema, same ``registryFormatVersion``, no second identity key to migrate
    into. This test is what fails if someone adds ``identity``/``username``
    alongside ``owner``.
    """
    anonymous = SubscriptionRecord.new("robot", "task.*", owner="events-cli")
    dynsec = SubscriptionRecord.new("robot", "task.*", owner="events-cli")
    assert set(anonymous.to_dict()) == set(dynsec.to_dict())
    assert anonymous.registry_format_version == dynsec.registry_format_version
    identity_keys = {key for key in anonymous.to_dict() if "owner" in key or "identity" in key}
    assert identity_keys == {"owner"}


def test_add_get_and_list_round_trip_through_the_registry(
    registry: SubscriptionRegistry,
) -> None:
    registry.add(SubscriptionRecord.new("beta", "task.*", owner="builder"))
    registry.add(SubscriptionRecord.new("alpha", "scope.completed", owner="scoper"))
    listed = registry.list()
    assert [record.name for record in listed] == ["alpha", "beta"]
    assert [record.owner for record in listed] == ["scoper", "builder"]
    assert registry.get("alpha").pattern == "scope.completed"
    assert registry.get("missing") is None


def test_the_listing_api_returns_the_owner_for_every_record(
    registry: SubscriptionRegistry,
) -> None:
    """What ``events sub list --json`` (t9) renders — the owner is never absent."""
    registry.add(SubscriptionRecord.new("robot", "task.*", owner="builder"))
    payloads = [record.to_dict() for record in registry.list()]
    assert all(payload["owner"] for payload in payloads)


def test_the_registry_survives_a_new_handle_on_the_same_root(tmp_path: Path) -> None:
    SubscriptionRegistry(tmp_path).add(SubscriptionRecord.new("robot", "task.*"))
    assert [record.name for record in SubscriptionRegistry(tmp_path).list()] == ["robot"]


def test_a_duplicate_name_is_refused(registry: SubscriptionRegistry) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    duplicate = SubscriptionRecord.new("robot", "scope.*")
    with pytest.raises(DuplicateSubscriptionError) as excinfo:
        registry.add(duplicate)
    assert excinfo.value.remediation


def test_concurrent_adds_of_one_name_refuse_exactly_one(registry: SubscriptionRegistry) -> None:
    """The duplicate check must be atomic, not check-then-write.

    The sequential test above passes just as happily against an
    ``exists()``-then-:func:`os.replace` implementation, because nothing races
    it. This one does race it, and that version failed two ways at once: across
    processes both adds passed the existence check and the second silently
    clobbered the first, and *within* one process the two writers shared a
    pid-named temp file, so one raised a bare ``FileNotFoundError`` — a
    traceback escaping a CLI that promises none — after overwriting the other's
    content. The record that survived could belong to the add that had *not*
    reported success.

    So: exactly one add succeeds, the other raises
    :class:`DuplicateSubscriptionError`, and the stored record is the one whose
    caller was told it had registered.
    """
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def add(pattern: str) -> None:
        record = SubscriptionRecord.new("robot", pattern)
        barrier.wait()  # widen the window both writers are inside
        try:
            registry.add(record)
            result = ("ok", pattern)
        except DuplicateSubscriptionError:
            result = ("refused", pattern)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=add, args=(p,)) for p in ("task.*", "scope.*")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    accepted = [pattern for status, pattern in outcomes if status == "ok"]
    refused = [pattern for status, pattern in outcomes if status == "refused"]
    assert len(accepted) == 1, f"expected exactly one add to win, got {outcomes}"
    assert len(refused) == 1, f"expected exactly one refusal, got {outcomes}"

    stored = registry.list()
    assert [record.name for record in stored] == ["robot"]
    # The winner's own record is what landed — not the loser's content under
    # the winner's name, which is what the shared temp file used to produce.
    assert stored[0].pattern == accepted[0]
    assert not list(registry.root.glob("*.tmp")), "a temp sibling was left behind"


def test_removing_an_unknown_name_is_a_named_error(registry: SubscriptionRegistry) -> None:
    with pytest.raises(UnknownSubscriptionError):
        registry.remove("robot")


def test_remove_returns_the_record_it_deleted(registry: SubscriptionRegistry) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*", owner="builder"))
    removed = registry.remove("robot")
    assert removed.owner == "builder"
    assert registry.list() == ()


def test_the_registry_writes_no_leftover_temp_files(registry: SubscriptionRegistry) -> None:
    """Atomic writes are a temp sibling plus os.replace — the sibling never survives."""
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    assert [path.name for path in sorted(registry.root.iterdir())] == ["robot.json"]


def test_a_record_file_is_written_atomically_and_completely(
    registry: SubscriptionRegistry,
) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    payload = json.loads((registry.root / "robot.json").read_text(encoding="utf-8"))
    assert payload["name"] == "robot"
    assert payload["registryFormatVersion"] in SUPPORTED_REGISTRY_FORMATS


def test_a_corrupt_record_is_a_named_error_naming_the_file(
    registry: SubscriptionRegistry,
) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    (registry.root / "robot.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryCorruptError) as excinfo:
        registry.get("robot")
    assert "robot.json" in str(excinfo.value)


def test_a_record_from_a_newer_build_is_reported_not_reinterpreted(
    registry: SubscriptionRegistry,
) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    path = registry.root / "robot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["registryFormatVersion"] = "99"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryFormatError):
        registry.get("robot")


def test_a_record_missing_a_required_key_is_corrupt(registry: SubscriptionRegistry) -> None:
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    path = registry.root / "robot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["owner"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryCorruptError):
        registry.get("robot")


def test_a_record_whose_filename_disagrees_with_its_name_is_corrupt(
    registry: SubscriptionRegistry,
) -> None:
    """The filename is the key; a mismatch means ``get`` would answer the wrong record."""
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    path = registry.root / "robot.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = "impostor"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryCorruptError):
        registry.get("robot")


def test_the_registry_lives_beside_the_history_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-host state, following t6's convention rather than inventing a second one."""
    monkeypatch.delenv("EVENTS_HISTORY_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-example")
    assert default_registry_dir().parent == default_history_dir()
    assert default_registry_dir().is_absolute()


def test_the_registry_dir_follows_the_history_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENTS_HISTORY_DIR", str(tmp_path / "store"))
    assert default_registry_dir() == tmp_path / "store" / "registry"


def test_open_registry_defaults_to_the_per_host_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENTS_HISTORY_DIR", str(tmp_path / "store"))
    assert open_registry().root == default_registry_dir()


def test_an_empty_registry_lists_nothing_without_creating_anything(tmp_path: Path) -> None:
    root = tmp_path / "never-written"
    assert SubscriptionRegistry(root).list() == ()
    assert not root.exists()


# =========================================================================
# The client-id derivation — the session's identity in the broker
# =========================================================================


def test_the_client_id_is_a_pure_function_of_the_name() -> None:
    """Pin the exact derivation, not ``f(x) == f(x)``.

    Comparing two calls in one process is nearly vacuous: it catches only
    per-call randomness, and still passes when the id is derived from the pid,
    the host or the cwd — which is the failure that actually orphans a session
    (``test_the_client_id_is_stable_across_processes`` covers that half).
    Asserting the derived value itself is what pins the contract.
    """
    assert client_id_for("robot") == f"{CLIENT_ID_PREFIX}robot"


def test_two_names_never_share_a_client_id() -> None:
    names = ["robot", "robot2", "robot-2", "robot.2", "robot_2", "a" * MAX_NAME_LENGTH]
    assert len({client_id_for(name) for name in names}) == len(names)


def test_the_client_id_is_stable_across_processes() -> None:
    """A random or pid-derived id would orphan the session on every ``sub add``."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from events_cli.subs import client_id_for;print(client_id_for('r'))",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "PYTHONPATH": _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == client_id_for("r")


def test_the_client_id_is_not_the_per_process_random_client_default() -> None:
    from events_cli.client import _default_client_id

    assert client_id_for("robot") != _default_client_id()
    assert str(os.getpid()) not in client_id_for("robot")


def test_the_client_id_stays_within_a_conservative_length_bound() -> None:
    longest = client_id_for("a" * MAX_NAME_LENGTH)
    assert len(longest.encode("utf-8")) <= 128


def test_the_client_id_rejects_an_invalid_name() -> None:
    with pytest.raises(SubscriptionValidationError):
        client_id_for("../escape")


# =========================================================================
# Criterion 2 — the MQTT persistent-session lifecycle
# =========================================================================


def test_sub_add_connects_with_clean_start_false_and_an_infinite_session_expiry(
    registry: SubscriptionRegistry,
) -> None:
    factory = fake_factory()
    add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    fake = factory.made[0]
    connect = fake.kwargs("connect")
    assert connect["clean_start"] is False
    assert connect["properties"].SessionExpiryInterval == SESSION_EXPIRY_INFINITE
    assert SESSION_EXPIRY_INFINITE == 0xFFFFFFFF


def test_sub_add_subscribes_at_qos_1_on_the_compiled_filter(
    registry: SubscriptionRegistry,
) -> None:
    factory = fake_factory()
    record = add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    subscribe = factory.made[0].kwargs("subscribe")
    assert subscribe["topic"] == record.topic_filter == "events/task/+"
    assert subscribe["qos"] == QOS_AT_LEAST_ONCE == 1


def test_sub_add_disconnects_gracefully_leaving_the_session_live(
    registry: SubscriptionRegistry,
) -> None:
    """Subscribe then a *graceful* DISCONNECT — no expiry-0 property on the way out.

    A DISCONNECT carrying ``SessionExpiryInterval=0`` would end the session we
    just created; the whole architecture depends on it outliving the process.
    """
    factory = fake_factory()
    add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    fake = factory.made[0]
    assert fake.sequence == ["connect", "loop_start", "subscribe", "disconnect", "loop_stop"]
    assert fake.kwargs("disconnect")["properties"] is None


def test_sub_add_uses_the_stable_client_id_not_a_random_one(
    registry: SubscriptionRegistry,
) -> None:
    factory = fake_factory()
    record = add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    assert factory.made[0].client_id == record.client_id == client_id_for("robot")


def test_sub_add_persists_the_record_after_the_session_is_established(
    registry: SubscriptionRegistry,
) -> None:
    factory = fake_factory()
    record = add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    assert registry.get("robot") == record


def test_no_record_is_written_when_the_session_cannot_be_established(
    registry: SubscriptionRegistry,
) -> None:
    """A registry entry claiming a session the broker never got is worse than an error."""
    factory = fake_factory(connect_error=ConnectionRefusedError(111, "Connection refused"))
    with pytest.raises(BrokerUnreachableError):
        add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    assert registry.list() == ()


def test_no_session_is_opened_when_the_name_or_pattern_is_invalid(
    registry: SubscriptionRegistry,
) -> None:
    factory = fake_factory()
    with pytest.raises(SubscriptionValidationError):
        add_subscription("bad/name", "task.*", registry=registry, client_factory=factory)
    assert factory.made == []
    assert registry.list() == ()


def test_sub_add_refuses_a_name_already_registered(registry: SubscriptionRegistry) -> None:
    factory = fake_factory()
    add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    with pytest.raises(DuplicateSubscriptionError):
        add_subscription("robot", "scope.*", registry=registry, client_factory=factory)
    assert len(factory.made) == 1  # the duplicate never touched the broker


def test_sub_remove_destroys_the_session_with_clean_start_and_zero_expiry(
    registry: SubscriptionRegistry,
) -> None:
    """Both halves: clean_start discards the old session, expiry 0 stops a new one forming."""
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    factory = fake_factory()
    remove_subscription("robot", registry=registry, client_factory=factory)
    connect = factory.made[0].kwargs("connect")
    assert connect["clean_start"] is True
    assert connect["properties"].SessionExpiryInterval == SESSION_EXPIRY_DESTROY == 0


def test_sub_remove_never_subscribes(registry: SubscriptionRegistry) -> None:
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    factory = fake_factory()
    remove_subscription("robot", registry=registry, client_factory=factory)
    assert factory.made[0].sequence == ["connect", "loop_start", "disconnect", "loop_stop"]


def test_sub_remove_uses_the_same_client_id_the_session_was_created_with(
    registry: SubscriptionRegistry,
) -> None:
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    factory = fake_factory()
    record = remove_subscription("robot", registry=registry, client_factory=factory)
    assert factory.made[0].client_id == record.client_id


def test_sub_remove_deletes_the_record(registry: SubscriptionRegistry) -> None:
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    remove_subscription("robot", registry=registry, client_factory=fake_factory())
    assert registry.list() == ()
    assert get_subscription("robot", registry=registry) is None


def test_sub_remove_keeps_the_record_when_the_broker_is_unreachable(
    registry: SubscriptionRegistry,
) -> None:
    """Dropping the record would orphan a live broker-side queue with no way back."""
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    factory = fake_factory(connect_error=ConnectionRefusedError(111, "Connection refused"))
    with pytest.raises(BrokerUnreachableError):
        remove_subscription("robot", registry=registry, client_factory=factory)
    assert [record.name for record in registry.list()] == ["robot"]


def test_sub_remove_force_deletes_the_record_with_the_broker_down(
    registry: SubscriptionRegistry,
) -> None:
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    factory = fake_factory(connect_error=ConnectionRefusedError(111, "Connection refused"))
    remove_subscription("robot", registry=registry, client_factory=factory, force=True)
    assert registry.list() == ()


def test_sub_remove_reports_an_unknown_subscription(registry: SubscriptionRegistry) -> None:
    factory = fake_factory()
    with pytest.raises(UnknownSubscriptionError):
        remove_subscription("robot", registry=registry, client_factory=factory)


def test_list_subscriptions_reads_the_registry(registry: SubscriptionRegistry) -> None:
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    assert [record.name for record in list_subscriptions(registry=registry)] == ["robot"]


def test_the_broker_address_defaults_to_the_loopback_stack() -> None:
    address = BrokerAddress()
    assert (address.host, address.port) == ("127.0.0.1", 1883)


def test_a_custom_broker_address_reaches_connect(registry: SubscriptionRegistry) -> None:
    factory = fake_factory()
    add_subscription(
        "robot",
        "task.*",
        registry=registry,
        address=BrokerAddress("10.0.0.5", 18831, keepalive=17),
        client_factory=factory,
    )
    connect = factory.made[0].kwargs("connect")
    assert (connect["host"], connect["port"], connect["keepalive"]) == ("10.0.0.5", 18831, 17)


# --- the session seam itself (what t8's drain reuses) ----------------------


def test_the_session_reports_session_present_on_resume() -> None:
    """t8 resumes the same session; ``session_present`` is how it knows it did."""
    factory = fake_factory(session_present=True)
    session = PersistentSession("events-cli-sub-robot", client_factory=factory)
    try:
        assert session.open() is True
        assert session.session_present is True
    finally:
        session.close()


def test_a_fresh_session_reports_session_present_false() -> None:
    session = PersistentSession("events-cli-sub-robot", client_factory=fake_factory())
    try:
        assert session.open() is False
    finally:
        session.close()


def test_the_session_seam_passes_manual_ack_through_for_the_drain() -> None:
    """t8's persist-then-ack drain needs ``manual_ack=True`` on the same seam."""
    factory = fake_factory()
    session = PersistentSession("events-cli-sub-robot", manual_ack=True, client_factory=factory)
    try:
        session.open()
        assert factory.made[0].manual_ack is True
    finally:
        session.close()


def test_the_default_seam_does_not_request_manual_ack() -> None:
    factory = fake_factory()
    session = PersistentSession("events-cli-sub-robot", client_factory=factory)
    try:
        session.open()
        assert factory.made[0].manual_ack is False
    finally:
        session.close()


def test_the_session_exposes_its_client_for_the_drain_to_attach_on_message() -> None:
    factory = fake_factory()
    with PersistentSession("events-cli-sub-robot", client_factory=factory) as session:
        session.open()
        assert session.client is factory.made[0]


def test_touching_the_client_before_open_is_a_named_error() -> None:
    session = PersistentSession("events-cli-sub-robot", client_factory=fake_factory())
    with pytest.raises(SessionError):
        _ = session.client


def test_the_session_is_a_context_manager_that_always_closes() -> None:
    factory = fake_factory()
    with PersistentSession("events-cli-sub-robot", client_factory=factory) as session:
        session.open()
    assert factory.made[0].sequence[-2:] == ["disconnect", "loop_stop"]


def test_closing_an_unopened_session_is_a_no_op() -> None:
    factory = fake_factory()
    PersistentSession("events-cli-sub-robot", client_factory=factory).close()
    assert factory.made == []


def test_close_is_idempotent() -> None:
    factory = fake_factory()
    session = PersistentSession("events-cli-sub-robot", client_factory=factory)
    session.open()
    session.close()
    session.close()
    assert factory.made[0].sequence.count("disconnect") == 1


def test_a_refused_connack_is_a_named_session_error() -> None:
    session = PersistentSession("events-cli-sub-robot", client_factory=fake_factory(refuse=True))
    with pytest.raises(SessionError) as excinfo:
        session.open()
    assert "Not authorized" in str(excinfo.value)


def test_a_connack_that_never_arrives_times_out_as_broker_unreachable() -> None:
    session = PersistentSession(
        "events-cli-sub-robot", client_factory=fake_factory(silent=True), connect_timeout=0.05
    )
    with pytest.raises(BrokerUnreachableError):
        session.open()


def test_a_failed_subscribe_is_a_named_session_error() -> None:
    session = PersistentSession("events-cli-sub-robot", client_factory=fake_factory(subscribe_rc=4))
    try:
        session.open()
        with pytest.raises(SessionError):
            session.subscribe("events/task/+")
    finally:
        session.close()


def test_a_broker_unreachable_error_is_a_subs_error() -> None:
    """t9 maps the whole family onto exit codes; one base class is what makes that possible."""
    assert issubclass(BrokerUnreachableError, SessionError)
    for cls in (
        SessionError,
        SubscriptionValidationError,
        DuplicateSubscriptionError,
        UnknownSubscriptionError,
        RegistryCorruptError,
        RegistryFormatError,
    ):
        assert issubclass(cls, SubsError)


def test_every_subs_error_carries_a_remediation(registry: SubscriptionRegistry) -> None:
    factory = fake_factory(connect_error=ConnectionRefusedError(111, "Connection refused"))
    with pytest.raises(BrokerUnreachableError) as excinfo:
        add_subscription("robot", "task.*", registry=registry, client_factory=factory)
    assert "events up" in excinfo.value.remediation


def test_the_default_client_factory_builds_an_mqtt5_client_with_the_stable_id() -> None:
    """The real factory: MQTT5 (session expiry is an MQTT5 property) and callback API v2."""
    import paho.mqtt.client as mqtt

    client = default_client_factory(mqtt, "events-cli-sub-robot", manual_ack=False)
    assert client._protocol == mqtt.MQTTv5
    assert client._client_id == b"events-cli-sub-robot"


def test_the_default_client_factory_honours_manual_ack() -> None:
    import paho.mqtt.client as mqtt

    client = default_client_factory(mqtt, "events-cli-sub-robot", manual_ack=True)
    assert client._manual_ack is True


# --- the lazy-import boundary ---------------------------------------------


def test_subs_never_imports_paho_at_module_scope() -> None:
    """Static (AST) so it also catches branches this suite never runs."""
    for path in subs_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.iter_child_nodes(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f"{path.name}: use absolute imports"
                names = [node.module or ""]
            for name in names:
                assert not name.startswith("paho"), f"{path.name}: paho imported at module scope"


def test_subs_imports_no_cli_module() -> None:
    """The registry is a domain package: four surfaces consume it, one has exit codes."""
    for path in subs_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                # Prefix-with-separator, not a bare prefix: `events_cli.client`
                # is the transport lane and is allowed, `events_cli.cli` is not.
                assert name != "events_cli.cli" and not name.startswith(
                    "events_cli.cli."
                ), f"{path.name}: subs may not import {name}"


def test_the_registry_lane_works_with_paho_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Records, validation and listing never need the transport client."""
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        monkeypatch.setitem(sys.modules, name, None)
    registry = SubscriptionRegistry(tmp_path)
    registry.add(SubscriptionRecord.new("robot", "task.*", owner="builder"))
    assert [record.name for record in registry.list()] == ["robot"]


def test_opening_a_session_without_paho_names_the_missing_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from events_cli.client import MqttDependencyError

    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        monkeypatch.setitem(sys.modules, name, None)
    session = PersistentSession("events-cli-sub-robot")
    with pytest.raises(MqttDependencyError) as excinfo:
        session.open()
    assert "paho-mqtt" in str(excinfo.value)


_LAZY_SNIPPET = """
import sys
assert "paho" not in sys.modules
import events_cli.subs as subs
assert "paho" not in sys.modules, "importing events_cli.subs eagerly imported paho"
record = subs.SubscriptionRecord.new("robot", "task.*", owner="builder")
assert record.topic_filter == "events/task/+"
assert "paho" not in sys.modules, "building a record imported paho"
print("SUBS_LAZY_OK")
"""


def test_importing_the_subs_package_never_imports_paho() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _LAZY_SNIPPET],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "SUBS_LAZY_OK" in proc.stdout


# =========================================================================
# Criterion 3 — name and pattern validation, with field-level errors
# =========================================================================


@pytest.mark.parametrize(
    "name", ["robot", "r", "reachy-mini", "reachy_mini", "v1.robot", "a" * MAX_NAME_LENGTH]
)
def test_a_slug_is_a_valid_subscription_name(name: str) -> None:
    assert SubscriptionRecord.new(name, "task.*").name == name


@pytest.mark.parametrize("char", ["#", "+", "/"])
def test_a_name_with_a_raw_mqtt_filter_character_is_rejected(char: str) -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new(f"ro{char}bot", "task.*")
    assert "reserved_mqtt_char" in {err.code for err in excinfo.value.errors}
    assert excinfo.value.fields == ("name",) * len(excinfo.value.errors)


def test_a_name_containing_dot_dot_is_rejected() -> None:
    """``..`` is the traversal segment: the name becomes a filename in the store."""
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("ro..bot", "task.*")
    assert "malformed" in {err.code for err in excinfo.value.errors}


@pytest.mark.parametrize("name", ["..", "../escape", "a/../b"])
def test_a_traversal_shaped_name_can_never_address_outside_the_store(name: str) -> None:
    with pytest.raises(SubscriptionValidationError):
        SubscriptionRecord.new(name, "task.*")


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("", "task.*")
    assert "empty" in {err.code for err in excinfo.value.errors}


def test_a_too_long_name_is_rejected() -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("a" * (MAX_NAME_LENGTH + 1), "task.*")
    assert "too_long" in {err.code for err in excinfo.value.errors}


def test_a_non_string_name_is_rejected() -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new(7, "task.*")  # type: ignore[arg-type]
    assert "not_a_string" in {err.code for err in excinfo.value.errors}


@pytest.mark.parametrize("name", ["Robot", ".robot", "-robot", "ro bot", "ro:bot", "ro\nbot"])
def test_a_name_outside_the_slug_grammar_is_rejected(name: str) -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new(name, "task.*")
    assert "malformed" in {err.code for err in excinfo.value.errors}


def test_every_validation_error_code_is_declared_in_the_core_taxonomy() -> None:
    """A caller switching on codes must never meet one this repo did not declare."""
    seen: set[str] = set()
    for bad in ["", "Robot", "ro#bot", "ro..bot", "a" * 200, 7]:
        with pytest.raises(SubscriptionValidationError) as excinfo:
            SubscriptionRecord.new(bad, "task.*")  # type: ignore[arg-type]
        seen.update(err.code for err in excinfo.value.errors)
    assert seen
    assert seen <= set(ERROR_CODES)


def test_every_valid_registry_name_is_also_a_valid_history_subscription() -> None:
    """The drain appends under this exact name; two grammars would silently diverge."""
    from events_cli.history import _require_sub

    for name in ["robot", "r", "reachy-mini", "reachy_mini", "v1.robot", "a" * MAX_NAME_LENGTH]:
        assert _require_sub(SubscriptionRecord.new(name, "task.*").name) == name


def test_a_validation_error_renders_field_level_json() -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("ro#bot", "task.*")
    payload = excinfo.value.to_dict()
    assert payload["error"] == "subscription_validation"
    assert all({"field", "code", "message"} == set(err) for err in payload["errors"])


@pytest.mark.parametrize("pattern", ["task.#", "task.+", "task/done", "events/#", "#"])
def test_a_pattern_with_a_raw_mqtt_filter_character_is_rejected(pattern: str) -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("robot", pattern)
    assert "reserved_mqtt_char" in {err.code for err in excinfo.value.errors}
    assert set(excinfo.value.fields) == {"pattern"}


@pytest.mark.parametrize("pattern", ["", "task..done", ".task", "task."])
def test_an_empty_or_ragged_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("robot", pattern)
    assert set(excinfo.value.fields) == {"pattern"}


def test_a_non_string_pattern_is_rejected() -> None:
    with pytest.raises(SubscriptionValidationError):
        SubscriptionRecord.new("robot", None)  # type: ignore[arg-type]


def test_name_and_pattern_problems_are_reported_together_in_one_pass() -> None:
    """One retry per broken field is exactly what the envelope core refuses to make callers do."""
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("ro#bot", "task.#")
    assert set(excinfo.value.fields) == {"name", "pattern"}


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("task.requested", "events/task/requested"),
        ("task.*", "events/task/+"),
        ("*", "events/+"),
        ("*.*", "events/+/+"),
        ("scope_completed", "events/scope_completed"),
        ("reachy.state.updated", "events/reachy/state/updated"),
    ],
)
def test_the_compiled_filter_is_always_events_prefixed(pattern: str, expected: str) -> None:
    record = SubscriptionRecord.new("robot", pattern)
    assert record.topic_filter == expected
    assert record.topic_filter.startswith("events/")


def test_a_compiled_filter_can_never_reach_a_producer_owned_topic() -> None:
    """Even a type spelled ``reachy.state.updated`` stays inside the contract lane."""
    record = SubscriptionRecord.new("robot", "reachy.state.*")
    assert filter_matches_topic(record.topic_filter, "events/reachy/state/updated") is True
    assert filter_matches_topic(record.topic_filter, "reachy/state/updated") is False


def test_the_wildcard_never_compiles_to_the_multi_level_wildcard() -> None:
    """``#`` would silently widen as producers add segments; ``+`` cannot."""
    record = SubscriptionRecord.new("robot", "task.*")
    assert "#" not in record.topic_filter
    assert filter_matches_topic(record.topic_filter, "events/task/sub/completed") is False


def test_a_stored_record_keeps_the_filter_the_session_was_created_with(
    registry: SubscriptionRegistry,
) -> None:
    """The broker holds a subscription on this literal string; re-deriving it later could drift."""
    add_subscription("robot", "task.*", registry=registry, client_factory=fake_factory())
    assert registry.get("robot").topic_filter == "events/task/+"


def test_the_registry_validates_names_on_every_lookup_path(
    registry: SubscriptionRegistry,
) -> None:
    for verb in (registry.get, registry.remove):
        with pytest.raises(SubscriptionValidationError):
            verb("../escape")


def test_a_field_error_from_this_layer_is_the_core_field_error_type() -> None:
    with pytest.raises(SubscriptionValidationError) as excinfo:
        SubscriptionRecord.new("ro#bot", "task.*")
    assert all(isinstance(err, FieldError) for err in excinfo.value.errors)


# =========================================================================
# The trust boundary: nothing read back from disk becomes a traceback
# =========================================================================


def test_a_record_that_is_not_a_json_object_is_corrupt() -> None:
    with pytest.raises(RegistryCorruptError):
        SubscriptionRecord.from_dict(["not", "an", "object"])  # type: ignore[arg-type]


def test_a_record_field_of_the_wrong_type_is_corrupt() -> None:
    payload = SubscriptionRecord.new("robot", "task.*").to_dict()
    payload["created"] = 1753350000
    with pytest.raises(RegistryCorruptError):
        SubscriptionRecord.from_dict(payload)


def test_a_record_file_that_cannot_be_read_is_corrupt_not_a_traceback(
    registry: SubscriptionRegistry,
) -> None:
    """An unreadable record names itself; it is never skipped, which would hide a live queue."""
    registry.add(SubscriptionRecord.new("robot", "task.*"))
    (registry.root / "broken.json").mkdir()
    with pytest.raises(RegistryCorruptError) as excinfo:
        registry.list()
    assert "broken.json" in str(excinfo.value)


def test_names_lists_keys_without_parsing_the_records(registry: SubscriptionRegistry) -> None:
    registry.add(SubscriptionRecord.new("beta", "task.*"))
    registry.add(SubscriptionRecord.new("alpha", "scope.*"))
    assert registry.names() == ("alpha", "beta")


def test_names_is_empty_on_a_registry_that_was_never_written(tmp_path: Path) -> None:
    assert SubscriptionRegistry(tmp_path / "absent").names() == ()


def test_the_session_defaults_are_the_durable_ones() -> None:
    """``open()`` with no arguments must resume-and-never-expire; anything else is opt-in."""
    factory = fake_factory()
    session = PersistentSession("events-cli-sub-robot", client_factory=factory)
    try:
        session.open()
        connect = factory.made[0].kwargs("connect")
        assert connect["clean_start"] is False
        assert connect["properties"].SessionExpiryInterval == SESSION_EXPIRY_INFINITE
    finally:
        session.close()


def test_close_swallows_a_failing_disconnect() -> None:
    """close() runs from a finally; one that raised would mask the real failure."""
    factory = fake_factory()
    session = PersistentSession("events-cli-sub-robot", client_factory=factory)
    session.open()
    fake = factory.made[0]
    fake.disconnect = _raising  # type: ignore[method-assign]
    fake.loop_stop = _raising  # type: ignore[method-assign]
    session.close()  # must not raise


def _raising(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("socket already gone")
