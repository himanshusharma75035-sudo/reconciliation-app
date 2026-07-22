# SBI Kiosk reconciliation — how it works + open questions

The SBI Kiosk product reconciles a CSP (kiosk banking) settlement account across **four
processes, P01–P04**. It has its own tables and engine (`backend/routes/sbi_kiosk.py`) — it
does **not** use the core matching engine. This doc is the written contract that was missing;
`docs/behavior-contract.md` item 17 covers only the delete-and-recreate coupling.

> **Status.** Phase 1 (watertight + business-date runs) and Phase 2 (readiness panel) are
> implemented. Phase 3 below — the matching-logic correctness questions — is **open and needs
> finance-ops sign-off** before any change. Do not "fix" a Phase-3 item without it.

## The five source files (per business day)

| File | Table | Feeds | Business-date column |
|---|---|---|---|
| Bank statement (settlement a/c) | `sbi_bank_transactions` | P01, P02, P03 | `txn_date` |
| Transaction report (BC txns) | `sbi_txn_reports` | P02, P03 | `txn_date` |
| KO Limits config | `sbi_ko_limits` | P01 | `txn_date` |
| KO Cash Holding | `sbi_ko_cash_holding` | P04 | `report_date` |
| Limit Failure report | `sbi_limit_failures` | P04 | `txn_date` |
| CSP Master (reference) | `sbi_csp_master` | P03 (mode lookup) | — |

**Every run is scoped by BUSINESS date** (`recon_date == txn_date`), not by upload batch, and is
**wipe-guarded**: a date with no source data is skipped, never deleted. `run_all` with no date
reconciles every business date present; auto-run after an upload reconciles that file's dates.

## The four processes (as implemented)

- **P01 — Settlement recon.** Per KO, compares *wallet withdrawals* (KO Limits, `KO Withdrawal`
  rows) against *bank settlement debits* (`is_settlement` rows) for the date. Key = KO id only.
  Status: `CREDITED` (match), `PENDING` (wallet but no bank), `EXCESS` (bank but no wallet),
  `PARTIAL` (amounts differ). Tolerance ₹0.01.
- **P02 — Bank ↔ Transaction report.** Matches each bank row's 20-digit reference against the
  transaction report's reference (first-match-wins). Same ref appearing as both DR and CR = a
  `Reversal`. Amount within **₹0.01** → `Matched`, else `Partial`; no ref/report → `Unmatched`.
  - **A reference is any 20-digit number, not just `61…`** — the leading digits encode the date
    (15 Jul = `6196…`, 20 Jul = `6201/6202…`); a `61`-only check silently mis-classifies later dates.
  - **Reversal detection ignores the `'- / -'` placeholder** (only real 20-digit refs group), else
    every no-ref cash row lumps into one bogus reversal (~977 phantom rows removed).
  - **Reversal = reconciled (Kiosk).** A reversal is a net-zero DR+CR pair (a failed txn posted
    then reversed). It stays a **distinct, visible** status but counts as **reconciled, not open**:
    the readiness rate is `(Matched+Reversal)/total`, analytics buckets it as matched, and
    `p02_reversal` is surfaced per day so a spike (e.g. 471/day) stays obvious.
- **P03 — Money out ↔ money in.** Matches transaction-report debits (money paid to CSP) against
  bank credits (money received) by **(KO code, amount)**. One-to-one within the run.
- **P04 — Wallet-balance / limit-failure.** For each limit failure, decides whether the wallet
  needs a `DEPOSIT` or `WITHDRAWAL` correction; `action_done` is toggled manually after the
  operator performs it in the SBI portal.

## The two operator output workbooks (reference-based recon)

`core/sbi_reports.py` reproduces the two files finance ops reconcile against by hand,
generated read-only from our own data (it does **not** touch the P01–P04 result tables).
The engine is the finance-ops-approved rule: match the bank statement's 20-digit
transaction number against the pooled six source files' Reference Number, one-to-one,
₹0.01 tolerance; non-20-digit / placeholder refs are tagged `Not Applicable`; unmatched
source rows split by Status (Failure = expected, Success = real gap). Download buttons sit
on the SBI page's Readiness panel, scoped to the selected business date.

**Settlement matching (finance-ops rule).** A bank *debit* that is an `EKO DEDUCTION`
(`is_settlement`, no 20-digit ref) is NOT a reference transaction — it reconciles against a
**KO-Limits "KO Withdrawal"** by (business date, KO id, amount within ₹0.01), one-to-one.
Matched → `Matched (Settlement)`; a settlement debit with no matching withdrawal →
`Unmatched Settlement` (surfaced in Unmatched Bank Entries for review — genuine amount
mismatch or a missing KO-Limits upload). After this pass, `No Txn Number` is only the
genuine cash / bank-only rows — essentially the still-open **credit** side (validated on
21-Jul: 154 settlement debits → 125 matched / 29 review, leaving 32 open credits, 0 debits).

- **`GET /api/sbi/reports/reconciliation`** → `Reconciliation_Report_<date>.xlsx` (bank-centric,
  5 sheets): Summary · Bank Statement (Reconciled, with the **Matched Source File** product
  per row) · Unmatched Bank Entries · Unmatched Source Records (Success first) · Duplicate Txn.
- **`GET /api/sbi/reports/source-match`** → `Source_Files_Match_Status_<date>.xlsx` (source-centric,
  8 sheets): Summary (split by Success/Failure) · Limit & Settlement · one sheet per source
  product with each row's match status highlighted.

Validated against the manual cloud reports for 15 Jul 2026: **all six products, the bank
totals, and the Unmatched-Success count (37) match exactly**; product attribution agrees on
13,773/13,777 bank rows (99.97%) — the only 4 differing are reversal legs, correctly tagged.
A defensive exact-duplicate dedup at read time keeps a double-uploaded file (e.g. AePS Onus
Deposit, seen doubled on 15 Jul) from flooding the report with phantom Unmatched-Success rows.

## Why a low match rate is usually NOT a bug

P02 can only match a day that has **both** a bank statement and a transaction report. When the
report file isn't uploaded, every bank row that day is `Unmatched` — a *missing file*, not a
recon failure. The **Readiness panel** on the SBI page shows, per date, which files are present
and the real match rate, so this is visible at a glance (days with complete files match ~98–99%).

---

## Phase 3 — open correctness questions (need finance-ops / SOP sign-off)

Each of these changes how money reconciles, so none should be changed without confirmation.

1. **P02 vs P03 double-count.** A non-settlement bank *credit* is currently reconciled
   independently by P02 (by reference) **and** P03 (by KO + amount), with no mutual exclusion —
   so the same credit is counted as reconciled in both. **Is that intended (two independent
   checks), or should a credit be reconciled once?**
2. **P03 match key — CONFIRMED gap, highest-priority.** P03 matches on **KO code + amount only**
   and ignores the reference number. An audit of the live unmatched pool found **~2,244 money-out
   transactions (~₹74 lakh) that are wrongly left unmatched**: the bank statement carries the
   KO/CSP id **truncated to 7 characters** while the transaction report carries the **full 8–10
   character** id, so `(KO, amount)` never matches even though the **reference number and amount
   agree exactly**. Independently verified four ways: 2,244/2,244 are exact 7-char prefixes;
   2,244/2,244 are a clean 1:1 on `(date, reference)`; and **2,244/2,244 (100%) already match in
   P02 by reference** — proving each is a single real transaction P03 split in two.
   - **Safe fix: add reference-number matching to P03** (as P02 already does), excluding the
     placeholder ref `'- / -'` (the only source of non-1:1 rows).
   - **Do NOT "normalize"/truncate the KO id to fix this.** It is unsafe: **3,383 collision groups
     (16,204 rows)** have different full KO ids sharing the same 7-char prefix + day + amount, so a
     prefix-normalizing match would **misattribute money between different KOs**. Reference is the
     only collision-free key.
   - Use the SBI tolerance (₹0.01), not the ₹1 used during the audit.
   - **Implementation finding (2026-07-22) — the fix is real but incomplete in P03 alone, so it
     was TRIED AND REVERTED on the live engine.** P03 only matches against bank **credits**
     (money-in). Switching its key to reference is *more precise* — but it then also removed
     ~1,760 matches/day that the old `(ko,amount)` key had made **falsely**: money-IN txns
     (Money Transfer, all Deposit types) matched to unrelated credits by coincidence. Those
     txns' real counterpart is a bank **debit**, which P03's credit-only scope never sees, so
     they read as Unmatched. Net effect on the live P03 was matched **34,321 → 26,619** — the
     *opposite* of the audit's expected +2,244. Completing it correctly = matching txns against
     **all** non-settlement bank rows (debits for deposits, credits for withdrawals), which is
     the **P02/P03 merge** (Still-open item 1) and needs finance-ops confirmation. **Until then,
     the fully-correct reference reconciliation is the report layer** (`core/sbi_reports.py`,
     verified against the manual cloud reports), and live P03 keeps its `(ko,amount)` behaviour.
   - The **P02 tolerance ₹1 → ₹0.01** part of this item *was* shipped (safe: 0 reclassification
     across the live data).
3. **P03 date window.** The code accepted ±2 days (the docstring/SOP says D+1 / D-1 = ±1); Phase
   1 reduced P03 to **same-day only** to avoid a bank credit being reused across two days' runs.
   **What is the correct window (same-day only, ±1, ±2), and how should a D+1 credit be
   attributed so it isn't counted twice?**
4. **P04 formula.** The current formula reduces algebraically to `action = -failed_amount`, so
   the **KO Cash Holding closing balance never affects the result** — the whole reason that file
   is uploaded. **What is P04 supposed to compute?** (e.g. compare closing balance against a
   target/limit?)
5. **P01 debit vs credit.** P01 sums bank settlement **debits** as "bank settled", but the
   docstring says settlement **credits**. **Which is correct for this account?**
6. **Tolerances.** P01/P04 use ₹0.01, P02 uses ₹1, P03 is exact (zero tolerance). The behavior
   contract lists ₹0.01 as *the* SBI tolerance. **What tolerance should each process use?**
7. **Cross-day settlement (P01).** A wallet deduction on day D may settle on day D+1's bank
   statement. P01 currently matches same-day per KO. **Should P01 look across days (D+1)?**
