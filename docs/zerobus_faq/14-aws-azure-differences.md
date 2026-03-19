# Are there differences between AWS and Azure deployments?

*Sources: [AWS limitations](https://docs.databricks.com/aws/en/ingestion/zerobus-limits) · [Azure limitations](https://learn.microsoft.com/en-us/azure/databricks/ingestion/zerobus-limits) · [AWS overview](https://docs.databricks.com/aws/en/ingestion/zerobus-overview) · [Azure overview](https://learn.microsoft.com/en-us/azure/databricks/ingestion/zerobus-overview) (all last updated March 2026)*

Yes — there are several cloud-specific differences. The functional behaviour (latency, throughput, schema rules, delivery guarantees) is identical, but URLs, regions, and billing differ.

## Endpoint and workspace URL format

The domain suffix is different on each cloud. This affects both the workspace URL you pass to the SDK and the ZeroBus server endpoint you connect to:

| | AWS | Azure |
|---|---|---|
| **Workspace URL** | `https://dbc-a1b2c3d4-e5f6.cloud.databricks.com` | `https://adb-1234567890123456.12.azuredatabricks.net` |
| **ZeroBus endpoint** | `https://<workspace_id>.zerobus.<region>.cloud.databricks.com` | `https://<workspace_id>.zerobus.<region>.azuredatabricks.net` |

```
AWS endpoint example:
  https://2281745829657864.zerobus.us-east-1.cloud.databricks.com

Azure endpoint example:
  https://2281745829657864.zerobus.eastus.azuredatabricks.net
```

## Available regions

AWS and Azure have entirely separate region lists. Both must satisfy the same rule: workspace and table storage must be in the same region.

**AWS — 13 regions** (all multi-zonal):

`us-east-1` · `us-east-2` · `us-west-2` · `ca-central-1` · `sa-east-1` · `ap-south-1` · `ap-southeast-1` · `ap-southeast-2` · `ap-northeast-1` · `ap-northeast-2` · `eu-central-1` · `eu-west-1` · `eu-west-2`

**Azure — 19 regions** (17 multi-zonal, 2 single-AZ ⚠️):

`eastus` · `eastus2` · `westus2` · `westus3` · `centralus` · `southcentralus` · `canadacentral` · `brazilsouth` · `westeurope` · `northeurope` · `germanywestcentral` · `swedencentral` · `switzerlandnorth` · `uksouth` · `australiaeast` · `centralindia` · `southeastasia`

⚠️ **Single-AZ only on Azure** (no redundancy across availability zones):
- `westus`
- `northcentralus`

If high availability is a requirement, avoid these two Azure regions.

## Billing SKU

| Cloud | SKU name |
|---|---|
| AWS | `"Jobs Serverless"` |
| Azure | `"Automated Serverless"` |

These different names are normal — they reflect Azure's unified billing taxonomy (`Automated`, `Interactive`, etc.) vs Databricks' own naming on AWS (`Jobs`, `Notebooks`, etc.). The underlying compute and pricing rates are equivalent.

The billing filter for the usage system table is the same on both clouds:

```sql
WHERE billing_origin_product = 'LAKEFLOW_CONNECT'
  AND product_features.lakeflow_connect.zerobus_request_type IN ('GRPC', 'HTTP')
```

## What is identical across both clouds

The following are the same on AWS and Azure:

| Area | Value |
|---|---|
| Latency SLAs | P50 ≤ 200ms durability, P50 ≤ 5s to table |
| Throughput limits | 100 MB/s per stream, 10 GB/s per table, 15K records/s per stream |
| Delivery guarantee | At-least-once only |
| Protobuf schema rules | proto2, optional fields, 1:1 schema match, max 2000 columns |
| Type mappings | Identical Delta ↔ Protobuf type table |
| Record size limit | 10 MB max |
| Schema evolution | Non-breaking (nullable add) only; never auto-evolves |
| Private endpoint | Not supported on either cloud |
| Catalog-managed commits | Not supported on either cloud |
| Durable fallback path | `_zerobus/table_rejected_parquets/` on both |
| Compliance note | "Additional capabilities not yet available" on both |

---

*[← Back to FAQ index](../zerobus_faq.md)*
