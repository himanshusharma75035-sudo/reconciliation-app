"""
Guard tests for POST /workflow/rule-suggestions-ai/accept (accept_ai_rule).

The adversarial review found this endpoint trusted the client-supplied partner
verbatim — so partner="all" would inject a GLOBAL active MatchRule that reclassifies
every partner's money, and non-canonical casing created silently dead rules. These
tests pin the fix: bind to a real, specific, lowercased partner; reject "all"; keep
the field-vocabulary + amount-only guards.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from models.database import Base, MatchRule, PartnerConfig, generate_id
from routes.workflow import accept_ai_rule, AiRuleAccept


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    s.add(PartnerConfig(id=generate_id(), slug="fino", display_name="Fino", match_prefix="FINO"))
    s.commit()
    yield s
    s.close()


USER = SimpleNamespace(id="u1", username="tester", role="user")


def _accept(db, partner, fields, name=None):
    return accept_ai_rule(AiRuleAccept(partner=partner, fields=fields, name=name), db=db, user=USER)


def test_rejects_global_all_scope(db):
    with pytest.raises(HTTPException) as e:
        _accept(db, "all", ["utr_number"])
    assert e.value.status_code == 400
    # no rule was created
    assert db.query(MatchRule).count() == 0


def test_rejects_unknown_partner(db):
    with pytest.raises(HTTPException) as e:
        _accept(db, "ghostbank", ["utr_number"])
    assert e.value.status_code == 404
    assert db.query(MatchRule).count() == 0


def test_lowercases_partner_to_canonical_slug(db):
    out = _accept(db, "FINO", ["utr_number", "eko_tid"])
    rule = db.query(MatchRule).one()
    assert rule.partner == "fino"                       # canonical, matcher will load it
    assert set(json.loads(rule.match_fields)) == {"utr_number", "eko_tid"}
    assert rule.is_active is True
    assert out["partner"] == "fino"


def test_rejects_amount_only(db):
    with pytest.raises(HTTPException) as e:
        _accept(db, "fino", ["amount"])
    assert e.value.status_code == 400
    assert db.query(MatchRule).count() == 0


def test_strips_disallowed_fields_then_rejects_if_empty(db):
    # bank_account is not an allowed match field → filtered out → nothing left → 400
    with pytest.raises(HTTPException) as e:
        _accept(db, "fino", ["bank_account"])
    assert e.value.status_code == 400
    assert db.query(MatchRule).count() == 0


def test_valid_accept_creates_rule_at_lowest_priority(db):
    db.add(MatchRule(id=generate_id(), name="existing", partner="fino", priority=5,
                     match_fields=json.dumps(["eko_tid"]), is_active=True))
    db.commit()
    _accept(db, "fino", ["utr_number"])
    new = db.query(MatchRule).filter(MatchRule.match_fields == json.dumps(["utr_number"])).one()
    assert new.priority == 6   # max(existing)+1 → appended at lowest priority
