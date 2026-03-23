# statschema — documentation map

All project documentation, grouped by topic.  
Root entry point: [`README.md`](../README.md)

---

```
README.md
├── Data generation
│   ├── docs/dba-yaml-guide.md          DBA quick-start: YAML file → three commands
│   ├── docs/canonical_yaml.md          Canonical schema YAML feature reference
│   │   ├── docs/canonical_yaml/01-core-structure.md
│   │   ├── docs/canonical_yaml/02-column-types.md
│   │   ├── docs/canonical_yaml/03-column-constraints.md
│   │   ├── docs/canonical_yaml/04-column-precision-length.md
│   │   ├── docs/canonical_yaml/05-foreign-keys.md
│   │   ├── docs/canonical_yaml/06-generation-rules.md
│   │   ├── docs/canonical_yaml/07-temporal-ordering.md
│   │   ├── docs/canonical_yaml/08-multi-instance-expansion.md
│   │   └── docs/canonical_yaml/09-round-trip-guarantee.md
│   ├── docs/implementation.md          Project structure and data-generation wiring
│   ├── docs/synthetic_data_shortcomings.md
│   └── docs/dbldatagen.md              dbldatagen v0/v1 integration notes
│
├── Benchmarks
│   ├── benchmarks/README.md            TPC load benchmark guide
│   └── benchmarks/results/benchmark_table.md
│
├── Stats transpiler
│   └── docs/stats_transpiler.md        Per-engine workarounds and function reference
│
├── Testing
│   ├── docs/testing.md                 Local test setup, credentials, Spark/Java config
│   ├── docs/test_plan_ddl_roundtrip.md DDL round-trip test plan
│   ├── docs/learnings/README.md        Learnings index
│   └── docs/learnings/oltp-migration-analysis.md
│
├── Database setup
│   ├── docs/local-databases.md         Index of all local database setup guides
│   ├── docs/databases/postgres.md      Podman, native ARM64
│   ├── docs/databases/neon.md          Podman + Neon Local cloud proxy
│   ├── docs/databases/cockroachdb.md   Podman, native ARM64 (single-node + multi-region)
│   ├── docs/databases/mysql.md         Podman, native ARM64
│   ├── docs/databases/mariadb.md       Podman, native ARM64
│   ├── docs/databases/sqlserver.md     Lima VM + QEMU (x86_64)
│   ├── docs/databases/oracle.md        Lima VM + Podman + QEMU (x86_64)
│   ├── docs/databases/oracle-hr.md     Oracle HR/CO sample schemas
│   ├── docs/databases/db2.md           Lima VM + Podman + QEMU (x86_64)
│   ├── docs/databases/adventureworks.md  SQL Server application-level testing
│   ├── docs/databases/chinook.md       SQL Server application-level testing
│   ├── docs/databases/mautic.md        MySQL application-level testing
│   ├── docs/databases/gitea.md         PostgreSQL application-level testing
│   ├── docs/faq/README.md              FAQ index — local database tooling
│   ├── docs/faq/17-what-is-gvenzl-oracle-xe.md
│   └── docs/faq/18-podman-machine-on-macos.md
│
└── Contributing & project
    ├── docs/adding-a-database.md       Contributor guide: add a new engine end-to-end
    ├── docs/PLAN.md                    Architecture and implementation notes
    ├── docs/ROADMAP.md                 Roadmap and planned work
    └── docs/git-submodules.md          Git submodule workflow
```

---

## Data generation

| Document | Contents |
|----------|----------|
| [`docs/dba-yaml-guide.md`](dba-yaml-guide.md) | DBA quick-start: write one YAML file, run three commands, no Python required |
| [`docs/canonical_yaml.md`](canonical_yaml.md) | Canonical schema YAML — feature reference (overview page) |
| [`docs/canonical_yaml/01-core-structure.md`](canonical_yaml/01-core-structure.md) | Core structure: `name`, `columns`, `primary_key` |
| [`docs/canonical_yaml/02-column-types.md`](canonical_yaml/02-column-types.md) | Column types and type mapping |
| [`docs/canonical_yaml/03-column-constraints.md`](canonical_yaml/03-column-constraints.md) | Column constraints: `nullable`, `unique`, `default` |
| [`docs/canonical_yaml/04-column-precision-length.md`](canonical_yaml/04-column-precision-length.md) | Precision and length for numeric/string columns |
| [`docs/canonical_yaml/05-foreign-keys.md`](canonical_yaml/05-foreign-keys.md) | Foreign key declarations and referential integrity |
| [`docs/canonical_yaml/06-generation-rules.md`](canonical_yaml/06-generation-rules.md) | `generation` block: distributions, patterns, value lists |
| [`docs/canonical_yaml/07-temporal-ordering.md`](canonical_yaml/07-temporal-ordering.md) | Temporal ordering and `depends_on` for time-series data |
| [`docs/canonical_yaml/08-multi-instance-expansion.md`](canonical_yaml/08-multi-instance-expansion.md) | Multi-instance expansion for replicated table patterns |
| [`docs/canonical_yaml/09-round-trip-guarantee.md`](canonical_yaml/09-round-trip-guarantee.md) | Round-trip guarantee: parse → emit → parse stability |
| [`docs/implementation.md`](implementation.md) | Project structure, schema sources, and data-generation wiring |
| [`docs/synthetic_data_shortcomings.md`](synthetic_data_shortcomings.md) | Known limitations of synthetic data generation and mitigations |
| [`docs/dbldatagen.md`](dbldatagen.md) | dbldatagen v0/v1 integration notes |

## Benchmarks

| Document | Contents |
|----------|----------|
| [`benchmarks/README.md`](../benchmarks/README.md) | TPC load benchmark guide: how to run, scale factors, output |
| [`benchmarks/results/benchmark_table.md`](../benchmarks/results/benchmark_table.md) | Latest benchmark results (rows/sec by engine and SF) |

## Stats transpiler

| Document | Contents |
|----------|----------|
| [`docs/stats_transpiler.md`](stats_transpiler.md) | Full details, per-engine workarounds, and function reference |

## Testing

| Document | Contents |
|----------|----------|
| [`docs/testing.md`](testing.md) | Local test setup, `.env` credentials, Spark/Java config, live-DB setup |
| [`docs/test_plan_ddl_roundtrip.md`](test_plan_ddl_roundtrip.md) | Complete DDL round-trip test plan (all types, boundaries, constraints) |
| [`docs/learnings/README.md`](learnings/README.md) | Learnings index: gotchas and decisions captured while building statschema |
| [`docs/learnings/oltp-migration-analysis.md`](learnings/oltp-migration-analysis.md) | OLTP migration analysis: Northwind, Sakila, Django Auth, WordPress, Chinook |

## Database setup

| Document | Contents |
|----------|----------|
| [`docs/local-databases.md`](local-databases.md) | Index of all local database setup guides |
| [`docs/databases/postgres.md`](databases/postgres.md) | PostgreSQL — Podman, native ARM64 |
| [`docs/databases/neon.md`](databases/neon.md) | Neon — Podman + Neon Local cloud proxy |
| [`docs/databases/cockroachdb.md`](databases/cockroachdb.md) | CockroachDB — Podman, native ARM64 (single-node + multi-region) |
| [`docs/databases/mysql.md`](databases/mysql.md) | MySQL — Podman, native ARM64 |
| [`docs/databases/mariadb.md`](databases/mariadb.md) | MariaDB — Podman, native ARM64 |
| [`docs/databases/sqlserver.md`](databases/sqlserver.md) | SQL Server — Lima VM + QEMU (x86_64) |
| [`docs/databases/oracle.md`](databases/oracle.md) | Oracle — Lima VM + Podman + QEMU (x86_64) |
| [`docs/databases/oracle-hr.md`](databases/oracle-hr.md) | Oracle HR/CO sample schemas — application-level testing |
| [`docs/databases/db2.md`](databases/db2.md) | DB2 — Lima VM + Podman + QEMU (x86_64) |
| [`docs/databases/adventureworks.md`](databases/adventureworks.md) | AdventureWorks — application-level testing (SQL Server) |
| [`docs/databases/chinook.md`](databases/chinook.md) | Chinook — application-level testing (SQL Server) |
| [`docs/databases/mautic.md`](databases/mautic.md) | Mautic — application-level testing (MySQL) |
| [`docs/databases/gitea.md`](databases/gitea.md) | Gitea — application-level testing (PostgreSQL) |
| [`docs/faq/README.md`](faq/README.md) | FAQ index — local database tooling |
| [`docs/faq/17-what-is-gvenzl-oracle-xe.md`](faq/17-what-is-gvenzl-oracle-xe.md) | FAQ: what is the gvenzl/oracle-xe image? |
| [`docs/faq/18-podman-machine-on-macos.md`](faq/18-podman-machine-on-macos.md) | FAQ: Podman machine setup on macOS |

## Contributing & project

| Document | Contents |
|----------|----------|
| [`docs/adding-a-database.md`](adding-a-database.md) | **Contributor guide**: add a new engine end-to-end (pre-checks, dialect registry, emitter, tests, docs) |
| [`docs/PLAN.md`](PLAN.md) | Architecture and implementation notes |
| [`docs/ROADMAP.md`](ROADMAP.md) | Roadmap and planned work |
| [`docs/git-submodules.md`](git-submodules.md) | Git submodule workflow (commit and push `.cursor` + parent repo) |
| [`.env.example`](../.env.example) | Credential template — copy to `.env` and fill in values |
