# Build Plan — envelope content marks

slug: `envelope-content-marks` · status: `exported` · from frame: `envelope-content-marks`

> events-cli envelopes carry content marks — visibility, origin, fidelity and typed timestamps — validated against closed vocabularies, carried on the wire unchanged, shown by events list/watch, and documented with absent-means-unknown, so culture-rules can act on them (#15)

## Tasks

### t1 — Core marks on Envelope (`events_cli`/core/envelope.py, `events_cli`/core/`__init__.py`, tests/`test_envelope.py`): add the four optional fields, the closed vocabularies, a frozen Timestamp(type, at, by=None), shape checks in `_CHECKS`, vocabulary membership in new() only, and a public is-known helper

- covers: c2, h2, c3, h3, c4, h4
- acceptance:
  - o1: Envelope.new() with any unknown visibility/origin/fidelity value or timestamp type raises EnvelopeValidationError naming every bad field in one pass (timestamps\[i\].type for timestamps)
  - o2: Envelope.`from_dict` of the same wire form succeeds and preserves the raw string; a malformed shape (non-string mark, timestamps not a list of objects, missing at/type, unknown key in a timestamp object) is refused on both paths
  - o3: an envelope read tolerantly validates (validate() applies no membership check) and appends to a HistoryStore without error
  - o4: the is-known helper reports per-mark vocabulary membership and is the only vocabulary declaration outside new(); exported from `events_cli`.core
  - o5: `to_dict` omits absent marks (never null) and `to_dict`(`from_dict`(x)) == x for marked wire forms, including preserved unknown values
  - o6: hash() works on marked envelopes, equal marked envelopes hash equal, Timestamp is frozen, and `from_dict` and new() yield the same tuple\[Timestamp, ...\]
  - o7: 'at' reuses `parse_rfc3339` unchanged; +02:00 is refused as `not_utc` and a date-only string as malformed under timestamps\[i\].at
  - o8/o9: new() refuses a 'published' timestamp with a message naming 'time' as the published instant (`from_dict` preserves it as unknown); 'by' uses the producer identifier rule so 'client' is valid and whitespace/empty is refused
  - the pinned `WIRE_FIELD_NAMES` dict in tests/`test_envelope.py` is updated in the same commit; the full default suite, black, isort, flake8 and bandit pass

### t2 — events list / events watch show marks (`events_cli`/cli/`_commands`/list.py, watch.py, tests/`test_cli_list.py`, tests/`test_cli_watch.py`)

- depends on: t1
- covers: c5, h5
- acceptance:
  - o10: a marked event's text line shows visibility, origin, fidelity and timestamp types; a preserved unknown value is flagged unrecognised (via the is-known helper, no re-declared word list); 'time' is shown as published; an unmarked event's line is byte-identical to today's
  - o11: --json records carry marks exactly as envelope.`to_dict`() emits them, with no CLI-side reshaping

### t3 — events emit marks flags (`events_cli`/cli/`_commands`/emit.py, tests/`test_cli_emit.py`)

- depends on: t1
- covers: c14, h10
- acceptance:
  - o12: --visibility/--origin/--fidelity each land on the emitted envelope and `--marks <file>` carries timestamps; both feed the wire dict before Envelope.`from_dict` and then the new()-side membership check runs, so emit is write-strict
  - o12: a mark given by both a flag and --marks, an unreadable or non-object --marks file, an unknown value, or a 'published' timestamp each exit 1 with a hint, no traceback, and nothing published

### t4 — Contract and explain docs for marks (docs/contract.md, `events_cli`/explain/catalog.py, CLAUDE.md)

- depends on: t1
- covers: c7, h6
- acceptance:
  - o15: docs/contract.md gains a marks section naming both vocabularies, absent-means-unknown, write-strict/read-tolerant, by-is-a-claim, published-is-time, UTC-only 'at' and the reader-first rollout
  - o15: the ('emit',), ('list',), ('watch',) catalog entries mention the marks; `test_every_catalog_path_resolves` passes; markdownlint-cli2 is clean; CLAUDE.md's envelope description lists the four new fields

### t5 — Client and history carry marks unchanged (tests/`test_client.py`, tests/`test_history.py` — tests only)

- depends on: t1
- covers: c8, h11
- acceptance:
  - o13: a new `test_client.py` test publishes a marked Envelope via `publish_event` and asserts the wire JSON carries all four marks; `events_cli`/client.py has no diff
  - o14: a new `test_history.py` test proves a record written without marks still reads back, and a marked record re-read via HistoryRecord.`from_dict` keeps its marks

### t6 — Broker round-trip acceptance test for #15 (new tests/`test_marks_stack.py`, stack marker)

- depends on: t1
- covers: c20, h15, c18, h13
- acceptance:
  - \#15 criterion 1: an envelope with visibility=private, origin=human, fidelity=transcribed and two typed timestamps, published at QoS 1 through a registered subscription, drains back via `drain_subscription` with marks equal to what was sent (pytest -m stack)
  - \#15 criteria 2 and 3 are named in the file's docstring with pointers to the t1 tests that prove them (refusal at new(); unmarked envelope valid and reads as unknown), so all three criteria map to named tests

### t7 — Version bump to 0.11.0 + CHANGELOG (pyproject.toml, CHANGELOG.md, uv.lock via version-bump skill)

- depends on: t2, t3, t4, t5, t6
- acceptance:
  - the version-bump skill sets 0.11.0 (minor) under schemaVersion '1' with a Keep-a-Changelog entry naming the marks, the write-strict/read-tolerant rule and the reader-first rollout requirement; the version-check CI job passes

### t8 — Acceptance audit before PR: run every suite and record the evidence (docs/acceptance/2026-10-27-envelope-marks.md)

- depends on: t7
- covers: c1, h1, c17, h12, c19, h14
- acceptance:
  - the default suite, pytest -m stack and the six lint gates incl. agentfront cli doctor --strict all pass; results recorded in the acceptance doc
  - the doc maps every confirmed requirement and honesty condition to a named test or doc diff (h1), records that main@ab9ae04 Envelope had none of the four fields and rejected them as `unknown_field` (h14), and that the diff touches nothing in `events_cli`/client.py or reachy-mini-cli (h12)

### t9 — Post-publish rollout (after 0.11.0 is on PyPI): sibling floor-bump issues and the #15 reply, via the communicate skill

- depends on: t8
- covers: c11, h8, c12, h9
- acceptance:
  - o16: a culture-rules issue is filed first asking for events-cli>=0.11 and naming the drop-on-old-reader hazard; an embodiment issue asks for events-cli>=0.11 and says not to emit marks until the culture-rules issue is closed
  - a reply on events-cli#15 links both issues and names the two deviations from its text: 'published' is not stored (time is the published instant) and unknown values are preserved on read

## Risks

- [unknown_nonblocking] culture-rules vendors `events_cli`-0.10.0-py3-none-any.whl at its repo root; if its deploy installs that wheel rather than resolving from PyPI, raising the floor alone will not upgrade the reader — the culture-rules issue must ask for the vendored wheel to be replaced too (task t9)
- [follow_up] CI never runs the stack marker, so #15's round-trip criterion is proven only by a local pytest -m stack on spark-f8a9; the acceptance doc (t8) is its only durable evidence (task t6)
