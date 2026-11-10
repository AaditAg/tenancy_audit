import pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datetime import date
import audit_engine as ae
import legal_kb, rules, textnorm, translations as tr

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_every_translation_complete_and_placeholders_match():
    for table in (tr.UI, tr.EMIRATE_NAMES, tr.RULE_TITLES, tr.CHECK_TEXT):
        for key, vals in table.items():
            assert len(vals) == len(tr.LANGS), key
            assert all(v.strip() for v in vals), key
            ph = [set(re.findall(r"\{(\w+)\}", v)) for v in vals]
            assert all(p == ph[0] for p in ph), (key, ph)


def test_every_rule_has_a_plain_title_and_every_emirate_a_name():
    assert {r.id for r in rules.RULES} <= set(tr.RULE_TITLES)
    assert set(legal_kb.JURISDICTIONS) == set(tr.EMIRATE_NAMES) == set(legal_kb.PROFILES)


def test_digit_and_script_normalisation():
    assert textnorm.to_ascii_digits("١١٠٬٠٠٠ و ۲۵٪") == "110,000 و 25%"
    assert textnorm.detect_language("عقد إيجار") == "ar" and textnorm.detect_language("rent contract") == "en"
    assert textnorm.normalize_ar("المؤجر") == textnorm.normalize_ar("الموجر")


def test_arabic_sample_parses_and_is_flagged():
    text = (ROOT / "samples" / "sample_contract_ar.txt").read_text()
    e = ae.parse_ejari_text(text)
    assert (e.contract_language, e.jurisdiction, e.current_annual_rent_aed, e.security_deposit_aed) == ("ar", "dubai", 110000, 11000)
    assert e.start_date == date(2025, 3, 1) and e.end_date == date(2026, 2, 28) and e.bedrooms == 2
    res = ae.run_audit(text, e)
    ids = {r for c in res.clause_findings for r in c.rule_ids}
    assert {"RENT-DISCRETION", "EVICT-NO-NOTICE", "PARTITIONS", "DEPOSIT-FORFEIT", "MAINTENANCE-ALL-TENANT",
            "PENALTY-EXCESSIVE", "LANDLORD-ENTRY", "FOREIGN-JURISDICTION"} <= ids
    assert res.verdict == "fail" and not res.missing_items
    assert [c.verdict for c in res.clause_findings if "قوانين" in c.text and "انجلترا" in c.text] == ["warn"]


def test_arabic_clean_clause_not_flagged():
    assert not rules.match_rules("يلتزم المؤجر بتسليم الوحدة بحالة جيدة ويستخدمها المستأجر للسكن فقط.")


def test_detects_each_emirate_from_text():
    for jur, word in [("abu_dhabi", "Abu Dhabi"), ("sharjah", "Sharjah"), ("ajman", "Ajman"),
                      ("ras_al_khaimah", "Ras Al Khaimah"), ("fujairah", "Fujairah"), ("umm_al_quwain", "Umm Al Quwain"),
                      ("sharjah", "الشارقة"), ("ajman", "عجمان")]:
        assert ae.parse_ejari_text(f"Location: {word}\nAnnual Rent: AED 40,000").jurisdiction == jur, word


def test_other_emirates_only_warn_never_fail():
    for jur in legal_kb.JURISDICTIONS:
        if jur == "dubai":
            continue
        e = ae.EjariFields(jurisdiction=jur, current_annual_rent_aed=50000, proposed_new_rent_aed=60000,
                           start_date=date(2025, 1, 1), end_date=date(2026, 12, 31),
                           renewal_date=date(2026, 12, 31), notice_sent_date=date(2026, 12, 1))
        checks, _, _ = ae.run_numeric_checks(e, None)
        assert checks and all(c.status != "fail" for c in checks), jur
        assert any(c.id == "REGISTRATION" and c.status == "warn" for c in checks)


def test_dubai_only_rules_do_not_fire_elsewhere():
    clause = "The Tenant may install partitions and sub-divide the rooms."
    assert "PARTITIONS" in {r.id for r in rules.match_rules(clause, "dubai")}
    assert "PARTITIONS" not in {r.id for r in rules.match_rules(clause, "abu_dhabi")}


def test_check_text_templates_fill():
    assert "12.5" in tr.check_text("RENT-CAP", "fail", "hi", proposed="12.5", max="5")
    assert tr.check_text("NOTICE-90", "fail", "ml", days="40")
    assert tr.check_text("UNKNOWN", "x", "en") == ""
