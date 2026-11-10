# legal_kb.py
# ----------------------------------------------------------------------
# Curated knowledge base of UAE / Dubai rental-relevant law.
#
# Every entry carries a `verified` flag. `True` means the citation and the
# gist were cross-checked against multiple published sources; `False` means
# the entry is a pointer that must be confirmed against the official gazette
# or RERA / DLD before anyone relies on it. The UI surfaces this flag.
#
# `LAST_REVIEWED` should be bumped whenever this file is re-checked.
# ----------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

LAST_REVIEWED = "2026-10-30"


@dataclass(frozen=True)
class LawEntry:
    id: str
    jurisdiction: str          # "federal" | "dubai" | "abu_dhabi" | "sharjah" | ...
    citation: str              # short label shown in the UI
    title: str
    summary: str               # plain-English gist used for retrieval + LLM grounding
    topics: List[str] = field(default_factory=list)
    verified: bool = True


KB: List[LawEntry] = [
    # ------------------------------------------------------------ Dubai core
    LawEntry(
        "DXB-26-2007", "dubai", "Law 26/2007",
        "Law No. 26 of 2007 Regulating the Relationship between Landlords and Tenants in Dubai",
        "Core Dubai tenancy statute. Tenancy contracts must be in writing and registered with RERA through Ejari. "
        "Landlord must hand over the property in a usable state and carry out major/structural maintenance; the tenant "
        "carries out minor maintenance and may not sublet or change the use of the property without the landlord's "
        "written consent. Rent cannot be increased during the contract term unless the contract allows it and the "
        "increase complies with the rent index decree. Parties cannot contract out of its mandatory protections.",
        ["registration", "maintenance", "subletting", "rent", "eviction", "waiver", "general"],
    ),
    LawEntry(
        "DXB-33-2008", "dubai", "Law 33/2008 (amending 26/2007)",
        "Law No. 33 of 2008 Amending Law 26 of 2007",
        "Amended Law 26/2007. Art. 14: rent changes at renewal require at least 90 days' written notice before the "
        "contract expires, otherwise the tenancy renews on existing terms. Art. 25: the landlord may only evict "
        "during the term on listed grounds, e.g. non-payment after a 30-day notice served via notary public or "
        "registered mail; unauthorised subletting; illegal or nuisance use; breach after a 30-day notice; demolition "
        "or renovation that needs vacant possession; or, at expiry, the landlord's own/ first-degree relatives' use "
        "or sale of the property - which requires 12 months' notice via notary public or registered mail.",
        ["eviction", "notice", "rent", "renewal", "termination"],
    ),
    LawEntry(
        "DXB-43-2013", "dubai", "Decree 43/2013",
        "Decree No. 43 of 2013 Determining Rent Increases for Properties in the Emirate of Dubai",
        "Caps renewal rent increases by how far the current rent is below the RERA rental index average for similar "
        "units: up to 10% below -> no increase; 11-20% below -> max 5%; 21-30% below -> max 10%; 31-40% below -> max "
        "15%; more than 40% below -> max 20%. If rent is at or above the index no increase is permitted. "
        "Applies to renewals, not mid-term.",
        ["rent", "renewal", "index"],
    ),
    LawEntry(
        "DXB-RENTAL-INDEX", "dubai", "Dubai Smart Rental Index",
        "Dubai Smart Rental Index (DLD / RERA)",
        "Official index tool run by the Dubai Land Department that supplies the average rent for comparable units. "
        "It supersedes the older RERA rent calculator and is the reference used for Decree 43/2013 caps. Always "
        "check the live index for the exact figure; third-party calculators can be stale.",
        ["rent", "index"],
        verified=True,
    ),
    LawEntry(
        "DXB-26-2013", "dubai", "Law 26/2013 (RDC)",
        "Law No. 26 of 2013 Establishing the Rental Disputes Settlement Centre",
        "The Rental Disputes Settlement Centre (RDC) of the Dubai Courts has jurisdiction over disputes between "
        "landlords and tenants and over unpaid rent / eviction claims for Dubai properties. A clause that tries to "
        "remove RDC jurisdiction or force foreign courts for a Dubai residential lease is unlikely to be effective.",
        ["jurisdiction", "disputes", "arbitration"],
    ),
    LawEntry(
        "DXB-7-2006", "dubai", "Law 7/2006",
        "Law No. 7 of 2006 Concerning Real Property Registration in Dubai",
        "Governs registration of real property interests with the Dubai Land Department. Long-term leases (generally "
        "over 10 years) and usufruct rights need DLD registration; ordinary 1-year residential leases register "
        "through Ejari instead.",
        ["registration", "long-term-lease"],
    ),
    LawEntry(
        "DXB-SHARED-HOUSING", "dubai", "Dubai Municipality shared-housing / partition rules",
        "Dubai Municipality occupancy and partitioning requirements",
        "Dividing a unit with partitions, creating bed-spaces or exceeding permitted occupancy breaches Dubai "
        "building and occupancy rules and can lead to fines and eviction. Reports exist of newer shared-housing "
        "legislation but the exact instrument could not be verified; confirm the current text with Dubai Municipality.",
        ["subletting", "partitions", "occupancy"],
        verified=False,
    ),
    LawEntry(
        "DXB-HOLIDAY-HOMES", "dubai", "DET Holiday Home permit regime",
        "Dubai Department of Economy and Tourism holiday-home rules",
        "Short-term / holiday letting of a unit requires a Holiday Home permit from DET and landlord (and, in "
        "communities, owners' association) approval. A long-lease tenant sub-letting on Airbnb without it is "
        "non-compliant.",
        ["subletting", "short-term"],
        verified=False,
    ),
    # ----------------------------------------------------------- Federal
    LawEntry(
        "FED-CTL-2025", "federal", "Federal Decree-Law 25/2025",
        "New UAE Civil Transactions Law (in force 1 June 2026)",
        "Replaces the 1985 Civil Transactions Law with effect from 1 June 2026. Contracts concluded before that "
        "date generally stay under the 1985 law; newer contracts fall under the new code for matters the "
        "emirate-level rental laws do not cover (good faith, penalty clauses, hardship, interpretation). "
        "Specific lease article numbers were not independently verified.",
        ["general", "penalty", "good-faith"],
    ),
    LawEntry(
        "FED-CTL-1985", "federal", "Federal Law 5/1985 (Civil Transactions)",
        "UAE Civil Transactions Law (1985), for pre-June-2026 contracts",
        "Applies where the local tenancy laws are silent: contracts are interpreted in good faith; the lessor must "
        "keep the property usable and bear major repairs; the lessee must use it for the agreed purpose with "
        "reasonable care; a court may reduce an agreed penalty that is excessive compared with the actual loss, and "
        "terms that exclude liability for gross fault are void.",
        ["penalty", "maintenance", "good-faith", "waiver"],
    ),
    LawEntry(
        "FED-VAT", "federal", "Federal Decree-Law 8/2017 + Cabinet Decision 52/2017",
        "UAE VAT Law and Executive Regulation (residential property)",
        "Residential rent is exempt from 5% VAT (except the first supply within 3 years of construction, which is "
        "zero-rated). Commercial property rent is subject to 5% VAT. A residential landlord charging VAT on rent is "
        "a red flag.",
        ["vat", "rent"],
    ),
    LawEntry(
        "FED-COMM-50-2022", "federal", "Federal Decree-Law 50/2022",
        "UAE Commercial Transactions Law (cheques)",
        "Governs cheques including post-dated rent cheques. Bounced cheques are now mainly a civil/administrative "
        "matter, with criminal liability limited to specific fraudulent cases (see Federal Decree-Law 14/2020 "
        "changes). Security cheques should state a clear amount and purpose.",
        ["cheques", "payment"],
    ),
    LawEntry(
        "FED-PDPL-45-2021", "federal", "Federal Decree-Law 45/2021",
        "UAE Personal Data Protection Law",
        "Personal data such as Emirates ID, passport and contact details may only be processed for a clear "
        "purpose with a lawful basis. Clauses granting blanket rights to share or sell tenant data are risky.",
        ["privacy"],
    ),
    LawEntry(
        "FED-ETRANS-46-2021", "federal", "Federal Decree-Law 46/2021",
        "UAE Electronic Transactions and Trust Services Law",
        "Gives electronic signatures and electronic contracts legal effect, so an e-signed tenancy contract "
        "(including via Ejari / DLD) is valid.",
        ["signature", "general"],
    ),
    # ------------------------------------------------------- Other emirates
    # Everything below comes from secondary sources (law-firm notes, news, property portals) that sometimes
    # disagree, so every entry is verified=False. Confirm with the municipality / rent committee before relying on it.
    LawEntry(
        "ABD-20-2006", "abu_dhabi", "Abu Dhabi Law 20/2006 (as amended)",
        "Abu Dhabi Law No. 20 of 2006 on Landlord-Tenant Relations (amended incl. by Law 4/2010)",
        "Abu Dhabi regime. Tenancy contracts are registered on Tawtheeq. Commonly cited: two months' written notice "
        "for residential rent changes or non-renewal (three for commercial); disputes go to the Rent Dispute "
        "Settlement Committee, whose decisions are final. A 5% annual cap was reported and can be changed by the "
        "Executive Council. Sources disagree on eviction notice for sale/own use (6-12 months reported).",
        ["registration", "rent", "eviction", "notice"],
        verified=False,
    ),
    LawEntry(
        "ABD-FREEZE-2026", "abu_dhabi", "ADREC rent-increase suspension (reported June 2026)",
        "Abu Dhabi Real Estate Centre suspension of rent increases",
        "Reported in June 2026: the Abu Dhabi Real Estate Centre suspended rent increases on new tenancy contracts "
        "and renewals until further notice. If still in force, a proposed increase would not be allowed. "
        "Confirm current status with ADREC.",
        ["rent", "renewal"],
        verified=False,
    ),
    LawEntry(
        "SHJ-5-2024", "sharjah", "Sharjah Law 5/2024 (new rental law)",
        "Sharjah Law No. 5 of 2024 Regulating the Landlord-Tenant Relationship",
        "Newer Sharjah law (replaces the older regime). Reported: rent cannot be increased in the first 3 years "
        "unless the tenant agrees; after that rent follows a 'fair rent' standard; refusing an increase is not a "
        "ground for eviction; leases must be written and registered with Sharjah Municipality, with ratification "
        "within about 15 days; unpaid-rent claims can follow a 15-day notice; disputes go to the Rental Dispute "
        "Resolution Centre. Eviction notice periods are reported inconsistently (about 3 months or more).",
        ["registration", "rent", "eviction", "notice"],
        verified=False,
    ),
    LawEntry(
        "AJM-TENANCY", "ajman", "Ajman tenancy rules",
        "Ajman Emirate rules on rent and landlord-tenant relations",
        "Reported: rent cannot be raised for 3 years from signing, then increases are limited (about 20% per "
        "3 years is reported). Contracts are registered with Ajman Municipality / Land and Property Department. "
        "The governing instrument is cited inconsistently (Law 3/2009, Emiri Decree 6/2005), and a 12-month "
        "eviction notice is reported by one source only.",
        ["registration", "rent", "eviction"],
        verified=False,
    ),
    LawEntry(
        "RAK-TENANCY", "ras_al_khaimah", "Ras Al Khaimah tenancy practice",
        "Ras Al Khaimah landlord-tenant rules",
        "Sources conflict on whether RAK has a dedicated tenancy statute; the federal Civil Transactions Law "
        "fills gaps. 90 days' written notice before expiry for rent or term changes is widely cited; reported rent "
        "caps range from 5% to 10% and a municipality rent index is mentioned. Contracts are registered online "
        "with the RAK government; unregistered leases may not be recognised by utilities or the rent committee.",
        ["registration", "rent", "notice"],
        verified=False,
    ),
    LawEntry(
        "FUJ-TENANCY", "fujairah", "Fujairah Law 1/2011 (reported)",
        "Fujairah rules on rent payments and increases",
        "Reported: Fujairah Law No. 1 of 2011 limits rent increases (a 5% annual maximum is reported). Leases "
        "are registered with Fujairah Municipality through the EJAAR portal and unregistered leases are not "
        "accepted by government departments. Eviction notice rules could not be found.",
        ["registration", "rent"],
        verified=False,
    ),
    LawEntry(
        "UAQ-TENANCY", "umm_al_quwain", "Umm Al Quwain tenancy rules",
        "Umm Al Quwain landlord-tenant rules",
        "Reported: a 10% maximum rent increase with three months' notice before the lease ends. Registration "
        "with Umm Al Quwain Municipality is mandatory; a late-registration penalty of about 1% of the rent plus a "
        "2% registration fee is reported. The governing statute is cited inconsistently (Law 1/2006 vs Law 3/2008).",
        ["registration", "rent", "notice"],
        verified=False,
    ),
]

KB_BY_ID: Dict[str, LawEntry] = {e.id: e for e in KB}

JURISDICTIONS: Dict[str, str] = {
    "dubai": "Dubai",
    "abu_dhabi": "Abu Dhabi",
    "sharjah": "Sharjah",
    "ajman": "Ajman",
    "ras_al_khaimah": "Ras Al Khaimah",
    "fujairah": "Fujairah",
    "umm_al_quwain": "Umm Al Quwain",
}

# Per-emirate profile used for registration / reported-rule checks. Values for every emirate except Dubai are
# *reported* (secondary sources), so the engine only ever raises a "warn" from them, never a "fail".
#   reg:       name of the registration system / body
#   notice:    commonly reported minimum days' notice for a rent change (None = unknown)
#   cap_pct:   reported maximum annual increase (None = none reported)
#   protect_years: reported period at the start of a lease with no increase allowed
#   freeze:    a reported blanket freeze on increases
#   law_ids:   knowledge-base entries to cite
PROFILES: Dict[str, Dict] = {
    "dubai": {"reg": "Ejari", "notice": 90, "law_ids": ["DXB-26-2007"]},
    "abu_dhabi": {"reg": "Tawtheeq", "notice": 60, "cap_pct": 5.0, "freeze": True,
                  "law_ids": ["ABD-20-2006", "ABD-FREEZE-2026"]},
    "sharjah": {"reg": "Sharjah Municipality registration", "notice": None, "protect_years": 3,
                "law_ids": ["SHJ-5-2024"]},
    "ajman": {"reg": "Ajman Municipality registration", "notice": None, "protect_years": 3, "cap_pct": 20.0,
              "law_ids": ["AJM-TENANCY"]},
    "ras_al_khaimah": {"reg": "RAK online tenancy registration", "notice": 90, "law_ids": ["RAK-TENANCY"]},
    "fujairah": {"reg": "Fujairah Municipality (EJAAR)", "notice": None, "cap_pct": 5.0, "law_ids": ["FUJ-TENANCY"]},
    "umm_al_quwain": {"reg": "Umm Al Quwain Municipality registration", "notice": 90, "cap_pct": 10.0,
                      "law_ids": ["UAQ-TENANCY"]},
}


def applicable_entries(jurisdiction: str) -> List[LawEntry]:
    """Federal law applies everywhere; add the emirate-specific entries."""
    return [e for e in KB if e.jurisdiction in ("federal", jurisdiction)]


def citation_for(law_id: str) -> str:
    e = KB_BY_ID.get(law_id)
    return e.citation if e else law_id
