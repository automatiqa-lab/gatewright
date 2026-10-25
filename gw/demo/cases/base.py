"""Base script: DC-01 FEL 3 as on sheet GW-200 (readiness 62.5 / 95 = 0.658)."""

from __future__ import annotations

from gw.demo.cases._util import script
from gw.demo.generator import Gen

DC01 = {
    "geotech_foundation": dict(status="accepted", due=-3, owner="m.devries", party="geoterra"),
    "building_permit": dict(status="accepted", due=1, owner="j.smits", party="muni-NL"),
    "environmental_permit": dict(status="accepted", due=1, owner="j.smits", party="env-NL"),
    "fire_strategy": dict(status="", force_status="open", due=7, owner="r.bakker", party="insurer"),
    "racking_mhe_spec": dict(status="accepted", due=4, owner="m.devries", party="rackline"),
    "slab_flatness_spec": dict(status="submitted", due=8, owner="m.devries", party="slabtec", submitted_days_ago=1),
    "wms_wcs_design": dict(status="accepted", due=8, owner="e.visser", party="nordflow"),
    "grid_connection": dict(status="accepted", due=11, owner="j.smits", party="grid-NL"),
    "estimate_class2": dict(status="accepted", due=8, owner="l.jansen", party="finance"),
    "schedule_dcma": dict(status="submitted", due=8, owner="m.devries", party="pmo", submitted_days_ago=2),
    "contracting_strategy": dict(status="waived", due=5, owner="a.serra", party="procurement"),
    "ops_readiness_plan": dict(status="", force_status="open", due=12, owner="e.visser", party="siteops"),
}


def prepare(g: Gen) -> None:
    for d, kw in DC01.items():
        script(g, "DC-01", d, **kw)
    g.dcma_fail["DC-01"] = ["leads", "hard_constraints"]
    g.dcma_fail["DC-07"] = ["logic", "high_float", "hard_constraints"]
    g.float_change.setdefault("DC-01", -6.0)
