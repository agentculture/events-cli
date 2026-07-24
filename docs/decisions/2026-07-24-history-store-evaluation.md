# History store evaluation: `data_refinery.store` vs `eidetic-cli` vs bespoke-minimal

- **Date:** 2026-07-24
- **Task:** second-wave `t5` (research spike — no store code written)
- **Serves:** issue [#7](https://github.com/agentculture/events-cli/issues/7) (durable
  subscriptions + history), spec `docs/specs/2026-07-24-events-second-wave.md`
  claims **c5** (store-assigned cursor) and **c28** (per-host store + format marker)
- **Standing constraint being discharged:** issue #1's non-goal — *"no bespoke history
  store before the eidetic-cli / data-refinery-cli evaluation adjudicates."*

## Verdict

**Build the bespoke-minimal append-only JSONL log in `events_cli/history/`.**
Both siblings were evaluated against the criteria and **both fail**, for
different and independently disqualifying reasons recorded below.

This is the outcome the constraint reserves for a both-fail result, and it is
reached on measured evidence, not preference. The decisive fact is structural
and identical for both candidates: **neither sibling has a store-assigned
monotonic sequence, and neither exposes a bounded read-since-cursor.** Claim c5
says the cursor *must* be a store-assigned sequence, because `evt_` ULIDs sort
only to millisecond granularity with no intra-millisecond monotonicity. A store
with no sequence column cannot supply the one field the arc is built on.

Adopting either sibling would mean wrapping it in exactly the sequence-assignment
and cursor-index layer we would otherwise write — while inheriting a foreign
error taxonomy, a foreign version line, and (for the files backend) quadratic
ingest. The wrapper is larger than the thing it wraps.

## Scoring

Scale: **pass** / **partial** / **fail**.

| Criterion | `data_refinery.store` (files) | `eidetic-cli` | bespoke-minimal JSONL |
|---|---|---|---|
| Ordered append + store-assigned monotonic sequence | **fail** — no sequence field exists; order is an undocumented emergent property | **fail** — inherits the same envelope; recall is relevance-ranked, not temporal | **pass** — the seq is ours to assign |
| Read-since-cursor, bounded, no full scan | **fail** — `list()` takes no `since`/`offset`/`limit`; loads everything | **fail** — `recall` fetches all, ranks, then slices | **pass** — offset index + bounded read |
| `get`-by-id | **partial** — works, but full-load + linear scan | **partial** — semantic search, not keyed lookup | **pass** |
| `list`-by-type | **partial** — `type` filter applied in-process after full load | **partial** — facet filter after full ranking | **pass** |
| Restart survival | **pass** — JSONL on disk, atomic temp+`os.replace` | **pass** — same substrate underneath | **pass** — same technique |
| Dependency posture | **partial** — files backend is stdlib-only, but adds a base dep on a sibling distribution | **fail** — pulls `data-refinery-cli[store]` → `pymongo` + `neo4j` + an embed client | **pass** — stdlib only |
| Dedupe on event id | **fail as shipped** — dedupes on **content hash**, which destroys distinct events (avoidable, see below) | **fail** — same hash-dedupe path | **pass** — dedupe on `id`, per contract |
| Ingest cost | **fail** — O(N) per append, O(N²) total (measured) | **fail** — same backend | **pass** — O(1) append |

## Evidence

Everything in this section was **executed**, not inferred. Probes ran against
`/home/spark/git/data-refinery-cli` at version 0.11.0 with `PYTHONPATH` set to
the sibling checkout, writing only into scratch temp dirs. **No sibling repo was
mutated.** Probe scripts are in the session scratchpad, not committed.

### Observed: no sequence field, no bounded read

```text
[1] Envelope.to_dict() keys -> ['content', 'hash', 'id', 'metadata', 'scope', 'type']
[2] store.list signature -> (*, scope: Scope = Scope(name='default', visibility='public'),
                             type: str | None = None, backend: str = 'files', **kwargs) -> list[Envelope]
[2] store.get  signature -> (id: str, *, scope: Scope = ..., backend: str = 'files', **kwargs) -> Envelope | None
Backend.list(scope: Scope) -> list[Envelope]
Backend.all() -> list[Envelope]
```

There is no `seq`, no ordinal, and no timestamp on the envelope, and **no
`since` / `offset` / `limit` / `after` parameter anywhere in the API** — not on
`store.list`, not on the `Backend` protocol. `events watch --since <cursor>
--max N` would have to load the entire history into memory and slice it. That
is the whole feature, unimplementable on this surface without an index we build
ourselves.

### Observed: insertion order is real but uncontracted

```text
[3] insertion order ev0..ev4 -> list() returns: ['ev0','ev1','ev2','ev3','ev4']  (preserved: True)
```

The files backend does preserve insertion order. But this is an **accident of
implementation, not a guarantee**: a grep for `order` / `sort` across
`data_refinery/store/**` and its test suite returns only `sorted(glob(...))`
over *filenames*. Nothing documents or tests record order, and the sibling
`mongo` backend's `list()` is `find({})` with **no sort clause** — MongoDB
natural order is not insertion order once documents are updated or deleted.
Building a cursor on this would be building on an untested coincidence that the
owning repo has never promised and could optimise away.

### Observed: content-hash dedupe destroys distinct events

`FilesBackend.upsert` (`backends/files.py:97`) drops any existing record sharing
the new record's content hash:

```python
records = [r for r in records if r.hash != envelope.hash]
```

Measured consequence — two genuinely distinct events carrying the same payload:

```text
two distinct events, identical payload -> ['evt_02']
DATA LOSS: True
```

`evt_01` was silently destroyed. events-cli's contract is the opposite: events
are immutable facts, delivery is at-least-once (QoS 1), and consumers **must
dedupe on `id`**. Two `{"temp": 42}` readings a second apart are two facts, not
a duplicate.

**In fairness, this one is avoidable by construction** — storing the full
envelope JSON (which embeds the unique ULID) as `content` makes hashes unique:

```text
content=full envelope JSON -> ['evt_01', 'evt_02']
hash-dedup avoidable by construction: True
```

So this is a **sharp edge, not the disqualifier**. It is recorded because any
future adoption must know the mitigation is load-bearing — and because relying
on it means depending on a dedupe axis the upstream repo owns and could change.

### Measured: O(N) per append, O(N²) ingest

`upsert` is a full read-modify-write of the entire scope file — `_load()` parses
every record, linear-scans for the id, then `_save()` re-serialises the whole
file. Measured:

```text
  250 appends ->  0.140s total,  0.560 ms/append
  500 appends ->  0.523s total,  1.046 ms/append   x1.87 per-append vs previous
 1000 appends ->  2.092s total,  2.092 ms/append   x2.00 per-append vs previous
 2000 appends ->  8.397s total,  4.199 ms/append   x2.01 per-append vs previous
```

Per-append cost doubles exactly as N doubles — textbook O(N) per put. Extrapolated
from the fitted constant:

```text
   10,000 events -> ~     210 s to ingest
  100,000 events -> ~  20,993 s  (~5.8 hours)
1,000,000 events -> ~ 2.1e6 s    (~24 days)
```

The history store is fed by drains from a fabric whose stated producer is a
**50 Hz control loop** (`reachy-mini-cli`, issue #3). At 50 Hz the store crosses
its own ingest budget within minutes. This is not a tuning problem; it is the
data structure.

### Observed: import posture is clean, but the dependency is a distribution

```text
[10] non-stdlib top-level modules imported -> ['data_refinery', ...ambient site noise]
```

Importing `data_refinery.store` pulls no third-party package — the files backend
is genuinely stdlib-only, and `mongo`/`neo4j` lazy-import their drivers inside
function bodies. `data-refinery-cli` itself declares `dependencies = []`.

So adopting it would **not** break the dockerless default pytest selection, and
would not by itself break the no-install introspection lane (a history import
would live outside `events_cli/cli/`, like `client.py`). Credit where due: the
posture is careful and compatible with ours. It is simply not enough to
outweigh the missing sequence, the missing cursor, and the ingest curve.

### Observed: the foreign-error hazard is real but survivable

`data_refinery/store/backend.py:26` imports `data_refinery.cli._errors.CliError`
at module top level, so store failures raise a **foreign** `CliError` class.
events-cli's `_dispatch` catches it under the blanket `except Exception` and
wraps it, so **no traceback leaks** — but it degrades to a generic code-1
`unexpected: CliError: ...` with a "file a bug" remediation, losing
data-refinery's structured code-2 environment fault and its real hint.

eidetic hit exactly this and had to write a translation shim
(`eidetic/memory/backend.py:230`, `_translate_errors`) whose docstring says so
outright. Any adoption inherits that shim as mandatory boilerplate.

## Rejection reasons

### `data_refinery.store` — rejected

1. **No store-assigned sequence** (c5 unsatisfiable). The cursor *is* the
   sequence; the envelope has no field to hold one, and metadata-stuffing a
   counter would mean we assign, persist and index it ourselves — i.e. we write
   the store anyway, on top of a slower one.
2. **No bounded read-since.** `list()` is all-or-nothing. `events watch --since
   --max N` degrades to full-scan-and-slice, and the spec's non-infinite-bounds
   rule exists precisely to stop unbounded work in an agent turn.
3. **O(N²) ingest, measured.** Disqualifying for a fabric whose first producer
   is a 50 Hz loop.
4. **Order is uncontracted** — real in the files backend, absent in mongo,
   untested in either.
5. Secondary: content-hash dedupe on the wrong axis (mitigable), foreign
   `CliError` needing a translation shim, and a version-line coupling to a
   sibling distribution.

**What it is genuinely good at**, and why the constraint was worth discharging:
scope-isolated opaque document storage with atomic writes, a real migration
endpoint, and a disciplined zero-dependency posture. It is a good *document*
store. An event history is an ordered log, which is a different data structure.

### `eidetic-cli` — rejected

1. **Wrong lane, decisively.** It is a *memory/recall* surface: `recall` ranks by
   relevance score descending (`eidetic/memory/scoring.py:252`,
   `scored.sort(key=lambda t: t[0], reverse=True)`) and fetches **all**
   candidates before slicing to `top_k` (`_commands/recall.py:73` passes
   `top_k=2**31` explicitly "so `rank()` never truncates"). There is no temporal
   ordering, no cursor, no since. Semantic search over memories is not ordered
   event replay.
2. **Inherits every `data_refinery.store` defect** — it delegates storage
   straight through (`StoreBackend.upsert` → `drstore.put`), so the missing
   sequence, the missing bounded read, the hash dedupe and the O(N²) append all
   arrive unchanged, with an extra layer on top.
3. **Dependency posture fails outright.** `dependencies =
   ["data-refinery-cli[store]>=0.6,<0.7"]` pulls **`neo4j>=5` and `pymongo>=4`**
   transitively, plus an embedding client. That lands two database drivers in
   `pip install events-cli` for a store we would use in files mode, and puts
   heavyweight deps in the path of a test selection that must stay dockerless.
4. **Version skew.** eidetic is pinned to the `data-refinery-cli` 0.6.x line
   while data-refinery ships **0.11.0**. Depending on eidetic would pin
   events-cli behind a five-minor-version-old storage substrate.

### Bespoke-minimal JSONL — chosen

Chosen only because both siblings failed above, per the constraint. Scope must
stay *minimal* — an append-only log with a sequence and an offset index, not a
database:

- append-only writes (`open(..., "a")`, one JSON object per line, `fsync` policy
  stated), so appends are **O(1)** and ordering is the file itself;
- a store-assigned monotonic `seq` per subscription, assigned at append — this
  is the cursor (c5), and it is never derived from comparing ULIDs;
- an offset index for `--since <seq>` so a bounded drain reads a bounded amount;
- dedupe keyed on **event `id`**, never on content hash.

## Constraints t6 must honour

1. **The cursor is a store-assigned monotonic sequence** — never a ULID
   comparison, never a list index. Spec c5, and its honesty condition demands a
   test that mints two envelope ids **in the same millisecond** and proves drain
   order equals store append order with exact cursor resume.
2. **Per-host location, not CWD-relative** (c28). Follow the existing stack
   convention in `events_cli/stack/__init__.py:92` (`default_stack_dir`):
   `$XDG_CONFIG_HOME` (or `~/.config`) `/events-cli/`, with an env override
   mirroring `EVENTS_STACK_DIR`. `events list` must answer identically from two
   different CWDs.
3. **Every record carries a store-format version marker** from the *first* write
   (c28), so a future migration has something to key on.
4. **Dedupe on event `id`.** QoS 1 is at-least-once; the spec's takeover scenario
   requires persist-then-ack plus id-dedupe to lose nothing under concurrent
   drains.
5. **Bounded reads only.** `--max` and `--timeout` with non-infinite defaults
   (`--max 100`, `--timeout 30`); no unbounded `--follow` in this arc.
6. **Stdlib only, and outside `events_cli/cli/`.** The history module must not
   break the no-install introspection lane, and store logic must keep its
   coverage in the **default dockerless selection** — pure append/read/cursor
   logic tested with no broker and no docker, broker paths behind the `stack`
   marker.
7. **Atomic and crash-safe.** Borrow data-refinery's proven technique — temp
   sibling + `os.replace` for index rewrites — rather than inventing one. An
   interrupted write must leave the log readable.
8. **Errors are `events_cli` `CliError`s** with real remediation; a corrupt line
   is a code-2 environment fault, matching the existing taxonomy.

## Revisit triggers

This verdict is about capability, not politics — reopen it if any of these change:

- `data_refinery.store` grows a store-assigned sequence **and** a bounded
  `list(since=..., limit=...)`; that removes reasons 1 and 2 at once, and the
  append cost becomes the only remaining blocker.
- The files backend moves to true append-only writes, removing the O(N²) curve.
- events-cli's history needs semantic search over event payloads — that is
  eidetic's actual strength, and it would be the right tool for that job even
  though it is the wrong one for replay.

Until then the bespoke log stays deliberately small, so that adopting a sibling
later is a backend swap behind the seam rather than a rewrite.
