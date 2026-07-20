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
  `Reversal`. Amount within ₹1 → `Matched`, else `Partial`; no ref/report → `Unmatched`.
- **P03 — Money out ↔ money in.** Matches transaction-report debits (money paid to CSP) against
  bank credits (money received) by **(KO code, amount)**. One-to-one within the run.
- **P04 — Wallet-balance / limit-failure.** For each limit failure, decides whether the wallet
  needs a `DEPOSIT` or `WITHDRAWAL` correction; `action_done` is toggled manually after the
  operator performs it in the SBI portal.

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
2. **P03 match key.** P03 matches on **KO code + amount only** and ignores the reference number.
   Two different transactions with the same KO and amount can be mis-paired. **Should P03 also
   key on the reference number?**
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
