import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import date
import audit_engine as ae
import rules
from llm_client import extract_json

SAMPLE = (pathlib.Path(__file__).resolve().parent.parent / "samples" / "sample_contract.txt").read_text()


def rule_ids(text, jur="dubai"):
    return {r.id for r in rules.match_rules(text, jur)}


def test_slabs():
    assert ae.rera_slabs_max_increase(5) == 0
    assert ae.rera_slabs_max_increase(15) == 5
    assert ae.rera_slabs_max_increase(25) == 10
    assert ae.rera_slabs_max_increase(35) == 15
    assert ae.rera_slabs_max_increase(45) == 20


def test_rules_fire():
    assert "EVICT-NO-NOTICE" in rule_ids("The Landlord may evict the Tenant at any time without notice for any reason.")
    assert "RENT-DISCRETION" in rule_ids("The Landlord may increase the rent at any time at the Landlord's sole discretion.")
    assert "DEPOSIT-FORFEIT" in rule_ids("The security deposit is non-refundable and shall be forfeited.")
    assert "PARTITIONS" in rule_ids("Tenant may install partitions and sub-divide rooms.")
    assert "VAT-ON-RENT" in rule_ids("The rent is exclusive of 5% VAT payable by the tenant.")
    assert "EVICT-SALE-SHORT" in rule_ids("The Tenant must vacate within 15 days if the Landlord decides to sell the property.")
    assert "PENALTY-EXCESSIVE" in rule_ids("In case of late payment the Tenant shall pay a penalty of 25% of the annual rent.")
    assert "FOREIGN-JURISDICTION" in rule_ids("This contract is governed by the laws of England and the courts of London.")


def test_rules_do_not_overfire():
    ok = "The Landlord shall hand over the unit in good condition and the Tenant shall use it for residential purposes only."
    assert not rule_ids(ok)
    assert "EVICT-SALE-SHORT" not in rule_ids("The Landlord may require vacant possession for sale on 12 months' notice by notary public.")
    assert "EVICT-DEFAULT-SHORT" not in rule_ids("The Landlord may terminate for non-payment after a 30 days notice served by registered mail.")
    assert "EVICT-DEFAULT-SHORT" in rule_ids("The Landlord may terminate for non-payment after a 7 days notice.")


def test_parse_and_audit_sample():
    e = ae.parse_ejari_text(SAMPLE)
    assert e.current_annual_rent_aed == 110000
    assert e.security_deposit_aed == 11000
    assert e.bedrooms == 2
    assert e.start_date == date(2025, 3, 1) and e.end_date == date(2026, 2, 28)
    assert e.ejari_number == "1234567890"
    assert e.jurisdiction == "dubai"
    res = ae.run_audit(SAMPLE, e)
    assert res.verdict == "fail"
    assert sum(c.verdict == "fail" for c in res.clause_findings) >= 4
    clean = [c for c in res.clause_findings if "hand over the unit" in c.text]
    assert clean and clean[0].verdict == "pass"


def test_no_index_is_unknown_not_fail():
    e = ae.EjariFields(current_annual_rent_aed=100000, proposed_new_rent_aed=105000)
    checks, pct, mx = ae.run_numeric_checks(e, None)
    cap = next(c for c in checks if c.id == "RENT-CAP")
    assert cap.status == "unknown" and mx is None and pct == 5.0


def test_rent_cap_and_notice():
    e = ae.EjariFields(current_annual_rent_aed=80000, proposed_new_rent_aed=90000,
                       renewal_date=date(2026, 6, 1), notice_sent_date=date(2026, 4, 20))
    checks, _, mx = ae.run_numeric_checks(e, 100000)      # 20% below index -> 5% cap
    assert mx == 5.0
    st = {c.id: c.status for c in checks}
    assert st["RENT-CAP"] == "fail" and st["NOTICE-90"] == "fail"
    e.proposed_new_rent_aed = 84000
    e.notice_sent_date = date(2026, 2, 1)
    st = {c.id: c.status for c in ae.run_numeric_checks(e, 100000)[0]}
    assert st["RENT-CAP"] == "pass" and st["NOTICE-90"] == "pass"


def test_missing_dates_not_invented():
    assert ae.to_date("") is None and ae.to_date("garbage") is None
    assert ae.check_notice_window(None, None)[0] == "unknown"


def test_segmenter():
    cl = ae.segment_clauses("1. First clause is long enough to keep.\ncontinued here.\n2. Second clause is also long enough.")
    assert len(cl) == 2 and "continued here" in cl[0]


class FakeLLM:
    model = "fake"
    def chat(self, messages, json_output=False, max_tokens=4096):
        if json_output:
            return '```json\n{"clauses":[{"id":1,"verdict":"non_compliant","reason":"Bad.","law_ids":["DXB-26-2007","MADE-UP"],"suggestion":"Fix"}]}\n```'
        return "Summary."


def test_ai_merge_drops_fake_citations():
    text = "1. The tenant shall paint the walls white before handing back the unit at the end."
    e = ae.EjariFields()
    res = ae.run_audit(text, e, llm=FakeLLM())
    c = res.clause_findings[0]
    assert res.ai_used and c.verdict == "fail" and c.source == "ai"
    assert "MADE-UP" not in c.laws and "DXB-26-2007" in c.laws


def test_extract_json_variants():
    assert extract_json('<think>x</think>{"a":1}') == {"a": 1}
    assert extract_json('Here: {"a": [1,2]} done') == {"a": [1, 2]}


def test_retry_delay_honours_provider_hint():
    import urllib.error
    from llm_client import _retry_delay
    e = urllib.error.HTTPError("u", 429, "x", {"Retry-After": "7"}, None)
    assert _retry_delay(e, "", 0) == 7.5
    e2 = urllib.error.HTTPError("u", 429, "x", {}, None)
    assert _retry_delay(e2, "Please try again in 3.4875s.", 0) == 3.4875 + 0.5
    assert _retry_delay(e2, "no hint", 1) == 3.0
