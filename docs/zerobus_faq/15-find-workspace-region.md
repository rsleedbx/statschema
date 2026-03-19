# How do I find my workspace region to construct the Zerobus endpoint?

Two values are needed to construct the endpoint:

```
https://<workspace_id>.zerobus.<region>.<cloud-domain>
         ────────────           ──────
              │                    │
              └─ (1) workspace_id  └─ (2) region
```

Select your cloud: [AWS](#aws) · [Azure](#azure)

> Replace `DEFAULT` with your profile name if using a named profile in `~/.databrickscfg`.

---

## AWS

**Endpoint format:**
```
https://<workspace_id>.zerobus.<region>.cloud.databricks.com
```

**1. Get the workspace ID**

**CLI:**
```bash
databricks auth env --profile DEFAULT | jq -r '.env.DATABRICKS_HOST'
# workspace_id is in the ?o= query parameter when accessed via browser
```

From the browser URL after `?o=`:
```
https://dbc-a1b2c3d4.cloud.databricks.com/?o=2281745829657864
                                              ────────────────
                                              workspace_id
```

**Python SDK:**
```bash
python3 -c 'from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile="DEFAULT").get_workspace_id())'
```

**2. Get the region**

**CLI:**
```bash
databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone[:-1]'
```

Example response:
```json
{
  "default_zone": "us-west-2a",
  "zones": ["us-west-2a", "us-west-2b"]
}
```

`default_zone` is an availability zone (`us-west-2a`); the region is the value without the trailing letter (`us-west-2`). The commands above handle this.

**Python SDK:**
```bash
python3 -c 'from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile="DEFAULT").clusters.list_zones().default_zone[:-1])'
```

**3. Construct the endpoint**

The workspace ID is in the `X-Databricks-Org-Id` response header, which the CLI does not expose. `python3` is used for that value only; the region uses the CLI.

**CLI:**
```bash
REGION=$(databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone[:-1]')
WORKSPACE_ID=$(python3 -c 'from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile="DEFAULT").get_workspace_id())')
WORKSPACE_URL=$(databricks auth env --profile DEFAULT | jq -r '.env.DATABRICKS_HOST')
ZEROBUS_ENDPOINT="https://${WORKSPACE_ID}.zerobus.${REGION}.cloud.databricks.com"
echo "WORKSPACE_URL=$WORKSPACE_URL"
echo "ZEROBUS_ENDPOINT=$ZEROBUS_ENDPOINT"
```

**Python SDK:**
```bash
python3 << 'EOF'
from databricks.sdk import WorkspaceClient

w = WorkspaceClient(profile="DEFAULT")
workspace_id = w.get_workspace_id()
workspace_url = w.config.host
region = w.clusters.list_zones().default_zone[:-1]
zerobus_endpoint = f"https://{workspace_id}.zerobus.{region}.cloud.databricks.com"
print(f"WORKSPACE_URL={workspace_url}")
print(f"ZEROBUS_ENDPOINT={zerobus_endpoint}")
EOF
```

---

## Azure

**Endpoint format:**
```
https://<workspace_id>.zerobus.<region>.azuredatabricks.net
```

**1. Get the workspace ID**

The workspace ID is the digits between `adb-` and the first `.` in the hostname:
```
Hostname: adb-1234567890123456.12.azuredatabricks.net
               ────────────────
               workspace_id = 1234567890123456
```

**CLI:**
```bash
WORKSPACE_ID=$(databricks auth env --profile DEFAULT \
  | jq -r '.env.DATABRICKS_HOST' \
  | sed 's/.*adb-\([0-9]*\)\..*/\1/')
echo $WORKSPACE_ID
```

**Python SDK:**
```bash
python3 -c 'from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile="DEFAULT").get_workspace_id())'
```

**2. Get the region**

**CLI:**
```bash
databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone'
```

Example response:
```json
{
  "default_zone": "eastus",
  "zones": ["eastus"]
}
```

The `default_zone` value is the region.

**Python SDK:**
```bash
python3 -c 'from databricks.sdk import WorkspaceClient; print(WorkspaceClient(profile="DEFAULT").clusters.list_zones().default_zone)'
```

**3. Construct the endpoint**

**CLI:**
```bash
REGION=$(databricks api get /api/2.0/clusters/list-zones --profile DEFAULT | jq -r '.default_zone')
WORKSPACE_URL=$(databricks auth env --profile DEFAULT | jq -r '.env.DATABRICKS_HOST')
WORKSPACE_ID=$(echo $WORKSPACE_URL | sed 's/.*adb-\([0-9]*\)\..*/\1/')
ZEROBUS_ENDPOINT="https://${WORKSPACE_ID}.zerobus.${REGION}.azuredatabricks.net"
echo "WORKSPACE_URL=$WORKSPACE_URL"
echo "ZEROBUS_ENDPOINT=$ZEROBUS_ENDPOINT"
```

**Python SDK:**
```bash
python3 << 'EOF'
from databricks.sdk import WorkspaceClient

w = WorkspaceClient(profile="DEFAULT")
workspace_id = w.get_workspace_id()
workspace_url = w.config.host
region = w.clusters.list_zones().default_zone
zerobus_endpoint = f"https://{workspace_id}.zerobus.{region}.azuredatabricks.net"
print(f"WORKSPACE_URL={workspace_url}")
print(f"ZEROBUS_ENDPOINT={zerobus_endpoint}")
EOF
```

---

## Alternative methods (all clouds)

### Databricks account console

1. Log in to [accounts.cloud.databricks.com](https://accounts.cloud.databricks.com)
2. Navigate to **Workspaces**
3. Click on your workspace — the region is shown in the workspace details

### SQL

```sql
DESCRIBE METASTORE;
-- Look for the 'region' field in the output
```

---

*Source: [Get your workspace URL and Zerobus Ingest endpoint](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest#get-your-workspace-url-and-zerobus-ingest-endpoint) (last updated March 10, 2026)*

*[← Back to FAQ index](../zerobus_faq.md)*
