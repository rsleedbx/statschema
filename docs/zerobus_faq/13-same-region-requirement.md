# Why must the workspace and target table be in the same region?

*Source: [Zerobus Ingest connector limitations — Workspace and Target table](https://docs.databricks.com/aws/en/ingestion/zerobus-limits#workspace-and-target-table) (last updated March 4, 2026)*

The [limitations page](https://docs.databricks.com/aws/en/ingestion/zerobus-limits#workspace-and-target-table) states:

> *"Both the workspace and the target table need to be in one of the available regions, and both in the same region."*

## What is the "workspace" in this context?

A Databricks workspace is your logical environment (notebooks, jobs, Unity Catalog metastore, authentication) deployed into a specific AWS region. The ZeroBus server endpoint is hosted as part of that workspace's serverless infrastructure **in that same region**. The endpoint URL encodes the region directly:

```
https://<workspace_id>.zerobus.<region>.cloud.databricks.com
```

## What is the "target table" region?

A Unity Catalog managed Delta table stores its actual files (Parquet data + Delta log) in a cloud storage bucket (S3). That S3 bucket is in a specific AWS region.

## Region co-location requirement — pictorial view

```
✅  VALID — workspace and table storage in the same region

        AWS Region: us-east-1
        ┌─────────────────────────────────────────────────────┐
        │                                                     │
        │  Databricks Workspace (us-east-1)                   │
        │  ┌──────────────────────────────┐                   │
        │  │  Unity Catalog               │                   │
        │  │  (table metadata/governance) │                   │
        │  │                              │                   │
        │  │  ZeroBus Server              │──────────────────►│  S3 bucket (us-east-1)
        │  │  (serverless, same region)   │  writes Delta     │  catalog.schema.my_table
        │  │                              │  files locally    │  /_delta_log/
        │  │  OAuth / Service Principal   │                   │  /part-0001.parquet
        │  └──────────────────────────────┘                   │
        │              ▲                                      │
        └──────────────┼──────────────────────────────────────┘
                       │ gRPC stream
                  Client app
                  (best: also us-east-1)


❌  INVALID — workspace and table storage in different regions

        AWS Region: us-east-1                AWS Region: us-west-2
        ┌───────────────────────┐            ┌──────────────────────┐
        │ Databricks Workspace  │            │  S3 bucket           │
        │ ZeroBus Server        │──── ✗ ────►│  catalog.schema.tbl  │
        │ (us-east-1)           │  blocked   │  (us-west-2)         │
        └───────────────────────┘            └──────────────────────┘
```

## What the workspace provides in the Zerobus flow

| Role | What it provides |
|---|---|
| **ZeroBus server endpoint** | The serverless ZeroBus process runs inside the workspace's regional control plane |
| **Unity Catalog** | Governs the target table — holds its schema, location, and access policies; ZeroBus validates incoming records against it |
| **Authentication** | OAuth service principal tokens are validated by the workspace identity service |
| **Storage proximity** | The workspace's serverless compute writes Delta files directly to the S3 bucket in the same region |

## Correct endpoint selection

*Source: [Get your workspace URL and Zerobus Ingest endpoint](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest#get-your-workspace-url-and-zerobus-ingest-endpoint) (last updated March 1, 2026)*

The endpoint URL encodes both the **workspace identity** and the **region**. Using an endpoint from a different region than the table's storage will cause ingestion to fail.

```
https://<workspace_id>.zerobus.<region>.cloud.databricks.com
         ──────────────          ──────
              │                    │
              │                    └─ must match the region of
              │                       your table's S3 bucket
              │
              └─ derived from your workspace URL:
                 Full URL:      https://abcd-test.cloud.databricks.com/?o=2281745829657864
                 Workspace URL: https://abcd-test.cloud.databricks.com
                 Workspace ID:  2281745829657864
```

## Practical check before using Zerobus

1. Find your **workspace ID** from the browser URL after `?o=` when logged into Databricks.
2. Find your **workspace region** — see [15-find-workspace-region.md](15-find-workspace-region.md) for methods.
3. Confirm that region matches the **AWS region of the S3 bucket** where your Unity Catalog managed table is stored.
4. Construct your endpoint: `https://<workspace_id>.zerobus.<region>.cloud.databricks.com`

If the region in the endpoint does not match the region of your table storage, ingestion will fail.

---

*[← Back to FAQ index](../zerobus_faq.md)*
