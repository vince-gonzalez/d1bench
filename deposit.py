#!/usr/bin/env python3
"""Create a Zenodo deposition for this repository, and STOP before publishing.

    python -I deposit.py            # create or update the draft
    python -I deposit.py --publish  # mint the DOI, irreversible

WHY IT DOES NOT PUBLISH BY DEFAULT
A published Zenodo record cannot be withdrawn. The DOI is permanent, it
resolves forever, and the metadata is harvested by indexers within the
day. So this builds the draft, uploads the archive, prints the link to
read it in a browser, and exits. Publishing is a separate explicit act
by a person who has read the thing.

WHAT IT UPLOADS
A source archive of the tagged release taken from `git archive`, so the
bytes deposited are the bytes the tag names rather than whatever is in
the working directory. The findings document goes up alongside it as a
separate file, so the result is readable from the record page without
downloading and unpacking anything.

ZENODO_TOKEN must be in the environment. It is never printed.
"""
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://zenodo.org/api"
TOKEN = os.environ.get("ZENODO_TOKEN")
if not TOKEN:
    sys.exit("ZENODO_TOKEN is not set")

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = "v0.1.0"
ARCHIVE = os.path.join(HERE, "d1bench-%s.zip" % TAG)
PUBLISH = "--publish" in sys.argv

METADATA = {
    "metadata": {
        "upload_type": "software",
        "title": ("d1bench: which Cloudflare D1 write strategy wins, "
                  "and which limit stops you first"),
        "creators": [{
            "name": "Gonzalez, Vincent",
            "orcid": "0009-0005-3640-014X",
        }],
        "license": "mit",
        "version": TAG,
        "language": "eng",
        "keywords": [
            "Cloudflare D1", "Cloudflare Workers", "SQLite",
            "database benchmark", "bulk insert", "bound parameters",
            "serverless", "reproducible measurement",
        ],
        "related_identifiers": [{
            "identifier": "https://github.com/vince-gonzalez/d1bench",
            "relation": "isSupplementTo",
            "scheme": "url",
        }],
        "description": """
<p>Cloudflare documents D1's limits one at a time. It does not document how
they interact, and they interact in a way that changes which bulk-insert
strategy is correct. This harness deploys <em>inside</em> a Worker, because two
of the limits under test only exist inside a Worker invocation, and sweeps
three write strategies across column count and row size against a live
database.</p>

<p>Five measured results, none computed from the documentation.</p>

<ol>
<li><b>A single <code>db.batch()</code> call accepted 128,000 statements</b>,
128 times the documented 1000-query per-invocation cap, at a flat 0.205 to
0.280&nbsp;ms per statement across that entire 128-fold range. The documented
row reads, verbatim, &ldquo;Queries per Worker invocation (read subrequest
limits)&rdquo;, and the parenthetical is routinely dropped when the number is
quoted. The page states that per-query limits apply to each statement inside a
batch, giving statement <em>length</em> as its example, but never says how the
query <em>count</em> is charged. The documentation therefore leaves the
question open while inviting the stricter reading; the measurement settles it,
and a batch is one.</li>

<li><b>The bound-parameter ceiling is exactly <code>floor(100/C)</code></b>,
confirmed by climbing until D1 answers <code>too many SQL variables</code> at
every column count. At 51 or more columns that is one row per statement, so
chunked bound parameters has silently become one-statement-per-row while still
being described as batching by the code that uses it.</li>

<li><b>Literal SQL is 2.7 times faster at 64 columns</b>, 187&nbsp;ms against
503&nbsp;ms, because it still carries 77 rows per statement where bound
parameters carry one.</li>

<li><b>On the row-size axis the comparison reverses and ends in a wall.</b>
Literal SQL's chunk collapses as rows fatten, and once a single literal tuple
exceeds the 100,000-byte statement limit no chunk size works. At roughly
131&nbsp;KB rows literal SQL is refused outright rather than merely slower,
while bound parameters are unaffected, because bound values travel beside the
SQL rather than inside it.</li>

<li><b>The result the other four are consequences of: the two strategies are
capped by orthogonal limits.</b> Bound parameters bind on column count and are
invariant to row size. Literal SQL binds on row size and is invariant to
column count.</li>
</ol>

<p><b>The check that makes these findings rather than timings.</b> Row counts
were reconciled against every statement sent: 260,487 expected across the
batch probes, 260,487 present, exact. A benchmark that reports a time for
writes which never happened is worthless.</p>

<p><b>A methodological result that applies to any Cloudflare Workers
benchmark.</b> In Workers, <code>Date.now()</code> advances only on I/O and is
frozen across pure computation. A clock inside the Worker therefore cannot
observe the CPU spent building a statement, and literal SQL is precisely the
strategy doing that work, so timed only from inside it would appear better
than it is and the headline would be an artifact of the measuring instrument.
Every run was therefore timed from both sides. Measured outside overhead came
to 84 to 98&nbsp;ms and was the same for both strategies at every column
count, establishing that the invisible CPU did not produce the reported
gap.</p>

<p><b>Limitations, stated rather than implied.</b> One region (ENAM), one
database, one account, one day. No indexes on any table. No sweep of escaping
cost, which is the one untested axis that could narrow literal SQL's
advantage. Nothing here tests the 30-second query-duration cap, which applies
per statement and was never approached; an earlier draft of this work claimed
otherwise by conflating a per-statement limit with a per-call total, and that
correction is recorded in the findings document. The ratios and the
orthogonality are the claims; the absolute timings are evidence for them, not
the result.</p>
""".strip(),
    }
}


def req(method, path, body=None, headers=None, raw=None):
    url = path if path.startswith("http") else API + path
    sep = "&" if "?" in url else "?"
    url = "%s%saccess_token=%s" % (url, sep, TOKEN)
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}


def say(label, value):
    print("  %-22s %s" % (label, value))


# --- the archive is built from the TAG, not the working directory ------
subprocess.run(["git", "archive", "--format=zip",
                "--prefix=d1bench-%s/" % TAG,
                "-o", ARCHIVE, TAG],
               cwd=HERE, check=True)
say("archive", "%s (%d bytes, from tag %s)"
    % (os.path.basename(ARCHIVE), os.path.getsize(ARCHIVE), TAG))

# --- reuse an existing unpublished draft rather than making a second --
status, mine = req("GET", "/deposit/depositions?status=draft&size=100")
draft = None
if status == 200 and isinstance(mine, list):
    for d in mine:
        if d.get("title", "").startswith("d1bench:"):
            draft = d
            break

if draft:
    say("existing draft", draft["id"])
    dep_id = draft["id"]
    status, body = req("PUT", "/deposit/depositions/%s" % dep_id, METADATA)
    if status not in (200, 201):
        sys.exit("  metadata update failed: %s %s" % (status, body))
else:
    status, body = req("POST", "/deposit/depositions", METADATA)
    if status not in (200, 201):
        sys.exit("  could not create the deposition: %s %s" % (status, body))
    draft = body
    dep_id = body["id"]
    say("created draft", dep_id)

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
bucket = draft["links"]["bucket"]

# --- upload, replacing any same-named file from an earlier attempt ----
for existing in draft.get("files", []):
    req("DELETE", "/deposit/depositions/%s/files/%s"
        % (dep_id, existing["id"]))

for path, name in [(ARCHIVE, os.path.basename(ARCHIVE)),
                   (os.path.join(HERE, "results", "FINDINGS.md"),
                    "FINDINGS.md")]:
    with io.open(path, "rb") as fh:
        blob = fh.read()
    st, resp = req("PUT", "%s/%s" % (bucket, name), raw=blob,
                   headers={"content-type": "application/octet-stream"})
    say("uploaded" if st in (200, 201) else "UPLOAD FAILED %s" % st,
        "%s (%d bytes)" % (name, len(blob)))

status, draft = req("GET", "/deposit/depositions/%s" % dep_id)
print()
say("state", draft.get("state"))
say("submitted", draft.get("submitted"))
say("files", ", ".join(f["filename"] for f in draft.get("files", [])))
say("reserved DOI", draft.get("metadata", {}).get(
    "prereserve_doi", {}).get("doi", "(assigned on publish)"))
print()
print("  READ IT HERE before anything is permanent:")
print("    %s" % draft["links"].get("html", "(no html link returned)"))

if not PUBLISH:
    print()
    print("  Not published. The draft is private to the account and nothing")
    print("  has a DOI yet. To mint it:  python -I deposit.py --publish")
    sys.exit(0)

print()
print("  PUBLISHING - this is irreversible")
status, body = req("POST", "/deposit/depositions/%s/actions/publish" % dep_id)
if status not in (200, 202):
    sys.exit("  publish failed: %s %s" % (status, body))
say("published DOI", body.get("doi"))
say("record", body.get("links", {}).get("record_html"))
