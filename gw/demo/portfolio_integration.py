"""Second synthetic portfolio: post-merger integrations on the `integration-100-day` gate model.

and ADR 0008: the same kernel
governs a programme run from task lists, with no schedule network, so schedule signals are absent.
Everything is invented: acquirers, targets, advisers, authorities and people.
"""

from __future__ import annotations

from gw.demo.portfolio import PartySpec, ProjectSpec, UserSpec

NAME = "integration"


def _proj(code: str, name: str, country: str, zone: str, gate: str, review: int, past: tuple[int, ...], pm: str,
          budget: float, target: float, milestone: str, elapsed: float) -> ProjectSpec:
    return ProjectSpec(code, name, name.split(", ")[-1], country, zone, "other", "integration", budget, "EUR",
                       "integration-100-day", gate, review, past, pm, 4, "Brightline Comms",
                       {"main": "Kessler & Rao Legal", "systems": "Corvid ERP Services", "payroll": "Paybridge"},
                       target, milestone, 0.06, elapsed)


PROJECTS: list[ProjectSpec] = [
    _proj("MA-01", "Integration of a regional distributor, Hamburg", "DE", "EU", "day100", 12, (-190, -150), "n.berg",
          4.2e6, 0.72, "Day 100 review", 0.85),
    _proj("MA-02", "Integration of a cold-chain carrier, Rotterdam", "NL", "EU", "day30", 9, (-40,), "s.vermeer",
          2.8e6, 0.64, "Day 30 review", 0.45),
    _proj("MA-03", "Integration of a packaging supplier, Lyon", "FR", "EU", "day1", 16, (), "c.marchand",
          1.9e6, 0.55, "Day 1 readiness review", 0.12),
    _proj("MA-04", "Integration of a 3PL operator, Poznań", "PL", "EU", "day100", 25, (-160, -110), "a.nowak",
          3.1e6, 0.81, "Day 100 review", 0.7),
    _proj("MA-05", "Integration of a spare-parts business, Madrid", "ES", "EU", "day30", 20, (-55,), "p.ferrer",
          2.2e6, 0.6, "Day 30 review", 0.5),
    _proj("MA-06", "Integration of a freight-forwarding desk, Milan", "IT", "EU", "day1", 30, (), "g.rossi",
          1.4e6, 0.42, "Day 1 readiness review", 0.08),
]


def _p(key: str, name: str, kind: str, country: str, behaviour: str, contact: str, lang: str = "en") -> PartySpec:
    return PartySpec(key, name, kind, country, behaviour, contact, lang)


PARTIES: list[PartySpec] = [
    _p("legal", "Kessler & Rao Legal", "consultant", "DE", "prompt", "M. Kessler"),
    _p("erp", "Corvid ERP Services", "vendor", "NL", "slow", "J. Dekker", "nl"),
    _p("payroll", "Paybridge", "vendor", "FR", "prompt", "L. Moreau", "fr"),
    _p("comms", "Brightline Comms", "contractor", "NL", "delegating", "E. Smit", "nl"),
    _p("hrcons", "Talentry HR Advisory", "consultant", "ES", "slow", "R. Navarro", "es"),
    _p("bank", "Northgate Bank", "vendor", "DE", "silent", "Onboarding desk"),
    _p("itout", "Stackwise IT Outsourcing", "vendor", "PL", "flagger", "K. Wójcik"),
    _p("audit", "Clearline Audit", "consultant", "NL", "prompt", "A. de Groot", "nl"),
    _p("pmo", "PMO", "internal_team", "NL", "prompt", "Integration office"),
    _p("finance", "Finance", "internal_team", "NL", "prompt", "Group finance"),
    _p("hr", "HR", "internal_team", "NL", "slow", "Group HR"),
    _p("it", "IT", "internal_team", "NL", "prompt", "Group IT"),
    _p("legal-int", "Legal", "internal_team", "NL", "prompt", "Group legal"),
    _p("salesops", "Sales operations", "internal_team", "NL", "prompt", "Sales ops"),
    _p("muni-DE", "Trade registry, Hamburg", "authority", "DE", "slow", "Registry desk"),
    _p("muni-NL", "Chamber registry, Rotterdam", "authority", "NL", "prompt", "Registry desk", "nl"),
    _p("muni-FR", "Commercial court registry, Lyon", "authority", "FR", "slow", "Greffe", "fr"),
    _p("muni-PL", "Court registry, Poznań", "authority", "PL", "silent", "Registry desk"),
    _p("muni-ES", "Mercantile registry, Madrid", "authority", "ES", "slow", "Registro", "es"),
    _p("muni-IT", "Business registry, Milan", "authority", "IT", "slow", "Registro"),
    _p("env-DE", "Competition authority (DE)", "authority", "DE", "slow", "Merger desk"),
    _p("env-ES", "Data protection authority (ES)", "authority", "ES", "silent", "Registro", "es"),
]

USERS: list[UserSpec] = [
    UserSpec("aleks", "A. Sidorecs", ("portfolio_lead", "investment_committee", "admin", "viewer")),
    UserSpec("b.keller", "B. Keller", ("sponsor", "viewer")),
    UserSpec("n.berg", "N. Berg", ("project_manager",), "Europe/Berlin"),
    UserSpec("s.vermeer", "S. Vermeer", ("project_manager",)),
    UserSpec("c.marchand", "C. Marchand", ("project_manager",), "Europe/Paris"),
    UserSpec("a.nowak", "A. Nowak", ("project_manager",), "Europe/Warsaw"),
    UserSpec("p.ferrer", "P. Ferrer", ("project_manager",), "Europe/Madrid"),
    UserSpec("g.rossi", "G. Rossi", ("project_manager",), "Europe/Rome"),
    UserSpec("l.jansen", "L. Jansen", ("sme:finance", "project_manager")),
    UserSpec("g.okoro", "G. Okoro", ("sme:it", "sme:legal", "sme:hr", "sme:engineering")),
    UserSpec("h.vos", "H. Vos", ("site_lead", "sme:operations")),
]

PACKAGES: dict[str, list[tuple[str, str, str]]] = {
    "other": [("legal", "Legal close and contracts", "main"), ("systems", "IT, ERP and data", "systems"),
              ("people", "People, payroll and retention", "payroll"), ("customers", "Customer and supplier comms", "gc"),
              ("finance", "Finance and reporting", "main")],
}

LESSONS = [
    ("L-101", "Book the payroll cut-over before signing", "On an earlier integration the first payroll ran late "
     "because the provider needed six weeks of notice.", None, ["people", "payroll"]),
    ("L-102", "Registry filings take longer in August", "Commercial registries answered in three weeks instead of one.",
     None, ["compliance", "authority"]),
    ("L-103", "Map the chart of accounts before Day 30", "Late mapping delayed the baseline P&L by a month.",
     None, ["cost", "systems"]),
]

FLOAT_NOW: dict[str, float] = {}
PLANNER_PROJECT = "MA-01"
HAS_SCHEDULES = False
CASES: tuple[str, ...] = ()
TARGET_COMMITMENTS = 760
TARGET_INBOUND = 4200
CORPUS_SIZE = 1000
PRIORS_SIZE = 300
