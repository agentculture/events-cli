"""Domain errors for the history store (no CLI, no transport).

Like :mod:`events_cli.core.errors` and :class:`events_cli.stack.StackError`,
this layer raises **its own** error type and never
:class:`events_cli.cli._errors.CliError`: four surfaces consume the store and
only one of them has exit codes. The CLI translates at its edge.

The exit code each class is meant to become — the CLI verb that surfaces the
store is a later task, so this is the contract it implements:

======================================  ====  =======================================
error                                   exit  meaning
======================================  ====  =======================================
:class:`InvalidSubscriptionError`       1     the caller named a subscription badly
:class:`HistoryCorruptError`            2     the store on disk is damaged
:class:`HistoryFormatError`             2     the store was written by a newer build
======================================  ====  =======================================

Every error carries a ``remediation`` string for exactly that translation: the
CLI's structured ``error:``/``hint:`` output needs a hint, and inventing one at
the boundary would mean guessing at a fault the store already understands.
"""

from __future__ import annotations

from events_cli.core.errors import EventsError


class HistoryError(EventsError):
    """Base class for every history-store failure.

    Subclasses :class:`~events_cli.core.errors.EventsError` so a caller that
    already handles the event domain catches store faults too.
    """

    def __init__(self, message: str, *, remediation: str = "") -> None:
        super().__init__(message)
        self.remediation = remediation


class InvalidSubscriptionError(HistoryError):
    """A subscription name that is not a safe, addressable store key.

    Names become directory names, so this is a containment boundary as much as
    a validation rule: rejecting ``..`` and ``/`` here is what stops a caller
    (or a broker-supplied string) from addressing a path outside the store.
    """


class HistoryCorruptError(HistoryError):
    """A committed record could not be read back as the store wrote it.

    An *environment* fault, not a user error: nothing the caller passed caused
    it, and no retry with different arguments will fix it.
    """


class HistoryFormatError(HistoryCorruptError):
    """A record carries a ``storeFormatVersion`` this build does not understand.

    Deliberately a corruption subclass: from the reader's point of view a
    record it cannot interpret is unreadable, whatever the reason. It is
    separate so an operator gets "written by a newer events-cli" rather than
    "damaged", which are very different things to act on.
    """
