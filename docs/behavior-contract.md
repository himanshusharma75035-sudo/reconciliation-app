# Behavior-preservation contract

This app reconciles real money. The behaviors below are **load-bearing**: changing any of
them silently reclassifies live transactions, breaks audit references, or double-counts
funds. PRs that touch them must say so explicitly and carry finance/ops sign-off.

Reviewers: cite this file by item number.

> Several of these are now pinned by a characterization test suite
> (`backend/tests/test_matching_engine_characterization.py`,
> `test_ingestion_characterization.py`, `test_ingest_pipeline_characterization.py`) —
> notably items 1, 2, 3, 5, 6, 7, 8, 10, and 18. Run `pytest tests/ -q` before **and** after
> any change near them; a diff in those tests means you changed load-bearing behavior.

1. **Match-ID scheme & sequencing** — `{PREFIX}-{YYYYMMDD}-{NNNN}` format, MAX+1 (never
   COUNT) semantics of `_next_seq` (`core/matching_engine.py`), the startup duplicate
   repair and per-series backfill counters (`main.py`). These are mutually dependent;
   existing IDs are audit references. Known prefix/sequence quirks must not be "fixed"
   without a data migration plan.
2. **`_parse_description` regex ladder order** (`routes/upload.py`) — reversal-strip →
   `REV IMPS-` → IMPS two-segment → `Charges/` → IMPS single → NEFT/RTGS → generic → bare
   digits. Digit-length bounds decide tracking vs TID vs UTR. Reordering reassigns
   extracted identifiers for live bank formats.
3. **`_classify_bank_row` precedence** (`routes/upload.py`) — reversal detection MUST run
   before fee-charge detection (a reversed fee would otherwise auto-close as
   `fee_matched`). The DR/CR heuristics decide which rows enter recon at all.
4. **Post-ingest pass order** — reversal → auto-recon (counterpart-gated) → NEFT D+1 →
   internal self-match → same-side duplicate flagging. `/clear` cascades counterpart
   resets then re-runs recon. Order changes silently change final ledger statuses. The
   duplicate-flag pass (`flag_same_side_duplicates`) runs LAST, in both ingest copies,
   and only re-labels still-`unmatched` `txn`-row re-ingestions (same partner/side/
   tracking) as `duplicate` — never a matched, operator-actioned, or reversal/fee row.
   *(2026-07-23)* The cascade is now universal: **every** delete path that removes one
   leg of a matched pair reverts every surviving leg sharing its match_id — module
   clears (E-Value/BBPS, full paired-status set incl. wrong_amount/amount_mismatch),
   the E-Value/BBPS per-id upserts, the BBPS provider wipe, cross-product EVIBT- links
   (both directions), and `_repair_orphaned_matches` heals all paired statuses, not
   just `matched`. An SBI source-side clear also removes the same scope's P01–P04
   result rows (recycle-binned) — results must never outlive their sources.
   *(2026-07-23)* Upload semantics: every upload is **replace/upsert within its own
   scope, never append** — SBI ko-limits (its dates × configurer), txn-report (its
   dates × product), cash-holding (report_date × its KOs), limit-failures (its dates ×
   BC), csp-master (content dedup), QR settlement (upsert per Settlement ID) — so the
   Re-upload (replace)/force path is idempotent everywhere it is offered.
5. **`_normalize` float canonicalization** (`core/matching_engine.py`) — `700.0 → '700'`,
   else 2-decimal string. Amount-keyed rules (PG, Digikhata) depend on this exact string.
   *(2026-07-27)* `MatchRule.scope` (default `bank_internal`) selects the side-pairing:
   `bank_internal` feeds the unchanged cross loop; `bank_bank` / `internal_internal` are
   applied by `_match_same_side_rules` AFTER the cross loop (net-zero contra: opposite
   DR/CR, ±₹1, self-match-guarded, Guard A defer-until-opposite-side-exists + Guard B
   don't-steal-an-open-opposite-key) and write `reversal_matched` / `internal_matched` so
   same-side pairs never enter the bank-vs-internal `matched` totals. Every legacy rule is
   `bank_internal`; `get_rules_for_partner` merges the default cross rules back when a
   partner has NO persisted cross rule so a same-side rule can't disable cross matching.
6. **`_build_key` returns None when ANY field is empty** — this is what lets fallback
   rules fire. Tolerating empties = over-matching.
7. **Tolerances differ by engine on purpose** — ₹1 core/BBPS, exact E-Value, 0.01 SBI,
   0.02 AePS, 0.05 QR. Equalizing them reclassifies money.
8. **NEFT D+1 matches on UTR only**, despite a docstring that says "UTR + amount". Fix the
   docstring if you must; never the code without sign-off.
9. **`FAILED_STATUSES` / open-vs-matched status sets are duplicated** across
   `upload.py`, `ingest_service.py`, `matching_engine.py`, `notifications.py`,
   `report_scheduler.py`, `recon.py`, `insights.py` — they must stay in lockstep, and the
   literal `'0'` counts as failed.
10. **Fino `ACCOUNT_ACTION_ID == 118` drop** exists in BOTH ingest copies
    (`upload.py` and `ingest_service.py`), including the `.split('.')` float-string parse.
    A compliance rule enforced only at ingest.
11. **Duplicate-upload protections** — the 409 slot hard-block (admin `force` only when
    `recon_date != 'auto'`) and the SHA-256 `file_hash_guard` (which deliberately does NOT
    commit — it rides the caller's transaction). Module clears wipe hashes so re-upload
    works after a clear.
12. **Timezone pact** — backend stores naive UTC (`datetime.utcnow`); the frontend axios
    interceptor converts datetime-shaped strings to IST; audit Excel converts server-side.
    Making timestamps tz-aware or ISO+Z double-shifts every displayed time.
13. **E-Value replace semantics** — a bank-statement upload DELETEs all prior bank rows for
    that account (including manual work); internal dumps upsert per `eko_trxn_id`;
    `reco_acc_no` must exactly equal the dump's `source` label **including deliberate
    quirks** (BOI-0351/0352 swap, PNB suffix mismatches); the cross-account reference pass
    runs globally after every per-account recon; fuzzy UTR/reference regexes and the cash
    `score >= 1` threshold are tuned to real narrations. **The internal (wallet-load) side's
    reconciliation date IS the load's VALUE date, not its transaction date** — the bank credits
    an E-Value load on its value date, so value date is compared against the bank's single
    statement date, falling back to the transaction date only when value date is blank. This is
    the load's *effective recon date* everywhere: the match comparison (`load_date()` in
    `evalue_engine.py` + the cross-account pass in `evalue.py`), the date **filter / ordering /
    display / ageing** in the E-Value window and reports (`_LOAD_DATE` SQL expr + the
    `EvalueWalletLoad.recon_date_effective` property), the analytics internal-side grouping
    (`analytics.py`), and the Open-Items adapter (`recon.py`). `transaction_date` stays stored
    and is still shown alongside (a separate column / tooltip). (finance ops / Rajendra,
    2026-07-15; scope extended from match-only to all data views 2026-07-16.)
14. **Open-Items contracts** — default status filter is unmatched+src_assigned; the
    `_skip_row_type` gate; `match_id` lookups bypass status & row-type gates; the literal
    `'all'` recon_status is accepted; `dmt` fans out to the DMT partners; "All partners"
    pagination stitches SQL offsets with in-memory module rows.
15. **Startup seeds mutate live data** — display-name renames keyed on exact old strings,
    Levin `has_bank_statement` flip, Levin "UTR Only" rule backfill. Editing those strings
    re-triggers or skips migrations on deployed databases.
16. **Maker-checker** — intercept placement differs per route (before validation in some,
    after remark validation in others); approval replays stored payloads through the
    CURRENT Pydantic models (queued rows freeze request schemas); the dual response shape
    (`{queued: true}` vs applied result) is parsed by the frontend; self-approval guard
    compares usernames.
17. **SBI couplings** — P01–P04 only see rows where `upload_date == today`; the bank file
    is tab-separated text named `.xls` with fixed header tokens (including typo'd source
    columns); P03 runs with `max_shift=2`; results are delete-and-recreate.
18. **Reversal pairing** — `zip()` deliberately drops surplus rows; originals are searched
    across ALL dates; the fee_reversal/fee_charge split exists because IMPS fee rows share
    the original's tracking number.
19. **Three divergent ageing-bucket definitions** (core SQL, module adapter, Excel export)
    — unifying them changes what users see vs what they download.
20. **Carry-forward** writes plain `matched` status; the only markers are
    `src_note='carry-forward D+N'` and the `CFW-` match ID.
21. **Counting semantics** — `row_count` counts only `row_type=='txn'`;
    settlement_credit rows are double-counted in credit and fund counts; the EOD email
    counts the bank side only; pair views count `side=='bank'` rows only.
22. **Magic literals** — partner `'mixed'`; recon_date `'auto'` / `'auto (multi-date)'`;
    usernames `'system'` / `'auto-upload'` / `'openclaw-bot'` (drive audit-source
    classification); error prefixes `[WLR]` / `[FREC]` (string-matched by the frontend).
23. **Data-type pacts** — `MONEY = Numeric(15,2, asdecimal=False)` (floats on purpose);
    business dates are zero-padded strings compared lexicographically; transaction match
    keys deliberately have NO unique constraint; String(20) PKs hold 36-char UUIDs on
    SQLite.
24. **`_extract_bank_account` heuristics** — 'Account No - ' in the first 25 lines, else
    9–18 leading filename digits, with auto-registration. Loosening the digit guard
    creates bogus accounts from date-prefixed filenames.
25. **External contracts** — Excel column headers and sheet names; date alias precedence
    (`date_from` wins over `from_date`); `/recon/run` response shape; jobs status dict
    shape; upload Step-3 result field names (`fee_charge_count`, `auto_recon`,
    `integrity_warnings`, `sev_notice`); `getattr` schema-drift shims; the
    unauthenticated `GET /api/admin/partners-public` (used by the UI pre-login).
