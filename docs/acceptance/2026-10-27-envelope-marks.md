# Acceptance record — envelope content marks, spark-f8a9, 2026-10-27

The evidence for [#15](https://github.com/agentculture/events-cli/issues/15)
(content marks on the envelope), built from the converged spec
[`docs/specs/2026-10-27-envelope-content-marks.md`](../specs/2026-10-27-envelope-content-marks.md)
and plan
[`docs/plans/2026-10-27-envelope-content-marks.md`](../plans/2026-10-27-envelope-content-marks.md).
Task t8 of that plan. Every row below names a test or a doc diff — nothing
rests on a judgment call (h1).

- **Box**: `spark-f8a9`.
- **Branch**: `spec/envelope-content-marks`, version `0.11.0`.
- **Broker under test**: throwaway `eclipse-mosquitto:2.1.2-alpine` containers
  from the `broker_factory` fixture, on ephemeral loopback ports. The live
  broker on `127.0.0.1:1883` was **not** touched.

## Gates

| Gate | Result |
|------|--------|
| `uv run pytest -n auto` (default selection) | 817 passed |
| `EVENTS_STACK_IT=1 uv run pytest -m stack` | 14 passed, 1 skipped (`EVENTS_TEST_BROKER` not set; pre-existing) |
| `black --check`, `isort --check-only`, `flake8`, `bandit` | clean |
| `markdownlint-cli2 "**/*.md" …` | 0 errors |
| `agentfront cli doctor . --strict` | exit 0, no FAIL |

One defect was caught by this gate rather than by the task agent: the t6 stack
test published before `EventClient`'s background connection was up
(`no_conn`). The agent had been told not to touch a broker; the merge-gate run
found it and `f7b7a38` waits for the connection.

A read-only `ask-colleague review` of the branch then found two more, both
fixed in `c6fb867`: the explain catalog said an `emit` flag overrides the same
key in `--marks` (it is refused), and a malformed timestamp mapping skipped its
value checks, under-reporting defects.

## #15's acceptance criteria

| Criterion | Test |
|-----------|------|
| 1. A `private` / `human` / `transcribed` envelope with two typed timestamps round-trips through the broker with marks intact | `tests/test_marks_stack.py::test_content_marks_survive_a_durable_broker_round_trip` (stack) |
| 2. An unknown `visibility` / `origin` / `fidelity` / timestamp type, or a non-RFC-3339 timestamp, is refused at creation | `test_new_refuses_a_mark_outside_the_vocabulary`, `test_new_reports_every_unknown_mark_in_one_pass`, `test_timestamp_at_reuses_the_utc_only_rfc3339_rule` |
| 3. An envelope with no marks is valid and reads back as unknown | `test_an_unmarked_envelope_is_valid_and_reads_back_as_unknown` |

## Requirements and honesty conditions

| Target | Evidence |
|--------|----------|
| c2 / h2 — fields; shape everywhere, membership in `new()` only | `test_from_dict_preserves_an_unknown_mark_verbatim`, `test_a_malformed_mark_shape_is_refused_on_read`, `test_a_malformed_mark_shape_is_refused_at_new_too`, `test_is_known_mark_reports_vocabulary_membership`, `test_a_tolerantly_read_envelope_validates_and_is_stored`, pinned map in `test_wire_field_names_are_pinned` |
| c3 / h3 — `Timestamp` tuple keeps envelopes hashable | `test_marked_envelopes_hash_and_compare_by_value`, `test_timestamp_is_frozen`, `test_marks_round_trip_the_wire_unchanged` |
| c4 / h4 — UTC-only `at` | `test_timestamp_at_reuses_the_utc_only_rfc3339_rule`; stated in `docs/contract.md` |
| c15 — `published` not stored; `by` is an identifier | `test_new_refuses_published_and_points_at_time`, `test_timestamp_by_follows_the_producer_identifier_rule` |
| c5 / h5 — `list` / `watch` show marks | `test_{list,watch}_text_line_shows_marks`, `…_flags_an_unrecognised_mark`, `…_for_an_unmarked_event_is_unchanged`, `…_json_carries_marks_exactly_as_the_envelope_emits_them` |
| c14 / h10 — `emit` flags and `--marks` | the 15 `test_emit_*` marks tests in `tests/test_cli_emit.py`, including `test_emit_mark_given_by_flag_and_marks_is_refused` and `test_emit_published_timestamp_is_refused_and_points_at_time` |
| c8 / h11 — client untouched | `test_publish_event_carries_content_marks_unchanged`; `git diff ab9ae04 HEAD -- events_cli/client.py` is empty |
| c6 — history needs no migration | `test_a_record_without_content_marks_reads_back_with_every_mark_none`, `test_a_marked_envelope_round_trips_its_content_marks_through_the_store` |
| c7 / h6 — contract and explain docs | `docs/contract.md` section "Envelope content marks"; `explain` entries for emit/list/watch; `test_every_catalog_path_resolves` passes |
| c11, c12 / h8, h9 — reader-first rollout | **not yet evidenced** — task t9, after 0.11.0 is on PyPI |

## The before-state and the audience

- **h14**: on `main` at `ab9ae04`, `events_cli/core/envelope.py` has no
  `visibility` / `origin` / `fidelity` / `timestamps` field, and
  `Envelope.from_dict` of a wire form carrying `"visibility": "private"`
  returns `[('visibility', 'unknown_field')]` — run from a detached worktree of
  that commit. That is also the drop-on-old-readers hazard the rollout order
  exists for.
- **h12**: the diff from `ab9ae04` touches nothing in `events_cli/client.py`
  and nothing in reachy-mini-cli. The named consumers are real: embodiment
  builds envelopes with `Envelope.new` (`embodiment/events.py`, `bus.py`), and
  culture-rules drains through `drain_subscription`
  (`culture_rules/events/events_cli_adapter.py`).

## Still open

- **t9** — after the 0.11.0 publish: the culture-rules floor-bump issue first
  (it must also replace its vendored `events_cli-0.10.0` wheel — plan risk r1),
  then embodiment's, then the #15 reply naming the two deviations. Until the
  culture-rules issue closes, **no producer may emit marks.**
- **r2** — CI never runs the stack marker; criterion 1's only durable evidence
  is this record.
