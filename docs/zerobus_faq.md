# Zerobus Ingest FAQ

Each question links to its own `.md` file so individual sections can be shared directly.

---

## Getting started

1. [What is Zerobus Ingest and how does it work?](zerobus_faq/01-what-is-zerobus.md)
2. [Does Zerobus automatically create the target table?](zerobus_faq/04-table-auto-creation.md)
3. [What authentication methods does Zerobus support?](zerobus_faq/05-authentication.md)
4. [How do I create a Service Principal (client\_id and client\_secret) for ZeroBus?](zerobus_faq/16-create-service-principal.md)
5. [What serialization formats does Zerobus support?](zerobus_faq/06-serialization-formats.md)

## Performance

6. [What are the throughput and latency characteristics?](zerobus_faq/07-throughput-latency.md)
7. [What delivery guarantees does Zerobus provide?](zerobus_faq/08-delivery-guarantees.md)
8. [Does Zerobus support schema evolution?](zerobus_faq/11-schema-evolution.md)

## Operations & monitoring

9. [What happens to data that Zerobus cannot write to the table?](zerobus_faq/09-rejected-data.md)
10. [How do I monitor Zerobus ingest?](zerobus_faq/10-monitoring.md)

## Security & compliance

11. [Does Zerobus store data in temporary storage where Customer-Managed Keys (CMK) apply?](zerobus_faq/02-cmk-temporary-storage.md)
12. [Does Zerobus support private endpoint or private link storage?](zerobus_faq/03-private-endpoint.md)

## Infrastructure & regions

13. [Why must the workspace and target table be in the same region?](zerobus_faq/13-same-region-requirement.md)
14. [How do I find my workspace region to construct the Zerobus endpoint?](zerobus_faq/15-find-workspace-region.md)

## Cloud differences

15. [Are there differences between AWS and Azure deployments?](zerobus_faq/14-aws-azure-differences.md)

## Limitations

16. [What are the key limitations to be aware of?](zerobus_faq/12-key-limitations.md)

---

*References: [Zerobus overview](https://docs.databricks.com/aws/en/ingestion/zerobus-overview) · [Zerobus connector usage](https://docs.databricks.com/aws/en/ingestion/zerobus-ingest) · [Zerobus limitations](https://docs.databricks.com/aws/en/ingestion/zerobus-limits) · [Zerobus system tables](https://docs.databricks.com/aws/en/admin/system-tables/zerobus-ingest)*
