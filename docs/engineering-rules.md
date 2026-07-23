# Engineering rules for a multi-bank reconciliation system

The professional rulebook for building and evolving an app that reconciles real money
across many banks, formats, and variance scenarios. Distilled from this codebase's own
incidents (see `docs/skills.md` for the war stories) plus standard financial-engineering
practice. These are *rules*, not suggestions — each one exists because breaking it has a
known failure mode.

## 1. Money & data integrity

1. **Source rows are immutable evidence.** Never edit an ingested bank/internal row in
   place; classification lives in *status* fields or *result* tables beside the row.
   Reconciliation output must always be re-derivable from the sources.
2. **Never hard-delete financial rows.** Every delete is a soft delete into a recycle bin
   (serialized rows + batch id + who/when), restorable and idempotent. A mis-scoped clear
   must be recoverable without a DB dump.
3. **Deleting one leg of a match un-matches the other.** A match is a pair; removing a row
   that participates in a match must revert its counterpart to unmatched and retire the
   manual-match record. A "matched" row whose partner no longer exists is a lie the
   auditor will find.
4. **One writer.** Matching runs single-writer (per process/date). Sequenced IDs come from
   `MAX+1` under a lock or an atomic sequence — never `COUNT+1` (deleted rows recycle IDs
   onto unrelated matches). Don't run a second recon process beside the live one.
5. **Tolerances are contracts, not conveniences.** Each counterparty's amount tolerance is
   an explicit, documented constant validated against real data (here: paisa-exact was
   proven on 27k+ matches; a ₹1 band was unsupported and widened mis-attribution room).
   Changing a tolerance reclassifies money — it needs sign-off, not a refactor.
6. **Audit every mutation.** Uploads, runs, clears, manual matches, restores — actor,
   action, filters, counts, before-state where cheap. The audit log is how you reconstruct
   incidents (it is how the "who deleted this data" question gets answered in minutes).

## 2. Matching

1. **Match on the transaction's unique identifier first** (reference/UTR/RRN). Composite
   heuristics like `(party, amount)` both *invent* pairs (coincidences) and *miss* real
   ones (a component differs — e.g. one side truncates the party id). Amount then becomes
   a *validation* on the ref match, not the key.
2. **Validate identifiers before they group or match.** Placeholder sentinels (`'- / -'`,
   blanks) must never act as join keys — a placeholder key once glued dozens of unrelated
   rows into one phantom "reversal". Know the identifier's real format (length, charset)
   and don't over-fit it (a `61…`-only check silently rejected other-prefix dates).
3. **One-to-one consumption.** A source row is consumed by at most one bank row per run;
   prefer amount-agreeing candidates, then first-match-wins deterministically. Same input
   → same output, always (no randomness, no dict-order dependence).
4. **Classify the residue, don't lump it.** "Unmatched" must be split by *why*: failed at
   source (expected), missing counterpart file (operational), settlement handled by a
   different process, genuine discrepancy (review). One undifferentiated bucket hides the
   87 rows that matter inside 2,000 that don't.
5. **Explained ≠ open.** Reversal pairs (net-zero DR+CR), settlement legs matched
   elsewhere — keep them visible as their own labeled states, counted as *reconciled*,
   never as open items and never silently as "matched to a success".
6. **Cross-check engines against each other.** Two processes matching the same rows by
   different keys is free corroboration (a ref-match engine confirmed 2,244 pairs the
   composite-key engine missed). When two views disagree, one of them is teaching you
   something.

## 3. Ingestion

1. **Trust content, not names or extensions.** Sniff magic bytes; a ".xls" can be OLE2,
   a Java-written BIFF variant, real .xlsx, HTML, or tab-text — sometimes from the same
   sender in the same week. Chain readers (xlrd → calamine → openpyxl → text) so one
   writer-quirk can't hard-block a business day.
2. **Business date, never upload date.** Every row carries the transaction's business
   date; every run, replace, and report is scoped by it. "Today" is meaningless for a
   back-dated or bulk file.
3. **Idempotent by construction.** Re-applying the same file must not double anything:
   per-date replace for statement-shaped data, upsert-per-unique-id for dump-shaped data,
   and append-only rows must be guarded or skipped on re-apply.
4. **Duplicate-file guard WITH a deliberate force path.** Hash every accepted file and
   block byte-identical re-uploads — but the hash records *file ever seen*, not *data
   still present*. Any feature that can delete the data (clears!) invalidates a
   "re-upload can never be legitimate" assumption, so every guarded path needs an
   explicit, confirmed force/replace flow.
5. **Drop the furniture explicitly.** Footer lines ("computer generated…"), metadata rows,
   non-money event rows (GUID-style refs) are detected and excluded by rule — not left to
   luck (a footer once survived a format change and poisoned a balance).
6. **Verify parses by value, not by row count.** Reconcile the balance chain
   (opening + credits − debits = closing) or a checksum column; counts look right while
   amounts are silently wrong.
7. **Scope side effects to the file.** An upload triggers work for *its* dates only. A
   full-history job inside a request handler grows linearly with data until it freezes
   the app (it did: ~14 minutes, single worker).

## 4. Change safety (the discipline that keeps it alive)

1. **Behavior contract + isolation of change.** Load-bearing behaviors are written down;
   nothing under the contract changes silently. Every change must be additive or isolated
   to its feature — and the post-change audit re-verifies that *untouched* modules produce
   identical numbers, not just that the new thing works.
2. **Measure a matching change against a backup before shipping.** Re-run on a copy, diff
   before/after. If the observed effect contradicts the sign-off's stated expectation
   (approved "+2,244" that actually measured "−7,702"), STOP and re-confirm — the model
   behind the approval was wrong somewhere.
3. **Verify on inputs that can trigger the bug.** A fix validated only on a slice where
   the bug can't occur (one date, one ref-prefix, an empty file) proves nothing. Pick
   verification cases spanning the input's real variety.
4. **Characterization tests before touching warts.** Divergent duplicate code paths and
   deliberate quirks get pinned by tests *first*, then unified — or left alone.
5. **Same-PR documentation.** The lesson, the contract change, the new invariant — written
   in the same change that introduced it. Six weeks later nobody remembers why.

## 5. Operations

1. **Backup before any bulk rewrite; detach any long job.** `nohup`+logfile for multi-
   minute runs (an SSH drop mid-write once rolled back an hour of recon); scheduled DB
   dumps with retention; a targeted table dump before every regeneration.
2. **Deploys are atomic sets.** Ship *every* build asset (JS *and* CSS), verify each with
   a fetch, keep the entry HTML uncached. Half a deploy renders an unstyled app.
3. **Health is per-product.** A blended match-rate hides one product's failure inside
   another's volume. Name the worst performer; surface per-day file-presence so
   "missing file" never masquerades as "broken matching".
4. **Keep CI green or fix it loudly.** A perpetually-red pipeline trains everyone to
   ignore the one failure that matters. Environment rot (a dependency's breaking release,
   a licensed action) is a bug to fix, not background noise.

## 6. Architecture (and the CQRS question)

1. **Monolith-first is correct at this scale.** One process, one DB, one deploy unit —
   with clean *internal* layering: routes (I/O) → engines (pure-ish matching) → models.
   The failure modes that kill recon apps are data-integrity and format-drift problems,
   not throughput problems; microservices add network partitions, dual writes, and
   distributed debugging *before* they add value.
2. **Take CQRS's *idea*, not its machinery.** Separate the write model (ingestion +
   matching, single-writer, transactional) from read models (reports, analytics,
   readiness — read-only layers that never touch matching state). That is CQRS-lite and
   this codebase already practices it; formal CQRS with separate stores/buses buys
   nothing here but eventual-consistency bugs in a domain where the numbers must agree
   *now*.
3. **The audit log is your event trail.** Full event sourcing (rebuild state by replaying
   events) is powerful but heavy; an append-only audit of every mutation plus immutable
   source rows gives most of the forensic value at a fraction of the complexity.
4. **Revisit the architecture on real triggers, not fashion:** sustained multi-user write
   contention, recon windows exceeding their SLA after single-process optimization, or a
   second team needing independent deploys. Then extract the *matching engine* behind a
   job queue first — not everything at once.
