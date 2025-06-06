from __future__ import annotations

import typing as t

import numpy as np
from fastapi import APIRouter, Depends
from sqlglot import exp

from sqlmesh.core.context import Context
from web.server.models import RowDiff, SchemaDiff, TableDiff
from web.server.settings import get_loaded_context

router = APIRouter()


@router.get("")
def get_table_diff(
    source: str,
    target: str,
    on: t.Optional[str] = None,
    model_or_snapshot: t.Optional[str] = None,
    where: t.Optional[str] = None,
    temp_schema: t.Optional[str] = None,
    limit: int = 20,
    context: Context = Depends(get_loaded_context),
) -> t.Optional[TableDiff]:
    """Calculate differences between tables, taking into account schema and row level differences."""
    table_diffs = context.table_diff(
        source=source,
        target=target,
        on=exp.condition(on) if on else None,
        select_models={model_or_snapshot} if model_or_snapshot else None,
        where=where,
        limit=limit,
        show=False,
    )

    if not table_diffs:
        return None
    diff = table_diffs[0] if isinstance(table_diffs, list) else table_diffs

    _schema_diff = diff.schema_diff()
    _row_diff = diff.row_diff(temp_schema=temp_schema)
    schema_diff = SchemaDiff(
        source=_schema_diff.source,
        target=_schema_diff.target,
        source_schema=_schema_diff.source_schema,
        target_schema=_schema_diff.target_schema,
        added=_schema_diff.added,
        removed=_schema_diff.removed,
        modified=_schema_diff.modified,
    )
    row_diff = RowDiff(
        source=_row_diff.source,
        target=_row_diff.target,
        stats=_row_diff.stats,
        sample=_row_diff.sample.replace({np.nan: None}).to_dict(),
        source_count=_row_diff.source_count,
        target_count=_row_diff.target_count,
        count_pct_change=_row_diff.count_pct_change,
    )

    s_index, t_index, _ = diff.key_columns
    return TableDiff(
        schema_diff=schema_diff,
        row_diff=row_diff,
        on=[(s.name, t.name) for s, t in zip(s_index, t_index)],
    )
