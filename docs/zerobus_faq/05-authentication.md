# What authentication methods does Zerobus support?

Zerobus uses **OAuth** (not user PATs). There are two common patterns:

## Service principal (CI / production — recommended)

Configure a service principal with OAuth M2M credentials and set the following environment variables:

```bash
export DATABRICKS_CLIENT_ID="<service-principal-application-id>"
export DATABRICKS_CLIENT_SECRET="<service-principal-secret>"
export ZEROBUS_SERVER_ENDPOINT="https://<workspace_id>.zerobus.<region>.cloud.databricks.com"
export DATABRICKS_WORKSPACE_URL="https://<workspace>.cloud.databricks.com"
export ZEROBUS_TABLE_NAME="catalog.schema.table"
```

## SDK / Databricks CLI auth (local development)

If you have a valid `~/.databrickscfg` profile (from Databricks Connect setup), you can use `IngestConfig.from_workspace_client()`. This derives a short-lived token from your existing workspace credentials without needing a separate service principal.

```python
from src.zerobus_ingest import IngestConfig, build_qualified_table_name

table = build_qualified_table_name("my_table")   # e.g. main.robert_lee.my_table
config = IngestConfig.from_workspace_client(table, lifetime_seconds=300)
```

## Required permissions for the service principal

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<service-principal-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<service-principal-id>`;
GRANT MODIFY, SELECT ON TABLE <catalog>.<schema>.<table> TO `<service-principal-id>`;
```

---

*[← Back to FAQ index](../zerobus_faq.md)*
