"""Fixed, seed-independent portfolio data.

Everything here is invented: firms, people, sites and authorities are generic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEMO_DOMAIN = "example.com"


@dataclass(frozen=True)
class ProjectSpec:
    code: str
    name: str
    site: str
    country: str
    zone: str
    archetype: str
    size: str
    capex: float
    currency: str
    gate_model: str
    current_gate: str
    review_in_days: int
    past_reviews: tuple[int, ...]  # days relative to today, oldest first, one per earlier gate
    pm: str
    estimate_class: int
    gc: str
    vendors: dict[str, str]  # tag -> vendor name (racking, systems, slab, fire, dock, refrigeration, pv, asrs, weigh, main)
    target_readiness: float
    next_milestone: str
    contingency_pct: float = 0.08
    time_elapsed: float = 0.4


PROJECTS: list[ProjectSpec] = [
    ProjectSpec("DC-01", "Regional DC, Venlo", "Venlo", "NL", "EU", "greenfield", "42,000 m² | 36 docks | VNA",
                68e6, "EUR", "fel-v3", "fel3", 14, (-253, -148), "m.devries", 2, "Bouwcombinatie Zuid",
                {"racking": "Rackline BV", "systems": "Nordflow", "slab": "SlabTec", "fire": "Sprinkfix",
                 "dock": "Dockrite", "main": "Rackline BV"}, 0.66, "FID review", 0.08, 0.30),
    ProjectSpec("DC-02", "Shuttle ASRS retrofit, Lyon", "Lyon", "FR", "EU", "brownfield", "18,000 pallet locations",
                24e6, "EUR", "fel-v3", "execute", 23, (-560, -430, -300), "c.marchand", 2, "Bouwcombinatie Zuid",
                {"asrs": "Shuttleworks GmbH", "systems": "Nordflow", "fire": "Sprinkfix", "main": "Shuttleworks GmbH"},
                0.89, "SAT shuttle system", 0.10, 0.62),
    ProjectSpec("DC-03", "Cross-dock hub, Łódź", "Łódź", "PL", "EU", "greenfield", "12,000 m² | 48 doors",
                19e6, "EUR", "fel-v3", "fel2", 29, (-160,), "a.nowak", 4, "Bouwcombinatie Zuid",
                {"dock": "Dockrite", "racking": "Rackline BV", "main": "Dockrite"}, 0.78, "Concept select", 0.10, 0.15),
    ProjectSpec("DC-04", "Cold-chain chamber -25 °C, Valencia", "Valencia", "ES", "EU", "extension",
                "6,500 m² | 9,000 pallets", 15e6, "EUR", "fel-v3", "fel3", 18, (-330, -190), "p.ferrer", 2,
                "Bouwcombinatie Zuid", {"refrigeration": "Frostline", "fire": "Sprinkfix", "main": "Frostline",
                                        "racking": "Rackline BV"}, 0.52, "Gate review", 0.09, 0.28),
    ProjectSpec("DC-05", "Returnables sorting centre, Lima", "Lima", "PE", "LATAM", "greenfield",
                "9,000 m² | 2 sorting lines", 11e6, "USD", "fel-v3", "execute", 31, (-600, -470, -320), "s.quispe", 2,
                "Andina Construcciones", {"main": "Andina Construcciones", "systems": "Nordflow"}, 0.84,
                "Civil works complete", 0.10, 0.55),
    ProjectSpec("DC-06", "Yard automation and weighbridges, Santos", "Santos", "BR", "LATAM", "site_systems",
                "4 weighbridges | 120 trucks/day", 6e6, "USD", "fel-v3", "startup", 9, (-700, -580, -430, -200),
                "d.moraes", 1, "Pesagem Ltda", {"weigh": "Pesagem Ltda", "systems": "Pesagem Ltda", "main": "Pesagem Ltda"},
                0.95, "Handover", 0.08, 0.92),
    ProjectSpec("DC-07", "Pick tower mezzanine, Nairobi", "Nairobi", "KE", "AFR", "brownfield", "3 levels | 5,400 m²",
                8e6, "USD", "riba-2020", "s2", 11, (-300, -170), "w.odhiambo", 5, "Savanna Build",
                {"main": "Savanna Build"}, 0.48, "Concept design", 0.12, 0.18),
    ProjectSpec("DC-08", "ESFR sprinklers and roof PV, Monterrey", "Monterrey", "MX", "LATAM", "compliance",
                "28,000 m² roof | 2.1 MWp", 9e6, "USD", "fel-v3", "execute", 14, (-520, -400, -260), "l.camacho", 2,
                "Solaris MX", {"fire": "Sprinkfix", "pv": "Solaris MX", "main": "Sprinkfix"}, 0.81, "Hydraulic test",
                0.08, 0.36),
    ProjectSpec("DC-09", "Dock expansion and VNA slab, Leuven", "Leuven", "BE", "EU", "brownfield",
                "+14 docks | 8,000 m² slab", 12e6, "EUR", "fel-v3", "fel2", 36, (-180,), "k.peeters", 4,
                "Bouwcombinatie Zuid", {"slab": "SlabTec", "dock": "Dockrite", "racking": "Rackline BV", "main": "SlabTec"},
                0.73, "Concept select", 0.10, 0.20),
    ProjectSpec("DC-10", "Micro-fulfilment, Ho Chi Minh City", "Ho Chi Minh City", "VN", "APAC", "urban",
                "2,800 m² | goods-to-person", 7e6, "USD", "three-gate", "g1", 22, (), "t.nguyen", 4, "Mekong Fit-out",
                {"systems": "Nordflow", "main": "Mekong Fit-out"}, 0.60, "Site decision", 0.10, 0.10),
]

COUNTRY_TZ = {"NL": "Europe/Amsterdam", "FR": "Europe/Paris", "PL": "Europe/Warsaw", "ES": "Europe/Madrid",
              "BE": "Europe/Brussels", "PE": "America/Lima", "BR": "America/Sao_Paulo", "MX": "America/Monterrey",
              "KE": "Africa/Nairobi", "VN": "Asia/Ho_Chi_Minh", "DE": "Europe/Berlin",
              "IT": "Europe/Rome"}


@dataclass(frozen=True)
class PartySpec:
    key: str
    name: str
    kind: str
    country: str
    behaviour: str
    contact: str
    lang: str = "en"


def _p(key: str, name: str, kind: str, country: str, behaviour: str, contact: str, lang: str = "en") -> PartySpec:
    return PartySpec(key, name, kind, country, behaviour, contact, lang)


PARTIES: list[PartySpec] = [
    # firms (14)
    _p("bouwcombinatie", "Bouwcombinatie Zuid", "contractor", "NL", "prompt", "H. Kuipers", "nl"),
    _p("rackline", "Rackline BV", "vendor", "NL", "prompt", "S. de Boer", "nl"),
    _p("shuttleworks", "Shuttleworks GmbH", "vendor", "DE", "flagger", "K. Weber"),
    _p("nordflow", "Nordflow", "vendor", "NL", "slow", "I. Lindqvist"),
    _p("slabtec", "SlabTec", "vendor", "BE", "slow", "G. Claes", "fr"),
    _p("sprinkfix", "Sprinkfix", "vendor", "NL", "prompt", "T. Mulder", "nl"),
    _p("geoterra", "GeoTerra", "consultant", "NL", "prompt", "B. Hendriks", "nl"),
    _p("frostline", "Frostline", "vendor", "ES", "slow", "M. Ortega", "es"),
    _p("pesagem", "Pesagem Ltda", "vendor", "BR", "prompt", "R. Almeida", "pt"),
    _p("solaris", "Solaris MX", "vendor", "MX", "delegating", "J. Treviño", "es"),
    _p("dockrite", "Dockrite", "vendor", "PL", "prompt", "P. Zielinski"),
    _p("andina", "Andina Construcciones", "contractor", "PE", "slow", "C. Huamán", "es"),
    _p("savanna", "Savanna Build", "contractor", "KE", "delegating", "N. Kamau"),
    _p("mekong", "Mekong Fit-out", "contractor", "VN", "prompt", "H. Tran"),
    # internal teams (6)
    _p("pmo", "PMO", "internal_team", "NL", "prompt", "PMO desk"),
    _p("engineering", "Engineering", "internal_team", "NL", "prompt", "Engineering desk"),
    _p("hse", "HSE", "internal_team", "NL", "slow", "R. Bakker"),
    _p("siteops", "Site operations", "internal_team", "NL", "prompt", "Site ops desk"),
    _p("procurement", "Procurement", "internal_team", "NL", "prompt", "A. Serra"),
    _p("finance", "Finance", "internal_team", "NL", "prompt", "L. Jansen"),
    # insurer (1)
    _p("insurer", "Property insurer (FM-type)", "insurer", "NL", "slow", "Risk engineering desk"),
    # municipalities (10)
    _p("muni-NL", "Municipality, Venlo", "authority", "NL", "slow", "Permit desk", "nl"),
    _p("muni-FR", "Municipality, Lyon", "authority", "FR", "slow", "Service urbanisme", "fr"),
    _p("muni-PL", "Municipality, Łódź", "authority", "PL", "prompt", "Permit desk"),
    _p("muni-ES", "Municipality, Valencia", "authority", "ES", "slow", "Licencias", "es"),
    _p("muni-BE", "Municipality, Leuven", "authority", "BE", "slow", "Permit desk", "fr"),
    _p("muni-PE", "Municipality, Lima", "authority", "PE", "silent", "Licencias", "es"),
    _p("muni-BR", "Municipality, Santos", "authority", "BR", "slow", "Alvarás", "pt"),
    _p("muni-MX", "Municipality, Monterrey", "authority", "MX", "slow", "Licencias", "es"),
    _p("muni-KE", "County planning, Nairobi", "authority", "KE", "slow", "Planning desk"),
    _p("muni-VN", "District authority, Ho Chi Minh City", "authority", "VN", "slow", "Permit desk"),
    # environmental agencies (2)
    _p("env-NL", "Environmental agency, Limburg", "authority", "NL", "slow", "Permit desk", "nl"),
    _p("env-ES", "Environmental agency, Valencia", "authority", "ES", "silent", "Medio ambiente", "es"),
    # grid operators (5)
    _p("grid-NL", "Grid operator, NL", "utility", "NL", "slow", "Connections desk", "nl"),
    _p("grid-FR", "Grid operator, FR", "utility", "FR", "slow", "Raccordement", "fr"),
    _p("grid-ES", "Grid operator, ES", "utility", "ES", "slow", "Conexiones", "es"),
    _p("grid-BR", "Grid operator, BR", "utility", "BR", "prompt", "Ligações", "pt"),
    _p("grid-MX", "Grid operator, MX", "utility", "MX", "slow", "Conexiones", "es"),
]

# behaviour -> (median reply days, silence rate, flag share of replies, delegate share)
BEHAVIOUR = {
    "prompt": (1.0, 0.05, 0.08, 0.03),
    "slow": (4.0, 0.15, 0.15, 0.05),
    "silent": (8.0, 0.45, 0.10, 0.03),
    "delegating": (2.0, 0.10, 0.10, 0.40),
    "flagger": (2.0, 0.10, 0.50, 0.03),
}


@dataclass(frozen=True)
class UserSpec:
    key: str
    name: str
    roles: tuple[str, ...]
    tz: str = "Europe/Amsterdam"


USERS: list[UserSpec] = [
    UserSpec("aleks", "A. Sidorecs", ("portfolio_lead", "investment_committee", "admin", "viewer")),
    UserSpec("b.keller", "B. Keller", ("sponsor", "viewer")),
    UserSpec("f.laurent", "F. Laurent", ("project_director", "viewer"), "Europe/Paris"),
    UserSpec("m.devries", "M. de Vries", ("project_manager", "sme:engineering")),
    UserSpec("c.marchand", "C. Marchand", ("project_manager",), "Europe/Paris"),
    UserSpec("a.nowak", "A. Nowak", ("project_manager",), "Europe/Warsaw"),
    UserSpec("p.ferrer", "P. Ferrer", ("project_manager",), "Europe/Madrid"),
    UserSpec("s.quispe", "S. Quispe", ("project_manager",), "America/Lima"),
    UserSpec("d.moraes", "D. Moraes", ("project_manager", "site_lead"), "America/Sao_Paulo"),
    UserSpec("w.odhiambo", "W. Odhiambo", ("project_manager",), "Africa/Nairobi"),
    UserSpec("l.camacho", "L. Camacho", ("project_manager",), "America/Monterrey"),
    UserSpec("k.peeters", "K. Peeters", ("project_manager",), "Europe/Brussels"),
    UserSpec("t.nguyen", "T. Nguyen", ("project_manager",), "Asia/Ho_Chi_Minh"),
    UserSpec("j.smits", "J. Smits", ("project_manager",)),
    UserSpec("r.bakker", "R. Bakker", ("sme:hse",)),
    UserSpec("e.visser", "E. Visser", ("sme:systems", "sme:operations")),
    UserSpec("l.jansen", "L. Jansen", ("sme:finance", "project_manager")),
    UserSpec("a.serra", "A. Serra", ("sme:procurement",)),
    UserSpec("g.okoro", "G. Okoro", ("sme:engineering", "sme:it", "sme:legal", "sme:hr")),
    UserSpec("h.vos", "H. Vos", ("site_lead", "sme:operations")),
]

PACKAGES: dict[str, list[tuple[str, str, str]]] = {
    # discipline, name, vendor tag
    "greenfield": [("civil", "Civil and structure", "gc"), ("slab", "Industrial slab", "slab"),
                   ("racking", "Racking and MHE", "racking"), ("systems", "WMS / WCS", "systems"),
                   ("fire", "Fire protection", "fire"), ("utilities", "Utilities and grid", "gc"),
                   ("dock", "Dock equipment", "dock")],
    "brownfield": [("civil", "Civil and strip-out", "gc"), ("racking", "Racking and MHE", "racking"),
                   ("systems", "WMS / WCS", "systems"), ("fire", "Fire protection", "fire"),
                   ("asrs", "ASRS", "asrs"), ("slab", "Slab works", "slab"), ("dock", "Dock equipment", "dock")],
    "extension": [("civil", "Civil and envelope", "gc"), ("refrigeration", "Refrigeration", "refrigeration"),
                  ("fire", "Fire protection", "fire"), ("racking", "Racking", "racking"),
                  ("utilities", "Utilities", "gc")],
    "site_systems": [("civil", "Civil works", "gc"), ("yard", "Yard system", "systems"),
                     ("weigh", "Weighbridges", "weigh")],
    "compliance": [("fire", "ESFR sprinklers", "fire"), ("pv", "Roof PV", "pv"), ("civil", "Roof works", "gc")],
    "urban": [("fitout", "Fit-out", "gc"), ("systems", "Goods-to-person system", "systems")],
}

LESSONS = [
    ("L-041", "Submit roof layouts to the insurer before the sprinkler design",
     "On DC-08 the insurer would not review the ESFR design until the roof PV layout was submitted. A separate early "
     "submission of the roof layout would have saved three weeks.", "DC-08", ["fire", "insurer", "pv"]),
    ("L-017", "Slab flatness is a gate item, not a construction item",
     "On DC-09 the VNA slab failed flatness at SAT. The flatness specification (DIN 15185 / TR34 DM) must be accepted "
     "at concept or definition, and the MHE vendor must sign it before the slab is poured.", "DC-09", ["slab", "vna", "mhe"]),
    ("L-022", "Grid connection offers take longer than the schedule says",
     "On DC-01 the grid operator confirmed capacity eight weeks late; a genset was hired as mitigation.", "DC-01",
     ["utilities", "grid"]),
    ("L-030", "Change orders after FEL 3 move the estimate class",
     "On DC-03 a dock count change after the definition gate reopened the estimate.", "DC-03", ["estimate", "dock"]),
    ("L-012", "One HSE reviewer cannot carry three FEL 3 gates in one month",
     "Shared reviewers are a dependency; book their review slots at gate planning.", None, ["hse", "people"]),
    ("L-008", "Start the operational readiness plan from a previous site's template",
     "DC-06 reused a ramp-up template and cut preparation time by half.", "DC-06", ["ops"]),
]


@dataclass
class DemoConfig:
    seed: int = 7
    cases: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
    target_commitments: int = 1412
    target_inbound: int = 9800
    corpus_size: int = 2000
    priors_size: int = 600
    extra: dict[str, object] = field(default_factory=dict)


# Portfolio hooks read by the generator (gw/demo/portfolio_integration.py defines the same names).
NAME = "dc"
FLOAT_NOW: dict[str, float] = {"DC-01": 4, "DC-02": -6, "DC-04": 2, "DC-05": 3}
PLANNER_PROJECT = "DC-01"
HAS_SCHEDULES = True
CASES: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
TARGET_COMMITMENTS = 1412
TARGET_INBOUND = 9800
CORPUS_SIZE = 2000
PRIORS_SIZE = 600
