"""Where the broker is — the one place a default broker address is resolved.

Two lanes open a connection without being told where to point it: the importable
producer (:class:`events_cli.client.EventClient`'s constructor defaults) and a
durable subscription's session
(:class:`events_cli.subs.session.BrokerAddress`'s field defaults). Each used to
carry its own ``127.0.0.1`` / ``1883`` literals, so "the default broker" was two
constants that happened to agree. They now resolve through here, which buys two
things:

* **one definition of the default**, so the producer lane and the subscription
  lane cannot drift apart; and
* **an environment override** — ``EVENTS_BROKER_HOST`` / ``EVENTS_BROKER_PORT``
  — in the same shape as ``EVENTS_STACK_DIR``
  (:func:`events_cli.stack.default_stack_dir`) and ``EVENTS_HISTORY_DIR``
  (:func:`events_cli.history.default_history_dir`). Unset means the literal
  default, so nothing changes for a caller that never sets them.

Only a *default* is resolved here. An explicit ``EventClient("10.0.0.5", 1884)``
or ``BrokerAddress("broker.internal", 1884)`` still wins outright — the override
answers "where is the broker when nobody said", never "override what the caller
asked for".

Why an environment variable and not a flag
------------------------------------------
``events up`` runs one broker per host on ``127.0.0.1:1883`` and that is the
deployment model, so the address is a property of the host a process is talking
to rather than a per-invocation choice. The override exists so a *second*
broker can be addressed without editing anything — which is precisely what the
docker-backed integration suite needs, because it must never bind or address
host 1883, where the real stack (and, on the development box, a live robot's
event broker) already lives. A CLI flag would have to be threaded through every
verb that reaches a broker and would imply a per-call decision; remote access as
a supported, documented surface is
`#10 <https://github.com/agentculture/events-cli/issues/10>`_'s, and it will
revisit the flag question properly. This is deliberately the smallest thing that
works.

An unreadable override is fatal, never a silent fallback
--------------------------------------------------------
``EVENTS_BROKER_PORT=abc`` raises :class:`BrokerAddressError`. Falling back to
1883 is the dangerous reading of a typo: it would quietly send traffic to
whatever broker holds the default port, which is exactly the accident the
override exists to prevent. An *empty* value is treated as unset, because an
exported-but-empty variable is far more often shell plumbing than a request to
connect to the empty string.

Standard library only, and safe to import at module scope from both lanes:
nothing here pulls in paho, so the no-install introspection lane is unaffected.
"""

from __future__ import annotations

import os

from events_cli.core.errors import EventsError

__all__ = [
    "BROKER_HOST_ENV",
    "BROKER_PORT_ENV",
    "DEFAULT_BROKER_HOST",
    "DEFAULT_BROKER_PORT",
    "BrokerAddressError",
    "default_broker_host",
    "default_broker_port",
]

#: Environment override for the default broker host.
BROKER_HOST_ENV = "EVENTS_BROKER_HOST"
#: Environment override for the default broker port.
BROKER_PORT_ENV = "EVENTS_BROKER_PORT"

#: The loopback address ``events up`` publishes the broker on.
DEFAULT_BROKER_HOST = "127.0.0.1"
#: The port ``events up`` publishes the broker on.
DEFAULT_BROKER_PORT = 1883

_HINT = (
    f"unset {BROKER_PORT_ENV} to use the default {DEFAULT_BROKER_PORT} that 'events up' "
    f"publishes, or set it to a TCP port number in 1..65535 (with {BROKER_HOST_ENV} if the "
    "broker is not on 127.0.0.1)"
)


class BrokerAddressError(EventsError):
    """A broker-address override that cannot be used as an address.

    An *environment* fault (exit 2 at the CLI edge, see
    :func:`events_cli.cli._dispatch`): the invocation was fine, the environment
    the process was handed is not usable. Carries a ``remediation`` for exactly
    that translation, the same way :class:`events_cli.history.HistoryError` and
    :class:`events_cli.subs.SubsError` do.
    """

    def __init__(self, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.remediation = remediation


def default_broker_host() -> str:
    """The broker host to use when the caller named none.

    ``EVENTS_BROKER_HOST`` overrides :data:`DEFAULT_BROKER_HOST`; an empty or
    whitespace-only value counts as unset. No validation beyond that — a host
    that does not resolve is a connection failure the lanes already report
    (``BrokerUnreachableError`` for a session, ``ok=False`` for a publish), not
    something to second-guess here.
    """
    override = os.environ.get(BROKER_HOST_ENV, "").strip()
    return override or DEFAULT_BROKER_HOST


def default_broker_port() -> int:
    """The broker port to use when the caller named none.

    ``EVENTS_BROKER_PORT`` overrides :data:`DEFAULT_BROKER_PORT`; an empty or
    whitespace-only value counts as unset. Anything else that is not a TCP port
    number raises :class:`BrokerAddressError` rather than falling back — see the
    module docstring for why silence here would be the dangerous choice.
    """
    raw = os.environ.get(BROKER_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_BROKER_PORT
    try:
        port = int(raw)
    except ValueError:
        raise BrokerAddressError(
            f"{BROKER_PORT_ENV}={raw!r} is not a number", remediation=_HINT
        ) from None
    if not 0 < port < 65536:
        raise BrokerAddressError(
            f"{BROKER_PORT_ENV}={raw!r} is not a TCP port (must be in 1..65535)",
            remediation=_HINT,
        )
    return port
