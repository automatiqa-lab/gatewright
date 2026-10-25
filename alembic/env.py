"""Alembic environment. Targets DuckDB (solo) and Postgres (team) from the same metadata.


"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic.ddl.impl import DefaultImpl

from alembic import context
from gw.record.tables import metadata


class DuckDBImpl(DefaultImpl):
    """Alembic has no DuckDB dialect; DuckDB accepts the default (Postgres-like) DDL."""

    __dialect__ = "duckdb"
    transactional_ddl = True

db = os.environ.get("GW_DB", "gw.duckdb")
url = db if "://" in db else "duckdb:///" + Path(db).as_posix()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
