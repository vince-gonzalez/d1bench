# Writing rows to Cloudflare D1: which strategy wins, and which limit stops you first

Measured 2026-10-10 against a live D1 database (`f-keys-d1bench`, region
ENAM) from inside a deployed Worker. Harness `f-keys/d1bench`, Worker version
`d5bb2ca7`. Every number below is the median of repeated passes against the
live service; nothing here is computed from the documentation.

---

## The question

Cloudflare documents its D1 limits individually. It does not document how they
interact, and they interact in a way that changes which strategy is correct.
Three ways to write N rows, each stopped by a different limit:

| strategy | one statement holds | capped by |
|---|---|---|
| `params` | many rows, values **bound** | 100 bound parameters per query |
| `literal` | many rows, values **inlined** | 100,000-byte statement length |
| `onebyone` | one row | queries per invocation, 30 s per call |

The limits are published. Which one binds first, for a given shape of data,
is not — and that is the only part a developer actually has to decide.

---

## Finding 1 — the documented 1,000 is exact, and a `batch()` counts as one

### The documented limit is correct, and enforced to the query

The row on D1's limits page reads:

> **Queries per Worker invocation (read [subrequest limits](...))**: 1000
> (Workers Paid) / 50 (Free)

Tested directly, with **one fresh Worker invocation per rung** and the
cheapest possible statement (`SELECT 1`) so that a count limit would bite
before any duration limit:

| queries requested | completed | result |
|---|---|---|
| 900 | 900 | ok |
| 999 | 999 | ok |
| 1,000 | 1,000 | ok |
| 1,001 | **1,000** | refused |
| 1,100 | **1,000** | refused |
| 2,000 | **1,000** | refused |

The refusal names itself:

```
Too many API requests by single Worker invocation.
```

So the documented figure is not approximate and not stale. It is exactly
1,000, it is enforced on the 1,001st query, and the first 1,000 complete. The
unit the error names is an **API request**.

### What the documentation does not say

Nothing on the [limits page](https://developers.cloudflare.com/d1/platform/limits/)
or the [`batch()` API page](https://developers.cloudflare.com/d1/worker-api/d1-database/)
states how a `db.batch()` is counted against that budget: one API request, or
one per statement contained in it. The limits page does say that per-query
limits apply per statement inside a batch, giving statement length as its
example, which invites the stricter reading for the count as well.

Measured, in the same shape of invocation, both just above the documented
budget:

| form | result |
|---|---|
| 2,000 queries, one at a time | **refused** after exactly 1,000 |
| 2,000 statements in one `batch()` | **ok**, 421 ms |

A single `batch()` therefore costs **one** API request, not one per statement.
Pushed much further, a single call carried **150,000 statements** — 150 times
the per-invocation budget — and completed.

That is the whole finding, and it is the practical one: the common advice to
chunk a bulk insert so the statement count stays under 1,000 is solving a
problem that does not exist. The budget constrains how many **times you call
D1**, not how much SQL you hand it per call.

### Where a large batch does stop, stated without a mechanism

Pushed deliberately past the point of success:

| statements in one `batch()` | result |
|---|---|
| 128,000 | 30,208 ms, succeeded |
| 150,000 | 25,802 ms, succeeded |
| 175,000 | refused |
| 200,000 | refused |

So refusal begins between 150,000 and 175,000 statements at this row shape.
**Which limit produces it is deliberately not claimed here.** At least three
plausible ceilings sit within range of each other — the documented 30-second
maximum SQL query duration, which footnote 4 extends to the entire batch call;
the Workers Paid CPU default of 30 seconds; and D1's own internal CPU limit —
and the two refusals came back with different text:

```
D1_ERROR: D1 DB storage operation exceeded timeout which caused object to be reset.
D1_ERROR: D1 DB exceeded its CPU time limit and was reset.
```

Naming one of those limits as the cause would require isolating it, and that
experiment was not run. What is safe to take away is the shape rather than the
number: the ceiling on a single batch is a **budget of time, not of
statements**, so it moves with row width and value size. A statement count
copied out of this table into code handling wider rows will be wrong.

### Atomicity at the boundary

The `batch()` documentation states that batched statements are SQL
transactions and that a failure "aborts or rolls back the entire sequence."
One refusal was executed with a verified before-count of zero rows: it
returned the CPU-time error and left **zero rows**, matching the documented
guarantee.

A separate earlier refusal returned the storage-timeout error, and the table
was afterwards found holding exactly 200,000 rows. **No before-count was taken
on that run**, so it is recorded as unexplained and no claim is made from it.

### Corrections, and what caused them

This finding was stated wrongly three times before it was stated correctly,
and every error came from quoting the documentation instead of reading it.

1. First draft: *"the 30-second cap is not strictly enforced"*, from a
   128,000-statement call that ran 30,208 ms. That read 0.7% past a boundary
   as evidence, which is thinner than the uncertainty in where two different
   clocks start and stop.
2. Second: *"the cap applies only per statement, so nothing was violated."*
   That missed footnote 4, which says plainly that the duration limit applies
   to the entire batch call.
3. Third: the claim that the parenthetical *"(read subrequest limits)"* is a
   dropped hint about reads versus writes. It is not. It is an imperative verb
   attached to a hyperlink — *read* the subrequest limits page — and reading it
   as a kind of limit was an error. That page also shows the general Workers
   subrequest budget on Paid defaulting to 10,000 rather than 1,000, which
   looked like an inconsistency until the direct test showed D1 enforcing
   exactly 1,000. The D1 page is right.

The data never changed. Only the reading did, and the version that survives
confirms Cloudflare's own number exactly while isolating the single fact their
pages do not state.

## Finding 2 — the parameter ceiling is exactly `floor(100 / C)`, and it reaches 1

Climbed until D1 refused, at each column count:

| columns bound (C) | max rows per statement | parameters used | `floor(100/C)` | error at failure |
|---|---|---|---|---|
| 2 | 50 | 100 | 50 | `too many SQL variables` |
| 4 | 25 | 100 | 25 | `too many SQL variables` |
| 8 | 12 | 96 | 12 | `too many SQL variables` |
| 16 | 6 | 96 | 6 | `too many SQL variables` |
| 32 | 3 | 96 | 3 | `too many SQL variables` |
| 64 | **1** | 64 | 1 | `too many SQL variables` |

The arithmetic is exact at every point. The consequence is the part worth
saying out loud: **at C >= 51, `floor(100/C)` is 1**, so "chunked bound
parameters" has silently become one-statement-per-row. It is no longer a
batching strategy at all, while still being described as one in the code that
uses it.

---

## Finding 3 — literal SQL pulls away as columns rise

1,000 rows, 16-byte values, median of 5 passes, tables truncated before every
measured run.

| C | `params` | `literal` | `onebyone` | params rows/stmt | literal rows/stmt | verdict |
|---|---|---|---|---|---|---|
| 2 | 51 ms | 44 ms | 281 ms | 50 | 1000 | literal by 7 ms |
| 4 | 54 ms | 49 ms | 253 ms | 25 | 1000 | within noise (sd 20) |
| 8 | 88 ms | 59 ms | 284 ms | 12 | 500 | literal by 29 ms (33%) |
| 16 | 132 ms | 93 ms | 302 ms | 6 | 250 | literal by 39 ms (30%) |
| 32 | 195 ms | 123 ms | 309 ms | 3 | 143 | literal by 72 ms (37%) |
| 64 | 503 ms | 187 ms | 388 ms | 1 | 77 | **literal by 316 ms (63%)** |

At C=64 literal SQL is **2.7x faster**, and the mechanism is in the two
rows/stmt columns rather than in anything subtle: the parameter ceiling has
driven `params` to 1 row per statement while `literal` still carries 77.

**`params` at C=64 and `onebyone` are the same thing** — 1,000 single-row
statements — and their medians (503 ms and 388 ms) differ by less than the
spread of `params` (sd 93, min 327). No claim is made that one beats the
other; they converge, which is the point.

**The bias this had to survive.** In Workers, `Date.now()` advances only on
I/O, so it cannot see CPU — and `literal` is the strategy doing extra CPU,
building and escaping SQL strings. Timed only from inside the Worker, literal
SQL would look better than it is. So every run was also timed from outside, as
whole-request wall clock:

| C | params d1 / total | literal d1 / total | outside overhead |
|---|---|---|---|
| 2 | 51 / 135 ms | 44 / 129 ms | 84 vs 85 ms |
| 8 | 88 / 181 ms | 59 / 151 ms | 93 vs 92 ms |
| 64 | 503 / 599 ms | 187 / 285 ms | 96 vs 98 ms |

The outside overhead is 84–98 ms and **essentially identical between
strategies at every column count**, so the CPU that the inner clock cannot see
is not what produced the gap. The finding holds in both clocks. This was the
measurement most likely to overturn the result, which is why it was run.

---

## Finding 4 — on the row-size axis it runs the other way, and ends in a wall

C=8, median of 3 passes. N is scaled down as rows get fatter to hold each run
near 4 MB, so times compare **between strategies within a row size**, never
across row sizes.

| bytes/value | row ~ | params | literal | literal rows/stmt | verdict |
|---|---|---|---|---|---|
| 16 | 128 B | 96 ms | 69 ms | 500 | literal by 28% |
| 64 | 512 B | 90 ms | 99 ms | 200 | within noise |
| 256 | 2 KB | 254 ms | 182 ms | 53 | literal by 28% |
| 1,024 | 8 KB | 231 ms | 289 ms | 13 | within noise |
| 4,096 | 32 KB | 203 ms | 217 ms | 3 | within noise |
| 16,384 | 131 KB | 150 ms | **refused** | — | `statement too long` |
| 65,536 | 524 KB | 202 ms | **refused** | — | `statement too long` |

Literal SQL's chunk collapses as rows grow — 500, 200, 53, 13, 3 — and then
stops being an option at all: once a single row's literal tuple exceeds the
100,000-byte statement limit, there is no chunk size that works. It is not
slower there. It is **unavailable**.

`params` rows-per-statement stays pinned at 12 across every row size, because
bound values travel beside the SQL rather than inside it.

---

## Finding 5 — the two strategies are capped by orthogonal limits

That is the whole result in one line, and everything above is a consequence:

|  | bound by column count | bound by row size |
|---|---|---|
| `params` | **yes** — `floor(100/C)`, reaches 1 at C=51 | no — invariant |
| `literal` | no — invariant | **yes** — collapses, then unavailable |

### The decision rule this supports

- **Wide rows, small values** (high C, short strings): literal SQL, by a
  margin that grows to 2.7x at C=64.
- **Fat values** (any value pushing a row past ~100 KB of literal text):
  bound parameters, because literal SQL cannot express the statement.
- **The broad middle** (C <= 8, rows 512 B to 32 KB): the difference sits
  inside run-to-run spread. Use bound parameters — same speed, and no
  escaping to get wrong.
- **Never chunk to stay under 1000 statements.** That cap is not charged per
  statement inside `batch()`.

---

## Method, so it can be checked

- Harness: `f-keys/d1bench`, Worker version `d5bb2ca7`, one D1 database
  created for this and nothing else.
- Timing: `d1_ms` wraps only the awaited `batch()` call, inside the Worker.
  `total_ms` is whole-request wall clock from the driver. Both reported.
- Repeats are the **outer** loop, so every pass covers all (columns,
  strategy) pairs before the next begins. Run serially by strategy, any
  drift over the sweep would land entirely on whichever ran last and be
  reported as a difference between strategies.
- Tables truncated before every measured run, so table size is not a hidden
  variable.
- A difference smaller than the larger of the two conditions' standard
  deviations is reported as "within noise" and no winner is named.
- Probes climb until D1 refuses and record the refusal text. A documented
  limit that has never been hit on purpose is a number someone typed.

## What these numbers do not support

- **One region, one database, one account, one day.** ENAM only. Region
  placement and neighbor load are not characterized, and D1 is a hosted
  service whose absolute timings are not a property of the strategy.
- **No indexes on any table.** Index maintenance is real write cost,
  excluded deliberately to isolate the variable. A table with three indexes
  will not match these absolute numbers.
- **The duration boundary is bracketed, not pinpointed.** 150,000
  statements succeeded and 175,000 were refused; the exact crossing was not
  bisected, and it would move with row shape in any case.
- **Batch atomicity near the boundary is unresolved.** One refusal rolled
  back cleanly from a verified empty table. A separate refusal with a
  different error text was followed by a full table, but no before-count was
  taken, so nothing is concluded from it.
- **Values contained no characters needing escaping.** Escaping cost scales
  with quote density, which is a separate sweep and was not run. This matters
  only for `literal`, and it is the one axis where the untested direction
  could narrow literal SQL's advantage.
- **`rows_per_stmt` for `params` is set from the documented ceiling**
  (`floor(100/C)`), which Finding 2 confirms is exact. It is not tuned below
  the ceiling, so these are best-case `params` numbers.
- Absolute milliseconds are of this account on this day. The **ratios and
  the orthogonality** are the claims; the raw timings are evidence for them,
  not the result.
