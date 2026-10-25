# CostSource CSV schema v1 (`csv_drop` adapter)

UTF-8, comma-separated, header row, one file per export, file name
`cost_<YYYYMMDD>.csv`. Rows are appended; the adapter dedupes on
`(project_code, wbs, kind, posted_at, amount)`.

| column | type | notes |
|---|---|---|
| project_code | string | matches `Project.code` (DC-01) |
| wbs | string | work package code or WBS path |
| kind | enum | `commitment` (PO / contract), `actual` (invoice / accrual), `contingency_draw`, `estimate` (current estimate at posting) |
| amount | decimal | positive; credits as negative |
| currency | ISO 4217 | EUR, USD, … |
| posted_at | date | ISO 8601 |
| ref | string | PO / invoice / change order number (optional) |
| note | string | optional |
