"""Domain errors for the event core (no CLI, no transport).

The core raises **its own** error type — never
:class:`events_cli.cli._errors.CliError`. Four surfaces consume this module
(the CLI, MCP, HTTP and ``import events_cli``) and only one of them has exit
codes, so the exit-code policy stays at the CLI boundary and the core stays
importable by callers that have no CLI at all. The CLI translates an
:class:`EventsError` into a ``CliError`` at its edge.

Everything here is stdlib-only and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Every field-level reason the validator can emit. Declared once so callers can
# switch on a stable slug instead of matching prose, and so a test can prove no
# undeclared code escapes.
ERROR_CODES: tuple[str, ...] = (
    "missing",  # required field absent from the payload
    "unknown_field",  # key that is not part of the envelope contract
    "not_an_object",  # payload (or `data`) is not a JSON object
    "not_json",  # the bytes/text were not valid JSON at all
    "not_a_string",
    "not_an_integer",
    "empty",  # present but the empty string
    "too_long",
    "too_deep",  # payload nesting beyond the supported depth
    "malformed",  # present, right type, wrong shape
    "not_utc",  # a timestamp with a non-zero UTC offset
    "out_of_range",
    "unsupported_type",  # a value JSON cannot represent
)

# Field name used for problems with the document as a whole rather than one
# field ("this is not an object", "this is not JSON").
ENVELOPE_FIELD = "envelope"


class EventsError(Exception):
    """Base class for every events-cli domain error."""


@dataclass(frozen=True)
class FieldError:
    """One named reason a field was rejected.

    ``field`` is the **wire** name (``schemaVersion``, ``data.commit``), because
    that is the name the caller has to fix in the payload they sent.
    """

    field: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


class EnvelopeValidationError(EventsError):
    """Raised when an envelope is invalid, carrying every field-level reason.

    All problems found in one pass are reported together: an agent fixing a
    payload should not have to retry once per broken field.
    """

    def __init__(
        self,
        errors: Iterable[FieldError],
        *,
        summary: str = "invalid event envelope",
    ) -> None:
        self.errors: tuple[FieldError, ...] = tuple(errors)
        self.summary = summary
        detail = "; ".join(str(err) for err in self.errors)
        super().__init__(f"{summary}: {detail}" if detail else summary)

    @property
    def fields(self) -> tuple[str, ...]:
        """The wire names of the rejected fields, in report order."""
        return tuple(err.field for err in self.errors)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready shape for the ``--json`` surfaces."""
        return {
            "error": "envelope_validation",
            "message": self.summary,
            "errors": [err.to_dict() for err in self.errors],
        }
