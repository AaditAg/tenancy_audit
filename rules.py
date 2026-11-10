# rules.py
# ----------------------------------------------------------------------
# Deterministic clause rules. Each rule maps a pattern in a contract clause
# to a severity, a plain-English explanation, a suggested fix and the law
# entries (see legal_kb.py) it rests on.
#
# Severities:
#   fail - clause is very likely unlawful / unenforceable under UAE law
#   warn - clause is risky, unusual or one-sided; worth negotiating
#   info - not a violation, but the user should know
# ----------------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Pattern

from textnorm import has_arabic, normalize_ar

_I = re.IGNORECASE | re.DOTALL


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: str
    title: str
    explanation: str
    suggestion: str
    laws: List[str]
    pattern: Optional[Pattern[str]] = None
    check: Optional[Callable[[str], bool]] = None   # for numeric rules
    jurisdictions: tuple = ("dubai", "abu_dhabi", "sharjah", "ajman", "ras_al_khaimah", "fujairah", "umm_al_quwain")


def _p(expr: str) -> Pattern[str]:
    return re.compile(expr, _I)


_NUM = r"(\d{1,3})"


_DAY = r"(?:days?|يوم|يوما|ايام)"
_MONTH = r"(?:months?|شهر|شهرا|اشهر|شهور)"


def _days_from(text: str, unit_re: str) -> List[int]:
    return [int(m.group(1)) for m in re.finditer(_NUM + r"\s*(?:calendar\s+|working\s+|business\s+)?" + unit_re, text, _I)]


def _eviction_notice_too_short_for_default(t: str) -> bool:
    """Landlord may evict for default only after a 30-day notice (Law 33/2008 Art. 25)."""
    if (not re.search(r"evict|terminate|vacate|اخلاء|انهاء|فسخ", t, _I)
            or not re.search(r"default|non-?payment|unpaid|fail(?:s|ure)? to pay|arrears|تخلف|عدم\s+(?:السداد|دفع|سداد)|متاخرات", t, _I)):
        return False
    days = _days_from(t, _DAY)
    return bool(days) and min(days) < 30


def _sale_use_notice_too_short(t: str) -> bool:
    """Eviction at expiry for sale / personal use needs 12 months' notice."""
    if not re.search(r"\b(sell|sale|personal\s+use|own\s+use|occupy\s+(?:it|the\s+property)\s+himself|family\s+use)\b|بيع|استخدام\s+شخصي|للاستعمال\s+الشخصي|للاستخدام\s+الشخصي", t, _I):
        return False
    if not re.search(r"evict|vacate|terminate|notice|اخلاء|اشعار|انذار", t, _I):
        return False
    months = _days_from(t, _MONTH)
    days = _days_from(t, _DAY)
    if months and max(months) >= 12:
        return False
    return bool(days) or bool(months)


def _penalty_excessive(t: str) -> bool:
    if not re.search(r"penalt|late\s+(?:fee|charge|payment)|liquidated|fine|غرامة|شرط\s+جزائي|رسوم\s+تاخير", t, _I):
        return False
    pcts = [float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", t)]
    if any(p >= 10 for p in pcts):
        return True
    return bool(re.search(r"(?:double|twice|two\s+times|three\s+times|triple)\s+(?:the\s+)?(?:monthly\s+|annual\s+)?rent|ضعف|ضعفين|مثلي", t, _I))


RULES: List[Rule] = [
    # ----------------------------------------------------------- Eviction
    Rule(
        "EVICT-NO-NOTICE", "Eviction", "fail",
        "Eviction without statutory notice",
        "The landlord may only evict on the grounds and with the notice periods listed in Law 33/2008 Art. 25. "
        "A clause allowing eviction 'at any time', 'without notice' or 'for any reason' is void.",
        "Replace with the statutory grounds and notice: 30 days for default or breach, 12 months for sale or "
        "the landlord's own use, both by notary public or registered mail.",
        ["DXB-33-2008", "DXB-26-2007"],
        _p(r"(evict|terminate|vacate).{0,80}(without\s+(?:any\s+)?(?:prior\s+)?(?:notice|warning)|at\s+any\s+time|for\s+any\s+reason|at\s+(?:its|his|her|the\s+landlord'?s)\s+(?:sole\s+|absolute\s+)?discretion)"),
    ),
    Rule(
        "EVICT-DEFAULT-SHORT", "Eviction", "fail",
        "Default notice shorter than 30 days",
        "Eviction for non-payment or breach requires a 30-day notice served by notary public or registered mail "
        "(Law 33/2008 Art. 25). A shorter period is not enforceable.",
        "Extend the cure/notice period to at least 30 days and state the service method.",
        ["DXB-33-2008"],
        check=_eviction_notice_too_short_for_default,
        jurisdictions=("dubai",),
    ),
    Rule(
        "EVICT-SALE-SHORT", "Eviction", "fail",
        "Sale / personal-use eviction with less than 12 months' notice",
        "A Dubai landlord who wants the property back at expiry for sale or personal/family use must give 12 "
        "months' notice via notary public or registered mail.",
        "State a 12-month notice period for these grounds.",
        ["DXB-33-2008"],
        check=_sale_use_notice_too_short,
        jurisdictions=("dubai",),
    ),
    Rule(
        "EARLY-LANDLORD-TERMINATION", "Eviction", "fail",
        "Landlord may terminate before the contract ends",
        "A landlord cannot end a fixed-term tenancy early except on the statutory grounds. A blanket early "
        "termination right is unenforceable.",
        "Limit early termination to the statutory grounds, or delete.",
        ["DXB-26-2007", "DXB-33-2008"],
        _p(r"landlord.{0,60}(may|shall\s+have\s+the\s+right\s+to|is\s+entitled\s+to).{0,40}terminate.{0,60}(before|prior\s+to)\s+(the\s+)?(expiry|end)"),
    ),
    # --------------------------------------------------------------- Rent
    Rule(
        "RENT-DISCRETION", "Rent increases", "fail",
        "Rent can be raised at landlord's discretion",
        "Dubai rent increases at renewal are capped by Decree 43/2013 against the rental index and need 90 days' "
        "written notice (Art. 14 of Law 26/2007 as amended). Discretionary increases are not enforceable.",
        "Replace with: 'Any rent change at renewal shall comply with Decree 43 of 2013 and the Rental Index, "
        "with at least 90 days' written notice.'",
        ["DXB-43-2013", "DXB-33-2008"],
        _p(r"(rent.{0,60}(increase|adjust|revis|review).{0,100}(absolute|sole|unilateral|landlord'?s?)\s+(discretion|decision|determination)|(landlord|lessor).{0,60}(increase|adjust|revis).{0,30}rent.{0,80}(absolute|sole|unilateral|any\s+time|discretion))"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "RENT-MID-TERM", "Rent increases", "fail",
        "Rent increase during the contract term",
        "Rent is fixed for the contract term unless the contract itself provides a compliant adjustment. "
        "Mid-term unilateral increases are not permitted.",
        "Remove or restrict to a pre-agreed schedule that complies with the rent decree.",
        ["DXB-26-2007", "DXB-43-2013"],
        _p(r"(increase|raise|adjust).{0,20}rent.{0,60}(during|within|in\s+the\s+middle\s+of)\s+(the\s+)?(term|tenancy|lease|contract\s+period)"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "RENT-AUTORENEW-INCREASE", "Rent increases", "warn",
        "Automatic renewal with a fixed or percentage increase",
        "A pre-set renewal increase is only valid up to the cap in Decree 43/2013; where the index allows 0% a "
        "contractual 5% or 10% uplift would be unenforceable.",
        "Make the renewal rent 'subject to the rental index cap in force at renewal'.",
        ["DXB-43-2013"],
        _p(r"(auto(?:matic(?:ally)?)?\s*renew|renew(?:s|ed)?\s+automatically).{0,120}(\d+(?:\.\d+)?\s*%|increase)"),
        jurisdictions=("dubai",),
    ),
    # ----------------------------------------------------- Deposit / money
    Rule(
        "DEPOSIT-FORFEIT", "Security deposit", "warn",
        "Non-refundable or automatically forfeited deposit",
        "A security deposit secures damages and arrears; it is not a fee. Retaining it without proof of loss is "
        "open to challenge before the RDC, and courts may reduce excessive retention.",
        "State that the deposit is refundable within a fixed period (e.g. 30 days) after handover, less "
        "documented damage beyond fair wear and tear.",
        ["DXB-26-2007", "FED-CTL-1985", "FED-CTL-2025"],
        _p(r"(deposit.{0,60}(non-?refundable|forfeit|shall\s+not\s+be\s+(?:returned|refunded))|(non-?refundable|forfeit).{0,40}deposit)"),
    ),
    Rule(
        "NO-REFUND", "Fees and refunds", "warn",
        "Blanket 'no refunds' clause",
        "A total refund prohibition is unfair where the landlord is at fault (e.g. property not delivered in a "
        "usable condition).",
        "Limit the clause to non-refundable admin fees that are itemised and reasonable.",
        ["FED-CTL-1985", "FED-CTL-2025"],
        _p(r"\bno\s+refunds?\b|\bnon-?refundable\b(?!.{0,20}deposit)"),
    ),
    Rule(
        "PENALTY-EXCESSIVE", "Penalties", "warn",
        "Large or open-ended penalty",
        "Penalty clauses must be proportionate: a UAE court may reduce a penalty that exceeds the actual loss "
        "under the Civil Transactions Law.",
        "Cap late fees at a modest, specific amount and tie them to actual loss.",
        ["FED-CTL-1985", "FED-CTL-2025"],
        check=_penalty_excessive,
    ),
    Rule(
        "ACCELERATION", "Penalties", "warn",
        "Tenant owes all remaining rent on early exit",
        "Requiring the tenant to pay the whole remaining rent on leaving early is a penalty a court can reduce "
        "to the landlord's actual loss.",
        "Replace with a fixed early-termination fee (commonly 1-2 months' rent) subject to re-letting.",
        ["FED-CTL-1985", "FED-CTL-2025"],
        _p(r"(early|premature)\s+(termination|exit|vacat).{0,100}(full|entire|remaining|balance|all).{0,40}rent|(remaining|balance).{0,20}rent.{0,60}(become|be)\s+(immediately\s+)?(due|payable)"),
    ),
    Rule(
        "VAT-ON-RENT", "Fees and refunds", "warn",
        "VAT charged on residential rent",
        "Residential rent is VAT-exempt in the UAE. VAT on rent is only correct for commercial premises.",
        "Delete the VAT line for residential tenancies.",
        ["FED-VAT"],
        _p(r"(\b(vat|value\s+added\s+tax)\b.{0,60}(rent|rental)|(rent|rental).{0,60}\b(plus|\+|exclusive\s+of|subject\s+to)\s+(5\s*%\s*)?(vat|value\s+added))"),
    ),
    Rule(
        "CASH-NO-RECEIPT", "Payment", "warn",
        "Cash payment without receipt",
        "Rent should be paid by traceable means with a receipt. Cash with no receipt makes any arrears claim "
        "hard to defend.",
        "Require bank transfer or cheque and written receipts.",
        ["DXB-26-2007"],
        _p(r"cash.{0,60}(no\s+receipt|without\s+(?:a\s+)?receipt)|(no\s+receipt).{0,40}cash"),
    ),
    # ------------------------------------------------ Property use / people
    Rule(
        "PARTITIONS", "Property use", "fail",
        "Partitions or room subdivision",
        "Building partitions or dividing a unit into rooms or bed-spaces breaches Dubai building and "
        "occupancy rules and needs municipal approval.",
        "Delete, or limit to approved fit-out with written landlord and authority approval.",
        ["DXB-SHARED-HOUSING"],
        _p(r"\b(partitions?|partitioning|sub-?divid\w+|bed-?\s?spaces?|erect\s+walls?)\b"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "SUBLET-FREE", "Property use", "warn",
        "Subletting allowed without landlord control",
        "In Dubai subletting needs the landlord's written consent and Ejari registration of the sub-lease. "
        "Open-ended permission to sublet or host short stays without a permit is risky.",
        "Require written consent for each sub-tenant and, for holiday letting, a DET permit.",
        ["DXB-26-2007", "DXB-HOLIDAY-HOMES"],
        _p(r"tenant.{0,60}(may|is\s+(?:free|entitled|permitted|allowed)\s+to|can).{0,30}(sub-?let|sub-?lease|airbnb|short[- ]term\s+rental|holiday\s+home)"),
    ),
    Rule(
        "LANDLORD-ENTRY", "Property use", "warn",
        "Landlord entry without notice",
        "The tenant is entitled to peaceful possession. Entry at any time without notice or consent invades "
        "that right.",
        "Allow entry on 24-48 hours' notice (except genuine emergencies).",
        ["DXB-26-2007", "FED-CTL-1985"],
        _p(r"landlord.{0,60}(enter|access|inspect).{0,60}(at\s+any\s+time|without\s+(?:prior\s+)?(?:notice|consent|permission))"),
    ),
    Rule(
        "MAINTENANCE-ALL-TENANT", "Maintenance", "warn",
        "All or structural maintenance placed on tenant",
        "The landlord bears major/structural maintenance; the tenant covers minor wear-and-tear repairs. "
        "Shifting everything to the tenant is one-sided and likely unenforceable for major items.",
        "Split duties: tenant for minor repairs and consumables, landlord for structure, AC main units, plumbing "
        "and electrical faults not caused by the tenant.",
        ["DXB-26-2007", "FED-CTL-1985"],
        _p(r"tenant.{0,60}(responsible|liable|shall\s+bear|pay).{0,60}(all|any|major|structural|every)\s+(maintenance|repairs?)"),
    ),
    # ----------------------------------------------------- Legal mechanics
    Rule(
        "WAIVER-OF-RIGHTS", "Tenant rights", "fail",
        "Tenant waives statutory rights",
        "Mandatory protections in the Dubai tenancy laws cannot be waived by contract; such a clause is void.",
        "Delete, or limit to rights that can lawfully be waived.",
        ["DXB-26-2007", "FED-CTL-1985"],
        _p(r"tenant.{0,40}(waives?|relinquish\w*|gives?\s+up|forgoes?).{0,60}(all\s+)?(rights?|claims?|remed\w+|protections?)"),
    ),
    Rule(
        "NO-EJARI", "Registration", "fail",
        "Contract not to be registered with Ejari",
        "Tenancy contracts in Dubai must be registered through Ejari. An unregistered tenancy weakens both "
        "parties and blocks DEWA, visa and RDC filings.",
        "Add that the landlord/tenant will register the contract on Ejari within a stated period.",
        ["DXB-26-2007", "DXB-33-2008"],
        _p(r"(not\s+(?:be\s+)?(?:required\s+to\s+)?(?:register|registered)|no\s+need\s+to\s+register|without\s+(?:ejari|registration)).{0,60}(ejari|rera|tenancy|contract)|ejari.{0,40}(not\s+required|shall\s+not|will\s+not)"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "FOREIGN-JURISDICTION", "Disputes", "warn",
        "Foreign law or courts for a UAE lease",
        "Disputes over Dubai property fall to the Rental Disputes Settlement Centre; foreign courts or foreign "
        "law are unlikely to be applied to a Dubai lease.",
        "Specify UAE law and the Dubai Rental Disputes Settlement Centre.",
        ["DXB-26-2013"],
        _p(r"(governed\s+by|subject\s+to).{0,30}(laws?\s+of\s+)?(england|singapore|india|united\s+kingdom|new\s+york|difc)|courts\s+of\s+(england|london|singapore|india|new\s+york)"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "OUST-RDC", "Disputes", "warn",
        "Attempt to exclude the Rental Disputes Centre",
        "The RDC has statutory jurisdiction over Dubai landlord-tenant disputes. Contracting out is unlikely to "
        "work and may be seen as a unfair term.",
        "Reference the RDC, optionally with prior amicable negotiation.",
        ["DXB-26-2013"],
        _p(r"(waive|exclude|not\s+(?:apply|resort)\s+to|no\s+right\s+to).{0,50}(rental\s+dispute|rdc|rera\s+dispute)"),
        jurisdictions=("dubai",),
    ),
    Rule(
        "PERSONAL-DATA", "Privacy", "warn",
        "Broad data-sharing permission",
        "Personal data (Emirates ID, passport, contacts) can only be used for a clear, lawful purpose.",
        "Limit data use to the tenancy, Ejari/DEWA registration and legal obligations.",
        ["FED-PDPL-45-2021"],
        _p(r"(share|sell|disclose|transfer).{0,40}(tenant'?s?\s+)?(personal\s+)?(data|information|details).{0,40}(third\s+part\w+|marketing|any\s+(?:person|party))"),
    ),
    Rule(
        "CHEQUE-BLANK", "Payment", "warn",
        "Blank or open-amount security cheque",
        "Cheques should state an amount and purpose; blank cheques invite misuse.",
        "Use dated cheques with fixed amounts only.",
        ["FED-COMM-50-2022"],
        _p(r"(blank|undated|open[- ]amount|signed\s+(?:but\s+)?blank)\s+cheques?"),
    ),
]


# Arabic variants (written against textnorm.normalize_ar output: alef/ya unified, no diacritics, ASCII digits).
_RENT = r"(?:الايجار|الاجار|بدل\s+الايجار)"
ARABIC_PATTERNS: Dict[str, Pattern[str]] = {
    "EVICT-NO-NOTICE": _p(r"(?:اخلاء|انهاء|فسخ).{0,60}(?:دون|بدون|من\s+غير)\s+(?:اي\s+)?(?:اشعار|انذار|اخطار)|(?:اخلاء|انهاء).{0,60}(?:في\s+اي\s+وقت|لاي\s+سبب|بدون\s+سبب)"),
    "RENT-DISCRETION": _p(r"(?:زياد[ةه]|رفع|تعديل)\s+" + _RENT + r".{0,80}(?:حسب\s+تقدير|وفق\s+تقدير|وفقا\s+لتقدير|بتقدير|بشكل\s+منفرد|بارادة\s+منفرد|بمحض\s+ارادة)|(?:ال|لل)مؤجر.{0,40}(?:زياد|رفع|تعديل).{0,30}" + _RENT + r".{0,60}(?:في\s+اي\s+وقت|متي\s+شاء|حسب\s+رغبت)"),
    "RENT-MID-TERM": _p(r"(?:زياد[ةه]|رفع)\s+" + _RENT + r".{0,60}(?:اثناء|خلال)\s+(?:مد[ةه]|فتر[ةه])\s+(?:العقد|الايجار)"),
    "DEPOSIT-FORFEIT": _p(r"(?:التامين|مبلغ\s+التامين|الوديع[ةه]).{0,60}(?:غير\s+قابل\s+لل?(?:استرداد|استرجاع)|لا\s+(?:يرد|يسترد|يعاد|تسترد|ترد)|يصادر|مصادر[ةه]|يصبح\s+حقا\s+للمؤجر)"),
    "NO-REFUND": _p(r"(?:لا\s+يوجد|لا\s+يحق|لا\s+يتم)\s+(?:اي\s+)?(?:استرداد|استرجاع)|غير\s+قابل[ةه]?\s+لل?(?:استرداد|استرجاع)"),
    "PARTITIONS": _p(r"(?:قواطع|فواصل|حواجز|تقسيم\s+(?:الوحد|الغرف|الشق)|اضاف[ةه]\s+جدران|بناء\s+جدران|سكن\s+مشترك|مشاركه\s+السكن)"),
    "VAT-ON-RENT": _p(r"ضريب[ةه]\s+(?:القيم[ةه]\s+)?المضاف[ةه].{0,60}" + _RENT + r"|" + _RENT + r".{0,60}(?:غير\s+شامل|لا\s+يشمل|بالاضاف[ةه]\s+الي).{0,30}ضريب"),
    "LANDLORD-ENTRY": _p(r"(?:ال|لل)مؤجر.{0,60}(?:دخول|الدخول|تفتيش|معاين).{0,60}(?:في\s+اي\s+وقت|دون|بدون)"),
    "MAINTENANCE-ALL-TENANT": _p(r"(?:(?:ال|لل)مستاجر.{0,60}(?:مسؤول|يتحمل|ملزم)|(?:يتحمل|يلتزم)\s+(?:ال)?مستاجر).{0,60}(?:جميع|كاف[ةه]|كل|اي)\s+(?:اعمال\s+)?(?:الصيان[ةه]|الاصلاحات|التصليحات)"),
    "FOREIGN-JURISDICTION": _p(r"(?:قوانين|قانون)\s+(?:دول[ةه]\s+)?(?:انجلترا|بريطانيا|المملك[ةه]\s+المتحد[ةه]|الهند|سنغافور[ةه]|نيويورك)|محاكم\s+(?:لندن|انجلترا|سنغافور[ةه]|الهند|نيويورك)"),
    "WAIVER-OF-RIGHTS": _p(r"(?:(?:ال|لل)مستاجر.{0,40}(?:يتنازل|يسقط)|(?:يتنازل|يسقط)\s+(?:ال)?مستاجر).{0,60}(?:حقوق|مطالبات|دعاوي)"),
    "SUBLET-FREE": _p(r"(?:ال|لل)مستاجر.{0,40}(?:يحق|يجوز|له\s+الحق).{0,40}(?:التاجير\s+من\s+الباطن|تاجير.{0,12}من\s+الباطن|الايجار\s+القصير|التنازل\s+عن\s+العقد)"),
    "NO-EJARI": _p(r"(?:لا\s+يلزم|غير\s+ملزم|لا\s+حاج[ةه]|دون).{0,30}(?:تسجيل|توثيق).{0,40}(?:ايجاري|العقد)"),
    "CASH-NO-RECEIPT": _p(r"(?:نقدا|نقدي).{0,60}(?:دون|بدون)\s+(?:ايصال|سند\s+قبض|اي\s+ايصال)"),
}


# Compile against the same normalisation applied to contract text (e.g. ؤ -> و), or such letters never match.
ARABIC_PATTERNS = {k: re.compile(normalize_ar(v.pattern), _I) for k, v in ARABIC_PATTERNS.items()}


def match_rules(clause_text: str, jurisdiction: str = "dubai") -> List[Rule]:
    """Return every rule that fires on a clause (English or Arabic), most severe first."""
    t = re.sub(r"\s+", " ", clause_text or "")
    tn = normalize_ar(t) if has_arabic(t) else None
    hits: List[Rule] = []
    for r in RULES:
        if jurisdiction not in r.jurisdictions:
            continue
        ok = False
        for text in ([t] + ([tn] if tn else [])):
            if (r.pattern is not None and r.pattern.search(text)) or (r.check is not None and r.check(text)):
                ok = True
                break
        if not ok and tn and r.id in ARABIC_PATTERNS and ARABIC_PATTERNS[r.id].search(tn):
            ok = True
        if ok:
            hits.append(r)
    order = {"fail": 0, "warn": 1, "info": 2}
    hits.sort(key=lambda r: order.get(r.severity, 3))
    return hits
