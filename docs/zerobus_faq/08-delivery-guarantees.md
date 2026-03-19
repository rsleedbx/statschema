# What delivery guarantees does Zerobus provide?

Zerobus provides **at-least-once** delivery guarantees only. There is no exactly-once guarantee.

- If your client retries after a failure before receiving an acknowledgment, duplicate records may be written to the target table.
- To detect or remove duplicates downstream, include a unique record ID column in your schema and deduplicate using `MERGE` or a streaming dedup pattern in Databricks.

---

*[← Back to FAQ index](../zerobus_faq.md)*
