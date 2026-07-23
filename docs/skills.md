# Skills — how to build a reconciliation platform like this

A transferable playbook: the **domain knowledge, engineering patterns, and hard-won lessons**
needed to build a real-money reconciliation system. Written so someone starting from an empty
repo could rebuild this class of application — and so anyone extending *this* one inherits the
reasoning instead of rediscovering it the expensive way.

**How this differs from the neighbouring docs**

| Doc | Answers |
|---|---|
| `architecture.md` | How *this* app is wired, end to end |
| `behavior-contract.md` | Which behaviours are load-bearing and must not change |
| `known-issues.md` | What is deliberately left unfixed, and why |
| `onboarding.md` | How to get productive in *this* repo |
| **`skills.md`** (this) | **The skills & patterns to build one at all** |

> **Maintenance rule.** This is a living document. When you add a capability, hit a
> non-obvious bug, or learn something that would have saved you a day — add it here in the
> same PR. A lesson that only lives in a commit message is lost. Keep it **public-safe**:
> no credentials, hostnames/IPs, account numbers, customer data, or personal names.

---

## 1. Domain skills — the reconciliation model

You cannot engineer this correctly without the domain. Get these concepts right first.

**The two-sided problem.** Reconciliation compares two independent records of the same money:
- **Bank side** — what the bank says happened (a statement).
- **Internal side** — what your system says happened (a dump/ledger export).

The job: pair each bank row with its internal counterpart, and explain every row that doesn't
pair. Everything else is detail.

**Identifiers are the join keys.** Real systems have several, of varying reliability:
transaction ID, UTR/RRN (bank reference), tracking number, cheque/ATM ref. They arrive
embedded in free-text narration as often as in clean columns. **Which identifier takes
priority is a business decision, not an engineering one** — different partners key on
different fields, and getting the priority order wrong silently mis-pairs money. Make the
priority *configurable per partner* and confirm it with finance ops.

**Amounts need tolerance, and tolerance is per-product.** Two records of the same payment
routinely differ by rounding or fees. A tolerance is a business rule (how much drift is
"the same payment"), so it varies by product. **Equalising tolerances across products
reclassifies live money** — never "tidy" them into one constant.

**Dates are subtler than they look.** A transaction can carry several dates — initiation,
transaction, value, posted. Reconciliation must key on **the date the money actually moved
on the counterpart's books**, which is frequently the *value* date, not the transaction date.
Three lessons, all learned here the hard way:
1. Ask finance ops which date is authoritative; don't infer it.
2. When you switch, switch it **everywhere the date is used as the reconciliation date** —
   matching, filtering, ordering, ageing, grouping, reports — not just the match comparison.
   A half-applied date rule is worse than none: the engine matches on one date while every
   screen filters on another.
3. **Reconcile by the transaction's *business* date — never by the upload time or the clock.**
   One subsystem selected "which rows to reconcile" by *upload batch, defaulting to today*,
   decoupled from the date the operator picked. Running on any other day then reconciled the
   wrong data (or none), and — combined with delete-and-recreate — **wiped good results**.
   "Recon date" must mean the business day being reconciled, and the run must load exactly that
   day's rows. A bulk/back-dated upload should reconcile *each day it contains*, not "today".

**Statuses are a vocabulary, and the vocabulary is the product.** You will need more than
matched/unmatched. Real categories include: matched (auto), manually matched, matched across
accounts, internal self-match, amount mismatch (IDs agree, money doesn't), duplicate, failed,
reversal, fee, fund transfer, and "dispositioned" (a human assigned a reason code).

**Not every row needs reconciling.** Fees, reversals, failed transactions, bank debits,
internal transfers and duplicates are *legitimately* unmatchable. They must be **counted and
explained**, never silently dropped. See §5 — this is the single most common reporting bug.

**Beyond matching.** A mature system also needs: ageing (how long has this been open),
escalation (chase the counterparty past D+n), settlement (T+n payouts), funds position
(reconstructing bank balances from statement movements), and an audit trail for every human
override.

---

## 2. The matching engine

**Scope matching narrowly and deterministically.** Match per `(partner, date)` with an
ordered rule list, first-match-wins. Deterministic beats clever: an engine whose output you
can predict and characterise is one you can safely change.

**Rules as data, not code.** Store priority rules per partner in the DB with a UI to edit
them. Ops changes the priority order far more often than engineers change the engine.

**Pass order is load-bearing.** Real pipelines need several passes (reversal handling,
primary match, cross-date passes, internal self-match). **The order determines the outcome** —
document it and treat reordering as a behaviour change.

**Match IDs are external references.** Humans quote them in emails to banks, so they must be
stable and unique forever. Patterns that work: `{PREFIX}-{YYYYMMDD}-{NNNN}` sequenced by
`MAX+1`. Two traps:
- **Never sequence by `COUNT(...)`.** If rows are ever deleted/replaced, the count shrinks and
  IDs get **recycled onto unrelated matches**. `MAX+1` is immune. (This bit this codebase.)
- `MAX+1` needs serialisation. A process-local lock works **only for a single worker** — going
  multi-worker requires a real atomic sequence first.

**Re-runs must preserve human work.** Re-running recon typically resets rows to unmatched.
Any row a human touched (manual match, cross-account match, assigned reason code) must be
**excluded from re-matching**, or the next run silently destroys their work — and, worse,
orphans one leg of pairs whose counterpart lives elsewhere.

**Guard manual matches too.** If auto-matching flags an amount discrepancy, the manual path
must flag it identically. Otherwise a human pairing ₹2L with ₹10L saves as a clean match.
**Every path that can create a pair needs the same guard.**

---

## 3. Ingestion & parsing

**Every bank format is bespoke, and they change.** Expect one parser per source: differing
headers, junk preambles, footer legends, date formats, and locale number formatting. Budget
for this — it is the majority of the work, forever.

**Detect, don't assume.** Auto-detect the header row; auto-map columns; let the user confirm
via a preview before committing. Never trust a fixed row/column offset.

**Reject wrong files loudly.** Someone will upload the right file to the wrong product. Detect
it and hard-fail with a clear error, rather than ingesting garbage into a money ledger.

**Guard against re-upload.** Hash the file bytes and refuse duplicates — but understand the
failure mode: **a hash outlives the data it created**. If the rows are later replaced/cleared,
the hash still blocks a legitimate re-upload of the same file. Provide a deliberate override.

**Replace semantics are a data-loss trap.** "Delete all rows for this account, then insert the
file" destroys history the moment someone uploads a single-day file. Prefer **replacing only
the dates present in the file** so incremental uploads accumulate and a re-upload supersedes
only its own dates.

**Row classification order matters.** When one row could match several classifiers (a reversal
that is also a fee), the check order decides its identity. Pin the order and comment *why*.

**Non-transaction rows exist.** Statements contain opening/closing balances, totals, legends.
They carry no money and must not enter the transaction ledger as transactions — but they may
still be valuable (stated balances feed a funds-position feature). Classify, don't discard.

**Don't fork the pipeline.** If you have two ingest paths (interactive upload + a watch-folder
or API), they will drift, and a fix in one silently misses the other. **Share one core.** This
codebase carried two divergent copies for months; the dormant one still had the data-loss bug
the interactive one had already been fixed for.

**Formats change under you, without warning.** A bank that shipped tab-separated text for years
(under a `.xls` extension, which was never a real workbook) will one day ship a genuine `.xlsx`
— same columns, same order, now wrapped in a zip with a metadata block on top. The parser
decodes the zip as text, finds no header, and the whole day is un-uploadable. **Detect the
shape by magic bytes** (`PK` = xlsx zip, `\xd0\xcf\x11\xe0` = legacy OLE, else text), normalise
every shape into ONE row structure, and keep a single row-building loop. Adding a format should
be a new branch in the *reader*, never a second copy of the *parser*.

**Beware guards that only worked by accident.** A trailing footer line ("*This is a computer
generated statement…*") is dropped for free by a text parser: splitting it yields one field, so
a `len(parts) < 6` check discards it. Read the same file as a workbook and every row is **padded
to the full grid width** — the footer now has 8 cells, sails through that guard, and lands in
the ledger as a fake transaction (in this case also feeding a bogus `0.0` balance into the
funds-position snapshot). When you change how rows are produced, re-check every guard that
depended on the old shape. Trimming trailing empty cells restored the old semantics exactly.

---

## 4. Application architecture

**One process is a feature.** API + engines + scheduler + built SPA in a single deployable is
dramatically easier to run than microservices, and reconciliation is not throughput-bound.
Reach for complexity only when a real constraint forces it.

**Layering that survives:**
- `routes/` — thin HTTP; validation and orchestration only.
- `core/` — engines and domain logic; **no HTTP awareness**. This is the part worth testing.
- `models/` — schema + migrations.
- Additive read-only modules (health, quality, ledgers, analytics) that **never touch matching**.

**Dual-database from day one.** Support SQLite (dev/demo) and a server DB (prod) behind one
env var. Cheap early, very expensive to retrofit.

**Migrations without a framework** are viable — idempotent `ALTER`-adds run at startup — but
know the trade-off: seeders keyed on **exact strings** double as data migrations, so editing
a literal re-triggers or skips a migration on deployed databases.

**Pick one timezone convention and enforce it in one place.** Here: store naive UTC, convert
to local at the edge (one HTTP interceptor + server-side for exports). Pages contain zero
timezone logic. Mixed conventions produce double-shifted timestamps that are agony to trace.

**Money type.** Floats with explicit tolerances are workable *if* tolerances are deliberate and
comparisons never test equality. Decimal/integer-minor-units is the safer default for a new
build. Whatever you choose, **decide once** and write it down.

**Registries: one source of truth, or drift is guaranteed.** Product lists, status sets, member
banks, reason codes, report types, permissions — each will be needed in several files. Every
duplicated copy *will* drift. Export one definition and import it. Where a duplicate is
unavoidable, comment the mirror explicitly so the next person updates both.

---

## 5. Reporting & analytics — where the subtle bugs live

Reporting looks harmless and is where most correctness bugs hide. Every one of these was a
real defect here.

**Fold many vocabularies into a few buckets — carefully.** Each product has its own status
strings; dashboards need a common shape (matched / unmatched / mismatch / other). The bucket
map becomes a **second source of truth for "what counts as matched"**, and it drifts from the
engine's definition. Symptoms:
- A genuine matched status missing from the map → counted as "other" → **matched undercounted
  and the rate understated** (a product read 0% while fully reconciled).
- A product-specific reconciled status (e.g. a failed-then-refunded pair) missing → that
  product's rate is quietly wrong on every dashboard.

**Never let "other" be invisible.** If you show matched/unmatched/mismatch but the total
includes a fourth bucket, **the columns don't add up and users lose trust**. Worse, a rate of
`matched / (matched+unmatched)` can read **100% while most rows are hidden in "other"**. Two
rules:
1. Show the "other" count as a column.
2. Return its **composition** (which statuses, how many) so the UI can explain *why* those
   rows need no reconciliation.

**Count a pair once.** A matched transaction is two rows (bank leg + internal leg). Headline
counts must de-duplicate — conventionally by counting the bank leg. But then beware:
**an internal self-match has no bank leg**, so a naive "drop the internal leg" rule deletes it
entirely. Exempt it.

**Know your denominator.** `matched/(matched+unmatched+mismatch)` and `matched/total` are both
defensible and give very different numbers. Different screens using different denominators
will be reported as a bug. Pick one per surface and document it.

**Don't blend populations in one health metric.** A single blended match rate lets a healthy
product mask a broken one (and vice-versa). Assess **per product** and report the worst
offender by name — a blended average is unactionable.

**Reporting must cover every product.** Any surface that queries only the main ledger silently
omits products that reconcile in their own tables. When adding a product, grep every
aggregation, export, digest, health check and scheduled report. **Better: route them all
through one shared aggregator** so a new product appears everywhere at once.

**Operational vs analytical surfaces differ deliberately.** Operational screens (run recon,
manual match, rule editors) are legitimately scoped to one ledger; analytical surfaces must be
all-products. Write that rule down, or every audit re-flags the intentional exclusions.

---

## 6. Safety discipline for money code

**Write a behaviour contract.** Enumerate the invariants that must not change, and cite them
by number in review. Without it, every refactor is a coin flip.

**Characterisation tests before refactors.** Pin *current* behaviour — including behaviour you
think is wrong — then refactor. Tests that assert what the code does today are the only safe
way to change code nobody fully remembers.

**Catalogue deliberate non-fixes.** Otherwise each new reviewer "fixes" the same intentional
quirk. A `known-issues.md` pays for itself immediately.

**Adversarially review money-path diffs.** Have an independent pass try to *refute* the change
rather than approve it. In this codebase that discipline repeatedly caught real defects before
deploy — including bugs in code that had already passed a normal review.

**Verify against production reality, not just tests.** A green suite proves the code does what
you wrote; it does not prove the *data* behaves as you assumed. Exercise the real flow and
read the real numbers.

**Layout/visual bugs need rendering.** Reviewers reading code cannot see overlapping labels or
a clipped chart. Render it and assert coordinates, or look at the output.

**Never delete; reclassify, archive, or soft-delete.** Deletion of financial rows destroys
evidence. Prefer a status change with the prior state captured in an audit row. Where a
"delete" genuinely is the operation (clearing a bad upload), route it through a **recycle
bin**: serialise the rows *before* deleting them, so a mistake is one restore away instead of
a restore-from-backup. Make restore idempotent (skip rows whose primary key already exists) so
a double-restore can't duplicate live data — and coerce serialised values back to their column
types on the way in (JSON has no date type, so an ISO string handed to a DateTime column is
rejected; convert it back first).

**A filter that can be silently ignored is a wipe waiting to happen.** A destructive endpoint
took an early branch for one class of input (module products) that `return`ed *before* the
date/side/status filters were applied — so "clear one day on the bank side" truncated the
entire product: bank rows, the other side, config tables, and derived results (a live
incident). The rule: a destructive operation must **apply every filter it accepts, or refuse
the request** — never accept a filter and then ignore it. If a particular filter can't be
honoured for some target (a table with no business date, a status that doesn't map), return an
error explaining why; do not fall through to deleting more than the user asked for.

**Audit everything a human changes**, with the previous state, a mandatory reason, and who did
it. Add dual-control (maker–checker) for sensitive actions — and make sure it covers **every**
mutating endpoint, including newer products bolted on later.

**Fail open, not closed, on non-essential paths.** Auto-recon chains, notifications and health
checks must never block an upload or crash a report. Swallow and log.

---

## 7. Operations & deployment

*(Patterns only — environment specifics live outside the repo.)*

**Build locally, ship artefacts.** Building on the server invites drift between the server's
sources and the repo. Build from a known-clean checkout and copy the artefacts.

**Deploy every asset the entry point references.** A build can rehash both JS *and* CSS. Ship
only the JS and the app loads **completely unstyled** — HTML and JS work, so a naive check
passes. Enumerate the asset references from the built HTML, upload each, then **fetch each one
back and assert 200** — checking the page itself is not enough.

**Copy files into a directory, never copy the directory.** Copying a directory can reset its
permissions so the web server can't traverse it — every asset 404s while the files look
perfectly fine on disk. Verify the directory mode too.

**Cache-bust the entry point.** Hashed asset names are useless if the HTML that references them
is cached: users keep running the old bundle and report your fix as broken. Serve the entry
HTML `no-cache`.

**Run the app under a supervisor** (systemd unit or equivalent) with restart-on-failure and
start-on-boot. A process started by hand *will* be down after a reboot and nobody will notice.

**Back up on a schedule, and prove it restores.** A consistent, compressed dump on a cron with
retention. When you move it, update the schedule and the script paths **together**, then run it
by hand and confirm a fresh dump appears.

**Keep operational data out of the source tree** — or, if it must live there, ensure it is
ignored *before* it arrives. DB dumps and credential files inside a repo with a public remote
are one `git add -A` from disaster.

**Secrets never enter git, docs, or chat.** Env files, ignored by default, with an
`.env.example` documenting the keys and no values.

---

## 8. Gotchas that actually bit

Concrete, reusable, and each one cost real time.

| Trap | Why it hurts | Do this |
|---|---|---|
| Sequencing IDs with `COUNT+1` | Rows get replaced → IDs recycle onto unrelated matches | `MAX+1`, serialised |
| Full-account delete on upload | Single-day file wipes months of history | Replace only the file's dates |
| File-hash guard with no override | Hash outlives its data → blocks legitimate re-upload | Deliberate force path |
| Two ingest pipelines | One gets fixed, the other keeps the bug | Share one core |
| Source silently changes export format | Text-shaped `.xls` becomes a real `.xlsx`; parser hard-fails, a whole day is un-uploadable | Detect by magic bytes, normalise to one row shape, one parser |
| A single `.xls` reader that a Java-written file defeats | The SBI Limit Fail report is a jExcelApi BIFF8 (`build=4307 year=1996`) that xlrd 2.0.1 rejects with "BOF not workbook/worksheet"; it's real OLE2, so magic-byte sniffing won't catch it | Chain readers: xlrd → **python-calamine** (opens JXL/odd BIFF xlrd can't) → openpyxl; verify a genuinely-empty file (0 BIFF cell records) reads as 0 rows, not an error |
| Synchronous full re-recon on every upload | Auto-reconciling *all* dates inside the upload request grew from seconds to ~14 min as data accumulated; on a single worker it froze the whole app on each upload ("stuck Uploading…") | Scope auto-work to the dates the uploaded file actually touches; make "no dates" (`[]`) mean *reconcile nothing*, distinct from "unknown" (`None` → all) so an empty list can't fall through to a full run |
| A no-override rule justified by "this data can never be lost" | The internal-dump path refused `force` because "upsert never bulk-deletes" — then the Clear screen bulk-deleted those rows, and the lingering file hash blocked the restore re-upload forever | Such justifications die the moment *any other feature* can delete the data. Give every guarded path a deliberate force flow; close the path-specific hazard (skip append-only rows on a forced replay) instead of banning the override |
| Manual matches that survive re-runs + a delete path with no cascade | Auto matches self-heal (re-runs re-derive them); manual/preserved dispositions are skipped by re-runs *by design* — so deleting their partner leg leaves them "matched" forever, pointing at nothing | Every delete path (clears, per-id upserts, provider wipes) must revert surviving legs sharing the deleted rows' match ids — using the FULL paired-status set, not a `matched%` prefix (wrong_amount/amount_mismatch pairs count too) |
| A "Re-upload (replace)" button over an APPEND endpoint | The UI promised replace; five of six uploads appended — pressing the button doubled per-KO sums that feed settlement totals | Force may only be offered where re-applying is idempotent. Convert appends to *scoped* replace (the file supersedes exactly what it re-states: its dates × its product/BC) or content-key dedup — a blanket per-date replace destroys sibling files that share the date |
| Guard that worked by accident | A footer line dropped by `len(parts)<6` in text survives when a workbook pads rows to full width → fake txn + poisoned balance | Re-check guards when row production changes; trim trailing empties |
| Verifying a parser by row count alone | Counts can look right while amounts/dates are silently wrong | Reconcile the balance chain: opening + credits − debits = stated closing |
| Destructive endpoint with an early branch | A per-type shortcut `return`s before filters apply → "clear one day" wipes the whole product | Apply every accepted filter, or refuse; never silently ignore one |
| Append-only ingest + a re-upload | Recovering from one mistake (re-uploading) causes another (doubled data) | Make ingest idempotent (per-date replace) + a duplicate-file guard |
| Hard delete of financial rows | A mis-scoped clear is terminal; recovery means a DB dump | Soft-delete: serialise rows to a recycle bin first; idempotent restore |
| Reconciling by upload/clock date, not business date | Running on the wrong day reconciles the wrong data (or none) and can wipe good results | recon_date = the transaction's business date; load exactly that day |
| Delete-then-reload rebuild order | If the reload finds nothing, the delete already destroyed the prior good result | Load sources FIRST; if empty, skip — never delete what you can't rebuild |
| Low match rate read as "recon broken" | It's usually a missing counterpart file, not bad matching | Show per-period file presence so missing-file ≠ real discrepancy |
| Status set copied into N files | Drifts; a matched status lands in "other" | One exported definition |
| Half-applied business rule | Engine matches on one date, screens filter on another | Apply to every use of the concept |
| Hidden "other" bucket | Columns don't sum; rate can read 100% while rows hide | Show count + composition |
| De-dup "drop the internal leg" | Zeroes out internal self-matches (they have no bank leg) | Exempt them |
| Blended health metric | One product masks another's failure | Per-product, name the worst |
| Aggregations that query one ledger | New products silently missing everywhere | One shared aggregator |
| Deploying JS without CSS | App loads fully unstyled; naive checks pass | Upload all assets, fetch each, assert 200 |
| Copying a directory over | Perms reset → every asset 404s, files look fine | Copy files in; verify dir mode |
| Cached entry HTML | Users run the old bundle; your fix "didn't work" | `no-cache` the entry point |
| ORM enum reads back as a plain string | `.value` raises; comparisons look right but aren't | Normalise defensively |
| Calling a web-framework handler directly in a script | Default arg objects are truthy → bogus filters, empty results | Pass explicit args or drive it over HTTP |
| Non-sargable filter (`COALESCE(a,b)`) | Correct but drops the index | Fine at small scale — know you traded it |
| Trusting the left-most forwarded-IP header | Client-controlled → rate limits trivially bypassed | Use the value your proxy sets |
| Assuming a code path is dead | "Dormant" paths run later, with the old bug | Fix or delete, don't leave landmines |
| Auditing "should-match" by a composite/heuristic key | Keying on `(id, amount)` both **invents** false pairs (values coincide across unrelated rows — an adjacent-day audit found 7,638 "candidates", all coincidences) and **hides** real ones (one component differs) | Probe by the row's **unique** identifier (reference no.); a unique-key join is collision-free, and cross-checking a second process that already matches on it corroborates |
| An identifier truncated on one side of a join | One system emits the id truncated to a 7-char prefix, the other the full 8–10 chars → a key on that id **silently never matches** (here: ~2,244 real txns, ~₹74 L, split in two) | Test prefix relationships to spot it; fix by keying on the shared **unique** field — never "normalise" by truncating, because prefixes collide (3,383 groups mapped to different real ids) |
| Long remote job run inline over SSH | The client timeout backgrounds the local ssh, then a connection reset SIGHUPs the remote job **mid-write** | `nohup … & disown` + a logfile, poll the log; take a pre-run backup first — per-step commits mean a mid-run kill can leave a partial state |
| Shipping an "approved" fix without measuring its live effect | A sign-off can predict the wrong outcome. A reference-key fix approved to add ~2,244 matches instead *removed* ~7,700 — the old key had been making false matches the approval never accounted for | Re-run against a backup, diff before/after; if the effect contradicts the sign-off, **revert and re-confirm** — don't ship real-money reclassification on a contradicted expectation |
| Fixing the match *key* of a scope-limited process | Swapping `(ko,amount)`→reference in a **credit-only** reconciliation exposed that money-IN rows match **debits** it never scans, so they flipped to Unmatched — the fix needs a *scope* change (match all rows), not just a key change | Check the process's row scope before changing its key; a subset process may need merging/broadening, which is a bigger (sign-off) decision than a key swap |
| Verifying on a sample that can't trigger the bug | A ref-format check `^61…` passed 100% against a day whose refs were *all* 61-prefixed; other days use 62-prefix and were silently mis-classified | Pick verification cases that span the input's real variety (here: check a date of each ref prefix); "matches exactly" on one slice ≠ correct |
| Treating a placeholder as a real value in a grouping key | Grouping bank rows by reference to find reversals lumped every no-ref (`'- / -'`) row — which naturally spans debits+credits — into one bogus 977-row "reversal" | Exclude the empty/placeholder sentinel from any key that drives classification; validate the token (20 digits) before it groups |
| Adding an endpoint after a concept-grep missed the existing one | Grepping for the *concept* ("unmatch") in noisy output missed the real route; the duplicate registered on the same path+method is **silently shadowed** (first registration wins) — dead code that looks live, minus the original's remark/approval safeguards | Grep for the exact route string (`post("/unmatch"`) before writing any endpoint; if a duplicate path ever appears, keep the richer original and delete yours |
| Widening a per-day report to a date range by widening its queries | The day engine's matching pools (ref index, reversal pairing, one-to-one settlement consumption) are per-call; pooling the range lets day-1 rows consume day-2 rows — silent cross-date reclassification | Iterate the day engine per date and concatenate; keep per-day row numbers (they reference the day's statement) and let the existing date columns disambiguate |
| A range built from per-day passes inherits each day's missing-input state silently | A day whose bank file wasn't uploaded contributes a full day of phantom "needs review" unmatched rows to the range workbook; a day known only to a third table is dropped entirely — both invisible inside a file labelled from→to | Resolve range dates from EVERY table that feeds the report, and flag half-loaded days in the summary ("missing file, not a recon failure" — same words the dashboard already uses) |
| Refactoring a finance-verified report on tests alone | Synthetic fixtures can't cover every real-data path through a report builder | Before deploying, dump every cell of the old builder's output for a real business date on the server; after deploying, regenerate and `diff` — identical-or-explain is the bar |
| Excel text-marker apostrophes in identifier columns | One day's dump exported with '-prefixed id cells stores `'35701…` while the bank side is clean — exact-equality matching finds zero overlap and the whole day reads unmatched (looks like a recon failure, is an encoding artifact) | Strip leading apostrophes in the shared value-cleaner (they are formatting, never data; interior ones — O'Brien — stay); diagnose unmatched days by re-joining ids after normalization before suspecting the matcher |

---

## 9. If you are building one from scratch

A defensible order of work:

1. **Model the domain** — two sides, identifiers, statuses, tolerances, dates. Write it down.
   Confirm every business rule with finance ops before coding it.
2. **Ingest one source end to end** — parse → preview → confirm → store. Get the unglamorous
   parsing right; it is most of the work.
3. **Match one partner** — ordered rules, first-match-wins, one tolerance, deterministic IDs.
4. **Build the exception views** before the dashboards. Open items and mismatches are what the
   team uses all day; dashboards are what management looks at occasionally.
5. **Add the audit trail and overrides early.** Retrofitting provenance is miserable.
6. **Only then generalise** to more products — and route every new product through the shared
   aggregator, exception window and report registry so it appears everywhere at once.
7. **Write the behaviour contract the moment the first rule is real**, and characterisation
   tests before the first refactor.

The recurring theme: **the hard part is not the matching algorithm — it is the honesty of the
data around it.** Explaining every row you did not match matters more than matching a few more.
