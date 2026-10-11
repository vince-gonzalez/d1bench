```
╔════════════════════════════════════════════════════════════════════════════════════════════╗
║                                                                                            ║
║            ██████╗  ██╗██████╗ ███████╗███╗   ██╗ ██████╗██╗  ██╗                          ║
║            ██╔══██╗███║██╔══██╗██╔════╝████╗  ██║██╔════╝██║  ██║                          ║
║            ██║  ██║╚██║██████╔╝█████╗  ██╔██╗ ██║██║     ███████║                          ║
║            ██║  ██║ ██║██╔══██╗██╔══╝  ██║╚██╗██║██║     ██╔══██║                          ║
║            ██████╔╝ ██║██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║                          ║
║            ╚═════╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝                          ║
║                                                                                            ║
║           Which D1 write strategy wins, and which limit stops you first.                   ║
║                                                                                            ║
╚════════════════════════════════════════════════════════════════════════════════════════════╝
```

Cloudflare documents D1's limits one at a time. It does not document how they
interact, and they interact in a way that changes which bulk-insert strategy is
correct.

**Results: [`results/FINDINGS.md`](results/FINDINGS.md).** Raw data:
[`results/sweep.json`](results/sweep.json),
[`results/rowsize.json`](results/rowsize.json).

## What it found

| | |
|---|---|
| `batch()` is not charged per statement | 128,000 statements in a single call, 128x the documented per-invocation cap, at a flat 0.205–0.280 ms each |
| The parameter ceiling is exactly `floor(100/C)` | Confirmed by climbing until D1 answers `too many SQL variables`, at every column count |
| Chunked bound parameters stops being batching | At 51+ columns the ceiling is 1 row per statement, which is the naive strategy under another name |
| Literal SQL is 2.7x faster at 64 columns | 187 ms against 503 ms, because it still carries 77 rows per statement where bound parameters carry one |
| Literal SQL then becomes *unavailable* | Past roughly 131 KB of row, one literal tuple exceeds the statement limit and no chunk size works. Not slower. Refused. |
| The two are capped by **orthogonal** limits | Bound parameters bind on column count and ignore row size. Literal binds on row size and ignores column count. Everything else follows. |

## The one thing to know before trusting any of it

In Workers, `Date.now()` advances only on I/O. It is frozen across pure
computation — a deliberate timing-attack mitigation, not a bug. So a clock
inside the Worker **cannot see** the CPU spent building a statement, and
literal SQL is precisely the strategy doing that work. Timed only from inside,
literal SQL looks better than it is, and the headline becomes an artifact of
the measuring instrument.

So every run is timed twice: `d1_ms` from inside, around the awaited `batch()`
call, and `total_ms` from the driver, which includes CPU and network. Measured
outside overhead came to 84–98 ms and was **the same for both strategies at
every column count**, so the invisible CPU is not what produced the gap.

Reporting one clock without the other is how this gets the answer wrong.

## Why it runs inside a Worker

Two of the limits under test only exist inside a Worker invocation — queries
per invocation, and the 30-second cap on a `batch()` call. Measured from a
laptop those limits do not exist, and the number you get is mostly your own
internet connection.

## Why every writing route is gated

A public endpoint that inserts D1 rows as fast as it can is a billing hole
with a documented API. `BENCH_TOKEN` gates everything except `/limits`, and
there is no custom domain.

## Reproduce it

```
npx wrangler d1 create f-keys-d1bench     # put the id in wrangler.toml
npx wrangler secret put BENCH_TOKEN
npm run deploy

set D1BENCH_BASE=https://<your-worker>.workers.dev
set D1BENCH_TOKEN=<the BENCH_TOKEN value>

python -I drive.py            # column-count sweep
python -I drive_rowsize.py    # row-size sweep
```

Nothing is hardcoded to the account that produced the published results. Both
drivers exit naming the variable to set when it is missing.

## Method, so it can be checked

- Repeats are the **outer** loop, so every pass covers all (columns, strategy)
  pairs before the next begins. Run serially by strategy, any drift over the
  sweep would land entirely on whichever ran last and get reported as a
  difference between strategies.
- Tables are truncated before every measured run, so table size is not a
  hidden variable.
- A difference smaller than the larger of the two conditions' standard
  deviations is reported as **within noise** and no winner is named.
- Probes climb until D1 refuses and record the refusal text. A documented
  limit that has never been hit on purpose is a number someone typed.
- Row counts are reconciled against every statement sent. A benchmark that
  reports a time for writes which never happened is worthless, and across the
  batch probes it came to 260,487 expected and 260,487 present.

## What these numbers do not support

One region (ENAM), one database, one account, one day. No indexes on any
table. No sweep of escaping cost, which is the one untested axis that could
narrow literal SQL's advantage. The ratios and the orthogonality are the
claims; the absolute milliseconds are evidence for them, not the result.

`results/FINDINGS.md` carries the full limitations list, including a
correction to an earlier draft of this work.

## License

MIT. See [LICENSE](LICENSE).
