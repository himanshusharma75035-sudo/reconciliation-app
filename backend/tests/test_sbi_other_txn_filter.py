"""
Point-4 (Rajendra): the SBI Kiosk "Other Transactions" ingest must keep a row
only when its From OR To account is our SBI settlement account; a customer↔customer
ATM fund-transfer must be skipped. The rule applies to that file ONLY and fails
OPEN when the settlement account is unconfigured (so a mis-config never drops
every row). These pin `_keep_txn_report_row` — the pure decision used in ingest.
"""
from routes.sbi_kiosk import _keep_txn_report_row

SETTLE = ["71556"]   # visible suffix of the settlement account (as it appears masked in-file)


def test_other_txn_kept_when_to_account_is_settlement():
    # row 1 in Rajendra's screenshot: To = XXXXXX71556 → keep
    assert _keep_txn_report_row("Other Transactions", "XXXXXX21052", "XXXXXX71556", SETTLE) is True


def test_other_txn_kept_when_from_account_is_settlement():
    assert _keep_txn_report_row("Other Transactions", "XXXXXX71556", "XXXXXX21052", SETTLE) is True


def test_other_txn_skipped_when_neither_account_is_settlement():
    # row 89: ATM FUNDSTRANSFER between two customer accounts → skip
    assert _keep_txn_report_row("Other Transactions", "XXXXXX78571", "XXXXXX75384", SETTLE) is False


def test_filter_applies_only_to_other_transactions():
    # the SAME non-settlement accounts in a different file must NOT be filtered
    assert _keep_txn_report_row("Money Transfer", "XXXXXX78571", "XXXXXX75384", SETTLE) is True
    assert _keep_txn_report_row("AEPS Withdrawal Transaction Report", "XXXXXX78571", "XXXXXX75384", SETTLE) is True


def test_fail_open_when_unconfigured():
    # no settlement account configured → keep every Other-Transactions row
    assert _keep_txn_report_row("Other Transactions", "XXXXXX78571", "XXXXXX75384", []) is True


def test_empty_accounts_skipped_when_configured():
    # a row with no account values can't be ours → skipped under the filter
    assert _keep_txn_report_row("Other Transactions", "", "", SETTLE) is False


def test_multiple_settlement_accounts_any_match():
    assert _keep_txn_report_row("Other Transactions", "XXXXXX99999", "XXXXXX88888",
                                ["71556", "88888"]) is True
