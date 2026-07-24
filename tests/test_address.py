"""Dockerless tests for the default broker address and its environment override.

``events_cli/address.py`` is the one place ``127.0.0.1:1883`` is written down,
and the one place ``EVENTS_BROKER_HOST`` / ``EVENTS_BROKER_PORT`` are read. Two
lanes depend on it — :class:`events_cli.client.EventClient`'s constructor
defaults and :class:`events_cli.subs.session.BrokerAddress`'s field defaults —
so the properties worth pinning are:

* **unset means unchanged.** Every default is the literal it always was.
* **an explicit host/port never consults the environment.** Proved without any
  timing: a *malformed* override is fatal, so a construction that passes an
  explicit port and does not raise cannot have looked at it.
* **a malformed override is fatal, never a fallback.** Falling back to 1883
  would silently redirect a typo'd invocation onto whatever holds the default
  port — on this machine, a **production broker for a live robot**.
* **the environment is read at construction, not at import**, so setting the
  variable inside a process still works.

Nothing here opens a socket to 1883. The one test that lets a client connect
binds its **own** listener on an ephemeral loopback port first and asserts the
client arrives *there* — which is the point of the test, and is also why it can
never reach the real broker.
"""

from __future__ import annotations

import socket

import pytest

from events_cli.address import (
    BROKER_HOST_ENV,
    BROKER_PORT_ENV,
    DEFAULT_BROKER_HOST,
    DEFAULT_BROKER_PORT,
    BrokerAddressError,
    default_broker_host,
    default_broker_port,
)
from events_cli.cli import main
from events_cli.client import EventClient
from events_cli.subs import BrokerAddress

# How long to wait for paho's background loop to reach a listener that is
# already accepting on loopback. Generous because it absorbs thread start-up on
# a contended host; it is a liveness bound, not a latency assertion, so it never
# belongs to the `perf` marker.
_CONNECT_TIMEOUT = 30.0


@pytest.fixture(autouse=True)
def _no_inherited_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean environment.

    The docker-backed suite exports both variables, so without this a stack run
    of the whole tree would silently change what "unset" means here.
    """
    monkeypatch.delenv(BROKER_HOST_ENV, raising=False)
    monkeypatch.delenv(BROKER_PORT_ENV, raising=False)


# --- unset means unchanged -------------------------------------------------


def test_unset_resolves_to_the_loopback_stack() -> None:
    assert default_broker_host() == DEFAULT_BROKER_HOST == "127.0.0.1"
    assert default_broker_port() == DEFAULT_BROKER_PORT == 1883


def test_broker_address_defaults_are_unchanged_when_unset() -> None:
    address = BrokerAddress()
    assert (address.host, address.port) == ("127.0.0.1", 1883)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_override_counts_as_unset(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """An exported-but-empty variable is shell plumbing, not a request.

    The empty string is not a host and not a port, so treating it as an override
    could only ever fail — and it would fail at connect time, far from the cause.
    """
    monkeypatch.setenv(BROKER_HOST_ENV, blank)
    monkeypatch.setenv(BROKER_PORT_ENV, blank)
    assert default_broker_host() == DEFAULT_BROKER_HOST
    assert default_broker_port() == DEFAULT_BROKER_PORT


# --- the override applies --------------------------------------------------


def test_the_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BROKER_HOST_ENV, "broker.internal")
    monkeypatch.setenv(BROKER_PORT_ENV, "18830")
    assert default_broker_host() == "broker.internal"
    assert default_broker_port() == 18830


def test_surrounding_whitespace_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BROKER_HOST_ENV, "  broker.internal  ")
    monkeypatch.setenv(BROKER_PORT_ENV, " 18830 ")
    assert default_broker_host() == "broker.internal"
    assert default_broker_port() == 18830


def test_broker_address_reads_the_environment_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set the variable, *then* build an address — the order a process actually uses.

    ``BrokerAddress`` resolves through ``default_factory`` rather than a baked-in
    signature default, so a value exported after import is still seen. A plain
    default would have frozen whatever the environment held when the module was
    first imported, which for a test process is "nothing".
    """
    monkeypatch.setenv(BROKER_HOST_ENV, "10.0.0.5")
    monkeypatch.setenv(BROKER_PORT_ENV, "18831")
    address = BrokerAddress()
    assert (address.host, address.port) == ("10.0.0.5", 18831)
    assert address.keepalive == BrokerAddress().keepalive  # unaffected


# --- an explicit address never consults the environment --------------------


def test_an_explicit_broker_address_ignores_a_broken_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed override is fatal — so *not* raising proves it was never read.

    This is deliberately not a timing test: there is no socket, no sleep and no
    race. If ``BrokerAddress`` consulted the environment when a port was given
    explicitly, ``'not-a-port'`` would raise and this test would fail.
    """
    monkeypatch.setenv(BROKER_PORT_ENV, "not-a-port")
    address = BrokerAddress("broker.internal", 1884)
    assert (address.host, address.port) == ("broker.internal", 1884)


def test_an_explicit_event_client_address_ignores_a_broken_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same proof for the producer lane. ``connect=False`` keeps it socket-free."""
    monkeypatch.setenv(BROKER_PORT_ENV, "not-a-port")
    client = EventClient("broker.internal", 1884, connect=False)
    client.close()  # never connected; close is still the honest lifecycle


# --- a malformed override is fatal -----------------------------------------


@pytest.mark.parametrize("bad", ["abc", "18.83", "1883x", "0x1883", "--1"])
def test_a_non_numeric_port_raises(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(BROKER_PORT_ENV, bad)
    with pytest.raises(BrokerAddressError) as caught:
        default_broker_port()
    assert BROKER_PORT_ENV in str(caught.value)
    assert caught.value.remediation  # every environment fault carries a hint


@pytest.mark.parametrize("bad", ["0", "-1", "65536", "99999"])
def test_a_port_outside_the_tcp_range_raises(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(BROKER_PORT_ENV, bad)
    with pytest.raises(BrokerAddressError) as caught:
        default_broker_port()
    assert "1..65535" in str(caught.value)


def test_a_broken_override_never_silently_falls_back_to_1883(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason this raises. 1883 is a live broker on the host that runs this."""
    monkeypatch.setenv(BROKER_PORT_ENV, "1883s")
    with pytest.raises(BrokerAddressError):
        default_broker_port()
    with pytest.raises(BrokerAddressError):
        BrokerAddress()
    with pytest.raises(BrokerAddressError):
        EventClient(connect=False)


# --- the CLI reports it as an environment fault, not a traceback -----------


def test_the_cli_reports_a_broken_override_as_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``events emit`` with a broken override: exit 2, structured error, no traceback.

    ``emit`` is the verb used here because it constructs the client
    unconditionally — ``watch``/``sub`` resolve a registry record first and would
    fail on the unknown name before an address was ever built. The envelope is
    valid, so the *only* thing that can fail is the address; no socket is opened
    because the resolver raises before ``EventClient`` reaches paho.
    """
    monkeypatch.setenv("EVENTS_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv(BROKER_PORT_ENV, "not-a-port")

    rc = main(["emit", "task.requested"])

    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert captured.err.startswith("error:")
    assert BROKER_PORT_ENV in captured.err
    assert "hint:" in captured.err
    assert "Traceback" not in captured.err
    # Not the generic "unexpected: ... file a bug" wrapper — that would be exit 1
    # and would blame events-cli for the caller's environment.
    assert "unexpected:" not in captured.err
    assert "file a bug" not in captured.err


# --- the producer lane really connects where the override says -------------


def test_event_client_connects_to_the_overridden_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default-constructed client arrives at the overridden port — observed, not asserted.

    A bare TCP listener on an ephemeral loopback port stands in for a broker: it
    never speaks MQTT, so the client's CONNECT goes unanswered, but the accepted
    connection proves the resolved address is what paho was actually handed —
    not merely what the resolver returned.

    Built in two steps on purpose. ``connect=False`` first, and the resolved
    address is checked *before* any socket exists: if this ever regressed to
    ignoring the environment, a one-step version would open a connection to the
    default port, which on the machine this was written on is a **production
    broker for a live robot**. Reading the two private attributes is the price
    of that gate — the client exposes no address property, and adding one is not
    this change's job.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port != DEFAULT_BROKER_PORT
        listener.settimeout(_CONNECT_TIMEOUT)

        monkeypatch.setenv(BROKER_HOST_ENV, "127.0.0.1")
        monkeypatch.setenv(BROKER_PORT_ENV, str(port))

        client = EventClient(connect=False)
        try:
            assert (client._host, client._port) == ("127.0.0.1", port), (
                "EventClient() resolved an address other than the override — refusing to "
                "open the socket, because the unresolved default is a live broker"
            )
            client.connect()
            try:
                conn, peer = listener.accept()
            except socket.timeout:  # pragma: no cover - a hung loopback connect
                raise AssertionError(
                    f"EventClient() never reached the overridden port {port}"
                ) from None
            with conn:
                assert peer[0] == "127.0.0.1"
        finally:
            client.close()
