"""
Same-side matching rules (MatchRule.scope ∈ {bank_bank, internal_internal}).
Net-zero contra semantics, finance-ops decision 2026-07-27 (Rajendra).

Invariants:
1. A bank_bank rule pairs an opposite DR+CR on the BANK side sharing the rule's key
   (amount ±₹1) → both reversal_matched, shared match_id. Same for internal_internal
   → internal_matched.
2. Default rules (no scope / bank_internal) drive ONLY the cross loop — behaviour is
   byte-identical to before (guarded by the existing characterization suite too).
3. A row never pairs with itself; each row is used once.
4. Same-direction rows (both DR or both CR) are NOT paired (net-zero only).
5. Guard: a key still held by an OPEN opposite-side row (any date) is NOT netted
   same-side (leave it for a genuine cross match).
6. Same-side pairs stay OUT of the bank-vs-internal 'matched' total.
"""
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Transaction, MatchRule, ReconStatus, generate_id
from core.matching_engine import run_reconciliation

DATE = "2026-07-23"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def mk(db, side, partner="axis", recon_date=DATE, row_type="txn",
       recon_status=ReconStatus.unmatched, **kw):
    t = Transaction(id=generate_id(), partner=partner, side=side, recon_date=recon_date,
                    row_type=row_type, recon_status=recon_status, **kw)
    db.add(t)
    return t


def rule(db, partner="axis", name="r", fields=("tracking_number",), priority=1,
         scope="bank_bank"):
    db.add(MatchRule(id=generate_id(), name=name, partner=partner, priority=priority,
                     match_fields=json.dumps(list(fields)), is_active=True, scope=scope))


def anchor(db, side, recon_date=DATE):
    """A distinct-key opposite-side row so Guard A is satisfied without tripping Guard B."""
    return mk(db, side, tracking_number="ANCHORKEY", amount=1.0, dr_cr="DR",
              recon_date=recon_date, recon_status=ReconStatus.internal_matched)


def test_bank_bank_rule_nets_opposite_dr_cr(db):
    rule(db, scope="bank_bank", fields=("tracking_number",))
    anchor(db, "internal")                # opposite side present (Guard A)
    dr = mk(db, "bank", tracking_number="T1", amount=9400.0, dr_cr="DR")
    cr = mk(db, "bank", tracking_number="T1", amount=9400.0, dr_cr="CR",
            row_type="settlement_credit")
    db.commit()
    out = run_reconciliation("axis", DATE, db, "u1")
    assert out["same_side_matched"] == 1
    assert out["matched"] == 0            # NOT counted as a cross match
    db.refresh(dr); db.refresh(cr)
    assert dr.recon_status == ReconStatus.reversal_matched
    assert cr.recon_status == ReconStatus.reversal_matched
    assert dr.match_id == cr.match_id and dr.match_id


def test_internal_internal_rule_writes_internal_matched(db):
    rule(db, scope="internal_internal", fields=("tracking_number",))
    anchor(db, "bank")                    # opposite side present (Guard A)
    d = mk(db, "internal", tracking_number="X9", amount=500.0, dr_cr="DR")
    c = mk(db, "internal", tracking_number="X9", amount=500.0, dr_cr="CR")
    db.commit()
    out = run_reconciliation("axis", DATE, db, "u1")
    assert out["same_side_matched"] == 1
    db.refresh(d); db.refresh(c)
    assert d.recon_status == ReconStatus.internal_matched
    assert c.recon_status == ReconStatus.internal_matched


def test_same_direction_not_paired(db):
    rule(db, scope="bank_bank", fields=("tracking_number",))
    anchor(db, "internal")
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    db.commit()
    assert run_reconciliation("axis", DATE, db, "u1")["same_side_matched"] == 0


def test_guard_b_cross_date_open_internal_not_stolen(db):
    # Guard A satisfied by a same-date anchor; a genuine internal row on ANOTHER date
    # carries the same tracking (open) — the bank round trip must NOT be netted, it
    # belongs to that cross-date internal leg.
    rule(db, scope="bank_bank", fields=("tracking_number",))
    anchor(db, "internal")                                    # same-date, distinct key
    mk(db, "internal", tracking_number="T1", amount=100.0, dr_cr="DR",
       recon_date="2026-07-22", status="Success")            # cross-date, same key, OPEN
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="CR")
    db.commit()
    out = run_reconciliation("axis", DATE, db, "u1")
    assert out["same_side_matched"] == 0


def test_guard_b_composite_key_protected(db):
    # composite key [eko_tid, tracking_number]: a cross-date OPEN internal row with the
    # same composite must block same-side netting (regression: a bare-tracking guard set
    # missed composite keys).
    rule(db, scope="bank_bank", fields=("eko_tid", "tracking_number"))
    anchor(db, "internal")
    mk(db, "internal", eko_tid="E1", tracking_number="T1", amount=100.0, dr_cr="DR",
       recon_date="2026-07-22")
    mk(db, "bank", eko_tid="E1", tracking_number="T1", amount=100.0, dr_cr="DR")
    mk(db, "bank", eko_tid="E1", tracking_number="T1", amount=100.0, dr_cr="CR")
    db.commit()
    assert run_reconciliation("axis", DATE, db, "u1")["same_side_matched"] == 0


def test_default_rules_still_cross_only(db):
    # bank_internal rule → normal cross match, no same-side effect
    rule(db, scope="bank_internal", fields=("tracking_number",))
    b = mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    i = mk(db, "internal", tracking_number="T1", amount=100.0, dr_cr="CR")
    db.commit()
    out = run_reconciliation("axis", DATE, db, "u1")
    assert out["matched"] == 1
    assert out["same_side_matched"] == 0
    db.refresh(b); db.refresh(i)
    assert b.recon_status == ReconStatus.matched


def test_same_side_only_rule_does_not_disable_cross(db):
    # A partner with ONLY a same-side rule persisted must still get its default cross
    # rules (else adding a bank_bank rule would silently kill bank↔internal matching).
    from core.matching_engine import get_rules_for_partner
    rule(db, partner="axis", name="bb-only", scope="bank_bank", fields=("tracking_number",))
    db.commit()
    out = get_rules_for_partner("axis", db)
    scopes = {r["scope"] for r in out}
    assert "bank_bank" in scopes
    assert "bank_internal" in scopes   # merged back from DEFAULT_RULES["axis"]


def test_guard_a_defers_when_opposite_side_absent(db):
    # bank_bank rule, but NO internal side exists for the date → net nothing (defer)
    rule(db, scope="bank_bank", fields=("tracking_number",))
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="CR")
    db.commit()
    assert run_reconciliation("axis", DATE, db, "u1")["same_side_matched"] == 0
    # once an internal row for the date lands, the same call nets it
    mk(db, "internal", tracking_number="ZZZ", amount=1.0, dr_cr="DR"); db.commit()
    assert run_reconciliation("axis", DATE, db, "u1")["same_side_matched"] == 1


def test_cross_wins_before_same_side(db):
    # both a cross rule and a bank_bank rule exist; the bank DR has a genuine internal
    # counterpart → cross must claim it, leaving the bank CR unpaired (no open opposite
    # key remains for it, but its DR partner is gone).
    rule(db, name="cross", scope="bank_internal", fields=("tracking_number",), priority=1)
    rule(db, name="bb", scope="bank_bank", fields=("tracking_number",), priority=2)
    bdr = mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="DR")
    mk(db, "bank", tracking_number="T1", amount=100.0, dr_cr="CR")
    idr = mk(db, "internal", tracking_number="T1", amount=100.0, dr_cr="CR")
    db.commit()
    out = run_reconciliation("axis", DATE, db, "u1")
    assert out["matched"] == 1                 # bank DR ↔ internal
    db.refresh(bdr); db.refresh(idr)
    assert bdr.recon_status == ReconStatus.matched
    assert bdr.matched_with_id == idr.id
