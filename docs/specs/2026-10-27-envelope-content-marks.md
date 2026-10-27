# envelope content marks

> events-cli envelopes carry content marks — visibility, origin, fidelity and typed timestamps — validated against closed vocabularies, carried on the wire unchanged, shown by events list/watch, and documented with absent-means-unknown, so culture-rules can act on them (#15)
> instruction: Build per the confirmed requirements c2-c5, c7, c14; verify with #15's three acceptance criteria (success signal c20) plus the reader-first rollout (c11, c12, c16)

## Audience

- Producers that mark their content — embodiment (Gwen) first, via Envelope.new + EventClient — and readers that act on marks — culture-rules first, draining via `events_cli`.subs — plus humans and agents emitting or inspecting events through events emit/list/watch. reachy-mini-cli's raw-MQTT lane is unaffected

## Before → After

- After: An event can say who may see it (visibility), who produced it (origin), how it became what it is (fidelity), and when each thing happened to it (typed timestamps); a producer building a mark outside the closed vocabularies is refused at Envelope.new(); the marks round-trip through the broker and history unchanged; events list/watch show them; and an unmarked event stays valid and reads as unknown

## Why it matters

- Today nothing that crosses the mesh says whether it is private, whether a human or a model produced it, or whether it is verbatim or transcribed — and 'time' is one untyped instant. culture-rules cannot act on, and later quality/coherence scoring cannot weigh, a distinction the envelope never carries; the envelope is the one contract every producer shares, so the marks belong there (#15)

## Requirements

- Envelope (`events_cli`/core/envelope.py) gains four optional fields — visibility, origin, fidelity, timestamps — each added to `WIRE_FIELD_NAMES`, `_CHECKS` and Envelope.new() kwargs; `_collect_errors` checks each mark's SHAPE everywhere (a string; timestamps a list of {type, at, by?} objects; at a UTC RFC 3339 instant), while the closed-vocabulary MEMBERSHIP check runs in new() only (q1/c13); `test_envelope.py`:286 pins the exact `WIRE_FIELD_NAMES` dict and must be updated deliberately
  - instruction: Add the four fields to Envelope, `WIRE_FIELD_NAMES` (wire names visibility/origin/fidelity/timestamps), `_CHECKS` (shape) and new() (shape + membership); update the pinned dict at tests/`test_envelope.py`:286 in the same commit
  - honesty: `test_envelope.py` proves both halves: Envelope.new() with an unknown visibility/origin/fidelity/timestamp type raises EnvelopeValidationError naming the field, AND Envelope.`from_dict` of the same wire form succeeds with the raw value preserved and is-known false; a shape error (non-string mark, timestamp missing 'at') is refused on both paths
- timestamps is stored as an immutable tuple of a frozen Timestamp value type (type, at, by), not a list of dicts: Envelope's dataclass hash covers every field except data (`test_envelope.py`:730 asserts hash equality), so a list field would make hash(envelope) raise TypeError
  - instruction: Add a frozen Timestamp dataclass (type, at, by=None) in core/envelope.py, export it from `events_cli`.core; Envelope.timestamps is tuple\[Timestamp, ...\] | None; new() accepts Timestamp instances or mappings
  - honesty: hash(envelope) succeeds for an envelope carrying timestamps, two equal marked envelopes hash equal, and Timestamp is frozen (assignment raises FrozenInstanceError); `to_dict` renders timestamps as a JSON list of objects and `from_dict` converts back to the tuple
- each timestamp's 'at' is validated with the existing `parse_rfc3339`, so it inherits the UTC-only rule (Z or +00:00) that 'time' already enforces; a non-UTC offset is refused with the same `not_utc` code
  - instruction: Validate each 'at' via `parse_rfc3339` and report `_TimestampError` codes as FieldError('timestamps\[i\].at', ...)
  - honesty: a timestamp 'at' with a +02:00 offset is refused with code `not_utc` and a date-only string with code malformed, both under the field path timestamps\[i\].at; docs/contract.md states the UTC-only narrowing
- events list and events watch human output (list.py `cmd_list` line render; watch.py `_render` per-record line) show the marks when present and nothing when absent; --json already carries them for free because both serialise record.envelope.`to_dict`()
  - instruction: Extend the per-record line in list.py `cmd_list` and watch.py `_render`; present 'time' as the published timestamp (c15); no change to the JSON payload builders
  - honesty: `test_cli_list.py` and `test_cli_watch.py` assert a marked event's text line shows its visibility/origin/fidelity and timestamp types, an unmarked event's line is unchanged from today, an unknown preserved value renders flagged unrecognised, and --json output includes the marks verbatim
- docs/contract.md gains an envelope-marks section: the four attributes, both closed vocabularies, absent-means-unknown (never public, never human, no time beyond 'time'), 'by' as a claim not a measurement, and 'mixed' / per-part marks inside data; the explain catalog entries for emit/list/watch mention the marks
  - instruction: Write the contract section; touch the explain catalog entries ('emit',), ('list',), ('watch',); keep CLAUDE.md's description of the envelope in step
  - honesty: docs/contract.md has a marks section naming all four attributes, both vocabularies, absent-means-unknown, by-is-a-claim, published-is-time, write-strict/read-tolerant and the reader-first rollout; `test_every_catalog_path_resolves` still passes and markdownlint is clean
- rollout order is part of the deliverable: every reader (culture-rules, any events watch host) upgrades to the marks-aware events-cli BEFORE any producer (embodiment) emits marks — an older `from_dict` rejects the new keys as `unknown_field`, and the drain acks-and-drops a malformed payload permanently (contract.md 'A payload that is not an envelope is dropped, permanently')
  - instruction: File the issues after 0.11.0 publishes to PyPI, via the communicate skill; link them from the PR and the #15 reply
  - honesty: the sibling issues on culture-rules and embodiment state the reader-first order explicitly, and embodiment's issue says not to emit marks until culture-rules is on 0.11.0
- consumers bump their floor to the marks release (culture-rules and embodiment pyproject 'events-cli>=0.10' -> >= the new minor) — filed as cross-repo issues via the communicate skill, not edited from here
  - instruction: File via communicate (auto-signed); do not edit sibling repos from here
  - honesty: both sibling issues name the exact new floor 'events-cli>=0.11'
- events emit gains --visibility, --origin, --fidelity and `--marks <file>` (JSON object of the four marks; the only CLI path for timestamps); a flag and the file naming the same mark is a user error
  - instruction: Add the flags and --marks to emit.py register(); merge into the wire dict before Envelope.`from_dict`, then run the new()-side membership check so emit is write-strict
  - honesty: `test_cli_emit.py` covers: each scalar flag lands on the emitted envelope; `--marks <file>` carries timestamps; a flag and the file naming the same mark exits 1 with a hint; an unknown value via either path exits 1 through EnvelopeValidationError; 'published' in --marks is refused with the time hint

## Honesty conditions

- every element of the announcement is mechanically checked: vocabulary refusal, wire carriage, list/watch display and the contract section each map to a test or a doc diff — nothing rests on a judgment call
- the PR diff does not touch `events_cli`/client.py, and a NEW test in `test_client.py` publishes a marked Envelope via `publish_event` and asserts the wire JSON carries all four marks
- the named audience is real: embodiment builds envelopes with Envelope.new (embodiment/events.py, bus.py) and culture-rules drains via `drain_subscription` (`events_cli_adapter.py`), and the PR diff touches nothing on reachy-mini-cli's raw lane
- every element of the after-state is checked by a named test: refusal at new() (h2), round-trip through broker and history (c20's stack-marked test), list/watch display (h5), and an unmarked envelope reading as unknown
- the before-state is true on main today: Envelope has no visibility/origin/fidelity/timestamps field, and `from_dict` rejects those keys as `unknown_field`
- \#15's three acceptance criteria each map to one named test that passes — the round-trip under the stack marker (pytest -m stack), the refusals and the unmarked-reads-unknown case in the default selection — and the culture-rules floor-bump issue is closed before embodiment's emit issue is acted on

## Success signals

- \#15's three acceptance criteria pass in the test suite: a visibility=private/origin=human/fidelity=transcribed envelope with two typed timestamps round-trips through the broker (stack marker) and history with marks intact; an unknown visibility/origin/fidelity/timestamp type or a non-RFC-3339 'at' is refused at new(); an unmarked envelope is valid and reads back as unknown — and culture-rules is on 0.11.0 before embodiment emits marks

## Scope / boundaries

- the client lane (`events_cli`/client.py) is untouched: `publish_event` already sends Envelope.`to_json`, so producers like embodiment get the marks by building the Envelope with them — no client API change, and reachy-mini-cli's 50 Hz raw lane is unaffected
  - instruction: Add one test that publishes a marked envelope through EventClient.`publish_event` and asserts the wire JSON carries the marks; no client code change

## Non-goals

- events-cli does not enforce visibility: it carries and validates the marks but filters nothing on them — honouring 'private' is culture-rules' job, asked separately in #15
- no validation of mark coherence across parts inside data and no cross-field rules (e.g. fidelity=transcribed requires a transcribed timestamp) — vocabularies are closed, combinations are free; the operator expects to grow the vocabularies

## Assumptions

- the history store needs no schema change: HistoryRecord.`to_dict` nests envelope.`to_dict`() and `from_dict` re-validates through Envelope.`from_dict`, so marked events persist and read back intact, and pre-existing unmarked records keep parsing as unknown

## Scope exploration

- `s1` — `events_cli/core/envelope.py`: Envelope is a frozen dataclass whose wire bridge is one `WIRE_FIELD_NAMES` map + a `_CHECKS` table; optional fields are omitted when None, and new() validates before returning — so the four marks slot in as optional fields with one check each, refused at new() exactly as a bad type is today
  - seeds: `c2`
- `s2` — `Envelope hashing (envelope.py field(hash=False) on data only; tests/test_envelope.py:730)`: hash excludes only data; any new mutable field (a list of timestamp dicts) breaks hashing and the 'envelopes live in a set' promise
  - seeds: `c3`
- `s3` — `parse_rfc3339 (envelope.py)`: the existing parser is deliberately UTC-only and stricter than fromisoformat; issue #15 says only 'RFC 3339', so reusing it narrows the ask to UTC — the reuse is right but the narrowing must be stated to the producer
  - seeds: `c4`
- `s4` — `events_cli/cli/_commands/list.py + watch.py`: both --json paths emit envelope.`to_dict`() unchanged, so wire carriage is automatic; only the one-line text renders (type id seq sub) need the marks added
  - seeds: `c5`
- `s5` — `events_cli/history/backend.py + jsonl.py`: records store the envelope's wire dict and re-parse via Envelope.`from_dict` on read — additive optional fields need no store migration
  - seeds: `c6`
- `s6` — `docs/contract.md + events_cli/explain/catalog.py`: contract.md has no envelope-field section yet (sections run Surfaces..Non-goals); catalog keys ('emit',), ('list',), ('watch',) are the per-verb docs to touch
  - seeds: `c7`
- `s7` — `events_cli/client.py`: `_encode_payload` uses Envelope.`to_json`; nothing in the client inspects envelope fields
  - seeds: `c8`
- `s8` — `issue #15 closing line`: 'culture-rules, which stores and acts on these, is asked separately to honour them' — enforcement is out of this repo's lane
  - seeds: `c9`
- `s9` — `issue #15 vocabulary note`: 'The vocabularies can grow later; the operator expects to improve them' — keep rules to per-value membership
  - seeds: `c10`
- `s10` — `consumers: culture-rules (events-cli>=0.10, drain_subscription), embodiment (events-cli>=0.10, Envelope.new), reachy-mini-cli (events-cli>=0.9)`: culture-rules drains via `events_cli`.subs.`drain_subscription` -> Envelope.`from_json`; on 0.10.0 the strict unknown-field rule turns a marked envelope into a skipped malformed payload that is acked, so a producer-first rollout silently loses events. culture-rules also ships a vendored `events_cli`-0.10.0 wheel
  - seeds: `c11`
- `s11` — `sibling pyprojects`: embodiment/pyproject.toml:86 and culture-rules/pyproject.toml:24 both float at >=0.10 — the floor is what enforces the reader-first order
  - seeds: `c12`

## Decisions

- q1: write-strict, read-tolerant — new() refuses unknown mark values; `from_dict` preserves them verbatim with an is-known helper; list/watch flag them unrecognised; the vocabulary check is in new() only, not validate()
- q3: 'published' is not stored in timestamps (new() refuses it, hint: 'time' is the published instant) and list/watch present time as published; timestamp 'by' is a short identifier under the producer rule
- q4: release 0.11.0 under schemaVersion '1'; reader-first rollout — culture-rules raises its floor before embodiment emits marks; sibling floor-bump issues filed via communicate; reply on #15 records the two deviations from its text

## Open parks

- [unknown_nonblocking] Per-part marks inside data for 'mixed' content: #15 suggests 'a list of parts inside data, each with its own marks' — no shape is defined, and events-cli does not validate data's interior; leave it a producer convention documented in contract.md until a consumer needs it
