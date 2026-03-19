# How do I create a Service Principal for ZeroBus authentication?

`client_id` and `client_secret` are OAuth M2M credentials for a Databricks Service Principal. Creating them requires workspace admin.

> Replace `DEFAULT` with your profile name if using a named profile in `~/.databrickscfg`.

---

## Step 1 — Create the Service Principal

```bash
SP_JSON=$(databricks service-principals create \
  --display-name "my-zerobus-sp" \
  --json '{}' \
  --profile DEFAULT \
  --output json)

CLIENT_ID=$(echo "$SP_JSON" | jq -r '.applicationId')
SP_ID=$(echo "$SP_JSON" | jq -r '.id')

echo "client_id: $CLIENT_ID"
echo "numeric id (needed for step 2): $SP_ID"
```

`applicationId` is a UUID — this is `DATABRICKS_CLIENT_ID`.
`id` is a numeric value used only to create the secret in step 2.

---

## Step 2 — Create the secret

```bash
CLIENT_SECRET=$(databricks service-principal-secrets-proxy create $SP_ID \
  --profile DEFAULT \
  --output json | jq -r '.secret')

echo "client_secret: $CLIENT_SECRET"
```

The `secret` value is returned once. Store it before closing the terminal.

---

## Combined (steps 1 and 2 together)

```bash
SP_JSON=$(databricks service-principals create \
  --display-name "my-zerobus-sp" \
  --json '{}' \
  --profile DEFAULT \
  --output json)

CLIENT_ID=$(echo "$SP_JSON" | jq -r '.applicationId')
SP_ID=$(echo "$SP_JSON" | jq -r '.id')

CLIENT_SECRET=$(databricks service-principal-secrets-proxy create $SP_ID \
  --profile DEFAULT \
  --output json | jq -r '.secret')

echo "export DATABRICKS_CLIENT_ID=$CLIENT_ID"
echo "export DATABRICKS_CLIENT_SECRET=$CLIENT_SECRET"
```

---

## Grant table permissions

The target table must exist before permissions can be granted. If the schema or table does not exist yet, create them first:

```sql
CREATE SCHEMA IF NOT EXISTS <catalog.schema>;
CREATE TABLE IF NOT EXISTS <catalog.schema.table> (
  -- columns matching your ingest schema
);
```

Then grant `MODIFY` and `SELECT` to the Service Principal:

**CLI:**
```bash
databricks grants update TABLE <catalog.schema.table> \
  --profile DEFAULT \
  --json '{
    "changes": [{
      "principal": "<applicationId>",
      "add": ["MODIFY", "SELECT"]
    }]
  }'
```

**SQL:**
```sql
GRANT MODIFY, SELECT ON TABLE <catalog.schema.table>
  TO `<applicationId>`;
```

---

## Export for ZeroBus

```bash
export DATABRICKS_CLIENT_ID="<applicationId from step 1>"
export DATABRICKS_CLIENT_SECRET="<secret from step 2>"
```

These are the values passed to `ZerobusSdk.create_stream(client_id, client_secret, ...)` or read by `IngestConfig.from_env()`.

*[← Back to FAQ index](../zerobus_faq.md)*
