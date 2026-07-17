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
Two lessons, both learned here the hard way:
1. Ask finance ops which date is authoritative; don't infer it.
2. When you switch, switch it **everywhere the date is used as the reconciliation date** —
   matching, filtering, ordering, ageing, grouping, reports — not just the match comparison.
   A half-applied date rule is worse than none: the engine matches on one date while every
   screen filters on another.

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

**Never delete; reclassify or archive.** Deletion of financial rows destroys evidence. Prefer a
status change with the prior state captured in an audit row — reversible and provable.

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
