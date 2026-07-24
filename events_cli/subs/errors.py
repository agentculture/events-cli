"""Domain errors for the subscription registry and its session lifecycle.

Like :mod:`events_cli.core.errors`, :mod:`events_cli.history.errors` and
:class:`events_cli.stack.StackError`, this layer raises **its own** error type
and never :class:`events_cli.cli._errors.CliError`: four surfaces consume the
registry and only one of them has exit codes. The CLI translates at its edge
(task t9).

The exit code each class is meant to become — the contract the CLI verb
implements, recorded here so the translation is not invented at the boundary:

=====================================  ====  ========================================
error                                  exit  meaning
=====================================  ====  ========================================
:class:`SubscriptionValidationError`   1     the caller's name, pattern or bound is
                                             invalid
:class:`DuplicateSubscriptionError`    1     that name is already registered
:class:`UnknownSubscriptionError`      1     no subscription by that name
:class:`RegistryCorruptError`          2     the registry on disk is damaged
:class:`RegistryFormatError`           2     a record was written by a newer build
:class:`SessionError`                  2     the broker refused or dropped us
:class:`BrokerUnreachableError`        2     the broker is not there at all
:class:`DrainError`                    2     the drain could not store what it read
=====================================  ====  ========================================

Exit 1 is "you asked for the wrong thing"; exit 2 is "the environment is not in
a state where the right thing can happen". Every error carries a
``remediation`` string for exactly that translation, because the CLI's
``error:``/``hint:`` output needs a hint and the boundary would only be
guessing at a fault this layer already understands.
"""

from __future__ import annotations

from typing import Any, Iterable

from events_cli.core.errors import EventsError, FieldError

__all__ = [
    "BrokerUnreachableError",
    "DrainError",
    "DuplicateSubscriptionError",
    "RegistryCorruptError",
    "RegistryFormatError",
    "SessionError",
    "SubsError",
    "SubscriptionValidationError",
    "UnknownSubscriptionError",
]


class SubsError(EventsError):
    """Base class for every subscription-registry failure.

    Subclasses :class:`~events_cli.core.errors.EventsError` so a caller that
    already handles the event domain catches registry faults too — and so the
    CLI needs exactly one ``except`` clause per layer, not one per error.
    """

    def __init__(self, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.remediation = remediation


class SubscriptionValidationError(SubsError):
    """A subscription name or pattern that violates the boundary grammar.

    Carries every problem found in **one pass** as :class:`FieldError` objects,
    the same shape :class:`~events_cli.core.errors.EnvelopeValidationError` and
    :class:`~events_cli.core.errors.TopicValidationError` use: an agent fixing a
    ``sub add`` should not have to retry once per broken argument.

    This is a containment boundary as much as a validation rule. A name becomes
    a filename in the registry *and* an MQTT client id; a pattern becomes a
    topic filter. Rejecting ``..`` and ``/`` is what stops a name addressing a
    path outside the store, and rejecting ``#``/``+`` is what stops a pattern
    subscribing outside the ``events/`` contract lane.
    """

    def __init__(
        self,
        errors: Iterable[FieldError],
        *,
        summary: str = "invalid subscription",
        remediation: str = "",
    ) -> None:
        self.errors: tuple[FieldError, ...] = tuple(errors)
        self.summary = summary
        detail = "; ".join(str(err) for err in self.errors)
        super().__init__(
            f"{summary}: {detail}" if detail else summary,
            remediation=remediation or _VALIDATION_HINT,
        )

    @property
    def fields(self) -> tuple[str, ...]:
        """The field names of the rejected values, in report order."""
        return tuple(err.field for err in self.errors)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready shape for the ``--json`` surfaces."""
        return {
            "error": "subscription_validation",
            "message": self.summary,
            "errors": [err.to_dict() for err in self.errors],
        }


_VALIDATION_HINT = (
    "a subscription name is a lowercase slug (letters, digits, '.', '_', '-') and a "
    "pattern is a dotted event type where '*' stands for one segment, e.g. "
    "'events sub add robot task.*'"
)


class DuplicateSubscriptionError(SubsError):
    """That name is already registered.

    Refused rather than silently re-registered: the existing record names the
    broker session that is *already* queueing events, and overwriting it would
    change the pattern out from under a session that keeps the old filter until
    something re-subscribes it.
    """


class UnknownSubscriptionError(SubsError):
    """No subscription is registered under that name."""


class RegistryCorruptError(SubsError):
    """A registry record could not be read back as it was written.

    An *environment* fault, not a user error: nothing the caller passed caused
    it and no retry with different arguments will fix it. Never silently
    skipped — a subscription that vanishes from ``sub list`` while its broker
    session keeps queueing is a leak with no visible cause.
    """


class RegistryFormatError(RegistryCorruptError):
    """A record carries a ``registryFormatVersion`` this build cannot read.

    Deliberately a corruption subclass — from the reader's point of view a
    record it cannot interpret is unreadable, whatever the reason — but its own
    class, so an operator gets "written by a newer events-cli" rather than
    "damaged", which are very different things to act on.
    """


class SessionError(SubsError):
    """The broker refused, dropped, or never completed the persistent session."""


class BrokerUnreachableError(SessionError):
    """The broker is not accepting connections at all.

    Split from its parent because it is the one failure with an obvious next
    command (``events up``), and because it is the failure a co-located agent
    hits most: the stack simply is not running yet.
    """


class DrainError(SubsError):
    """A drain could not persist (or read back) what the broker delivered.

    An *environment* fault — a full disk, a damaged history store — so exit 2,
    not exit 1: nothing the caller passed caused it and no retry with different
    arguments fixes it. It always wraps the underlying
    :class:`~events_cli.history.HistoryError` (``raise ... from``), because the
    drain's contract is that every way a drain can fail is a
    :class:`SubsError`, and the CLI verb should need exactly one ``except``.

    Raising it **stops the drain**, and it is raised only after the offending
    message has been left *unacknowledged*: the broker still owns that message
    and will redeliver it. Whatever the drain persisted and acknowledged before
    the failure is durably in the store — the batch is lost to the caller, the
    events are not, and ``HistoryStore.read`` returns them.
    """
