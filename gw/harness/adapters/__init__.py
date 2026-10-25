"""Adapter modules; importing this package registers every adapter by name."""

from gw.harness.adapters import (  # noqa: F401
    cloud_schedules,
    cost_xlsx,
    fakes,
    graph,
    llm,
    local,
    models,
    oidc,  # noqa: F401,E402
    pm_tools,
    s3,
    schedule_files,
    schedule_mcp,
    slack,
    tabular,
)
