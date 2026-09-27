from datetime import date
from decimal import Decimal

import pytest

from src.core.text import (
    classify_registration, expand_abbreviations, fmt_inr, keyword_hits, match_names, parse_date_lenient, parse_money,
)


def test_keyword_whole_phrase_boundaries():
    assert keyword_hits("Suspected Lumbar Disc Herniation", ["hernia"]) == []
    assert keyword_hits("Inguinal hernia, left", ["hernia"]) == ["hernia"]
    assert keyword_hits("Dental X-Ray (IOPA)", ["x-ray"]) == ["x-ray"]
    assert keyword_hits("CBC Test", ["ct"]) == []


def test_abbreviation_expansion():
    assert expand_abbreviations("HTN, on treatment", {"htn": "hypertension"}) == "HTN (hypertension), on treatment"


@pytest.mark.parametrize("a,b,level", [
    ("Rajesh Kumar", "Rajesh Kumar", "EXACT"),
    ("Mr. Rajesh Kumar", "rajesh  kumar", "EXACT"),
    ("Rajesh Kumaar", "Rajesh Kumar", "FUZZY"),
    ("R. Kumar", "Rajesh Kumar", "INITIALS"),
    ("Priya", "Priya Singh", "PARTIAL"),
    ("Arjun Kumar", "Rajesh Kumar", "MISMATCH"),   # shared surname is NOT a match (v1 bug)
    ("Arjun Mehta", "Rajesh Kumar", "MISMATCH"),
    (None, "Rajesh Kumar", "UNKNOWN"),
])
def test_name_matching(a, b, level):
    assert match_names(a, b).level == level


def test_parse_money_formats():
    assert parse_money("₹1,000") == Decimal("1000")
    assert parse_money("Rs. 1,50,000.50/-") == Decimal("150000.50")
    assert parse_money(1500) == Decimal(1500)
    assert parse_money(None) is None
    with pytest.raises(ValueError):
        parse_money("abc")


def test_lenient_document_dates():
    assert parse_date_lenient("01-Nov-2024") == date(2024, 11, 1)
    assert parse_date_lenient("01/11/2024") == date(2024, 11, 1)
    assert parse_date_lenient("not a date") is None


def test_indian_currency_format():
    assert fmt_inr(Decimal("150000")) == "₹1,50,000"
    assert fmt_inr(Decimal("3240.00")) == "₹3,240"
    assert fmt_inr(Decimal("73.62")) == "₹73.62"


def test_registration_formats(interp):
    assert classify_registration("KA/45678/2015", interp.registration_patterns) == (True, "STATE_MEDICAL_COUNCIL")
    assert classify_registration("AYUR/KL/2345/2019", interp.registration_patterns) == (True, "AYUSH")
    assert classify_registration("12345", interp.registration_patterns)[0] is False


CUES = ["no", "denies", "negative for", "family history of", "r/o", "rule out", "without"]


@pytest.mark.parametrize("text,hits,negated", [
    ("No family history of diabetes", [], ["diabetes"]),
    ("Type 2 Diabetes Mellitus; no hypertension", ["diabetes"], ["hypertension"]),
    ("Diabetes, no complications", ["diabetes"], []),            # cue AFTER the mention does not negate
    ("No fever. Diabetes on metformin", ["diabetes"], []),        # sentence boundary stops the cue
    ("Denies chest pain, hypertension", [], ["hypertension"]),     # commas do not stop it
    ("R/O hernia; umbilical hernia confirmed", ["hernia"], []),   # any affirmed occurrence counts
])
def test_negation_aware_mentions(text, hits, negated):
    from src.core.text import find_mentions
    result = find_mentions(text, ["diabetes", "hypertension", "hernia"], CUES)
    assert result.hits == hits and sorted(result.negated) == sorted(negated)


def test_plural_forms_match_without_loosening_boundaries():
    assert keyword_hits("Calcium supplements", ["supplement"]) == ["supplement"]
    assert keyword_hits("Two veneers", ["veneer"]) == ["veneer"]
    assert keyword_hits("Lumbar Disc Herniation", ["hernia"]) == []


def test_abbreviations_expand_in_place_so_negation_still_applies():
    from src.core.text import find_mentions
    text = expand_abbreviations("No HTN. T2DM", {"htn": "hypertension", "t2dm": "type 2 diabetes"})
    assert text == "No HTN (hypertension). T2DM (type 2 diabetes)"
    result = find_mentions(text, ["hypertension", "diabetes"], CUES)
    assert result.hits == ["diabetes"] and "hypertension" in result.negated
