"""The events-cli core: what an event *means*, independent of how it travels.

This package is the bottom layer of the fabric and imports nothing but the
standard library — no transport client, no container runtime, no I/O. Every
other surface is built on it:

* ``import events_cli`` — applications construct and validate envelopes here;
* the CLI verbs — which translate :class:`EventsError` into their own
  ``CliError`` exit codes at the boundary;
* the derived MCP and HTTP tools.

Because it is pure, its tests run on a machine with no broker and no docker,
which is what keeps the code-quality gate independent of a live stack.
"""

from __future__ import annotations

from events_cli.core.envelope import (
    EVENT_ID_PREFIX,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    WIRE_FIELD_NAMES,
    Envelope,
    new_event_id,
    new_id,
    now_rfc3339,
    parse_rfc3339,
)
from events_cli.core.errors import (
    ENVELOPE_FIELD,
    ERROR_CODES,
    EnvelopeValidationError,
    EventsError,
    FieldError,
)

__all__ = [
    "ENVELOPE_FIELD",
    "ERROR_CODES",
    "EVENT_ID_PREFIX",
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "WIRE_FIELD_NAMES",
    "Envelope",
    "EnvelopeValidationError",
    "EventsError",
    "FieldError",
    "new_event_id",
    "new_id",
    "now_rfc3339",
    "parse_rfc3339",
]
