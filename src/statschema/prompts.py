"""
statschema/prompts.py — MCP prompt registry.

Prompts are reusable message templates that guide the model through multi-step
statschema workflows spanning multiple tool calls.  They appear in the client's
prompt picker so the user can invoke a workflow by name.
"""
from __future__ import annotations

from typing import Any


def register_prompts(server: Any) -> None:
    """Register all statschema MCP prompts on a FastMCP server instance."""

    @server.prompt()
    def discover_and_populate() -> str:
        """
        Guided workflow: discover forgedb-provisioned databases, then populate
        one with synthetic data and inject optimizer statistics.
        """
        return (
            "You are helping populate a forgedb-provisioned database with synthetic data.\n\n"
            "Step 1 — Discover available databases:\n"
            "  Ask the user for their forgedb scope (default: 'default').\n"
            "  Read the forgedb MCP resource: forgedb://connections/{scope}\n"
            "  List the available databases to the user in a table showing:\n"
            "    server_name, db_type, host_fqdn, port, catalog\n"
            "  Ask which database to populate.\n\n"
            "Step 2 — Map db_type to statschema dialect:\n"
            "  postgresql → postgres\n"
            "  mysql      → mysql\n"
            "  sqlserver  → mssql\n"
            "  oracle     → oracle\n"
            "  db2        → db2\n\n"
            "Step 3 — Build the DSN from the conn_blob fields (use dba credentials):\n"
            "  For postgres/mysql/cockroachdb:\n"
            "    host=<host_fqdn> port=<port> dbname=<catalog> "
            "user=<dba.user> password=<dba.password>\n"
            "  For mssql: Server=<host_fqdn>,<port>;Database=<catalog>;"
            "UID=<dba.user>;PWD=<dba.password>;TrustServerCertificate=yes\n\n"
            "Step 4 — Ask the user what schema to generate:\n"
            "  num_tables (default: 10), num_columns (default: 10),\n"
            "  col_types (default: 'int,float,text'), distribution (default: 'zipf'),\n"
            "  row_count (default: 10 000).\n\n"
            "Step 5 — Call statschema describe_schema with those parameters.\n"
            "  Show the user the first table's columns for confirmation.\n\n"
            "Step 6 — Call statschema load_data:\n"
            "  schema_yaml = result from step 5\n"
            "  dsn         = DSN from step 3\n"
            "  dialect     = dialect from step 2\n\n"
            "Step 7 — Call statschema inject_stats:\n"
            "  dsn / dialect = same as step 6\n"
            "  stats_yaml    = call describe_schema again with the same params\n"
            "                  (the heuristic stats are embedded in the YAML)\n\n"
            "Report: tables loaded, total rows, and stats injection outcome."
        )

    @server.prompt()
    def populate_from_description() -> str:
        """
        Guided workflow: generate a schema from a description, then populate
        a database with synthetic data and inject optimizer statistics.
        """
        return (
            "You are helping populate a database with synthetic data using statschema.\n\n"
            "When the user provides a description like 'populate 10 tables with 10 columns "
            "of int, float, text types' follow these steps:\n\n"
            "1. Call describe_schema with the parameters inferred from the description:\n"
            "   - num_tables: number of tables requested\n"
            "   - num_columns: non-PK columns per table\n"
            "   - pk_type: primary key type (default: bigint)\n"
            "   - col_types: comma-separated types (e.g. 'int,float,text')\n"
            "   - distribution: 'zipf' unless the user specifies uniform or normal\n"
            "   - row_count: number of rows per table (default: 10 000)\n\n"
            "2. Show the user the first table's column list from the returned YAML.\n"
            "   Ask for confirmation before loading data.\n\n"
            "3. Once confirmed, call load_data with:\n"
            "   - schema_yaml: the YAML from step 1\n"
            "   - dsn: the connection string (ask the user if not provided)\n"
            "   - dialect: the target database dialect\n\n"
            "4. After load_data succeeds, call inject_stats with:\n"
            "   - dsn / dialect: same target\n"
            "   - stats_yaml: regenerate by calling describe_schema again with "
            "     with_stats=true (or use the stats embedded in the schema YAML)\n\n"
            "5. Report the number of tables loaded, total rows, and whether stats\n"
            "   injection succeeded."
        )

    @server.prompt()
    def migrate_schema() -> str:
        """
        Guided workflow: collect schema and stats from a source database, then
        transpile the DDL and inject statistics into a target database.
        """
        return (
            "You are helping migrate a schema from a source database to a target database.\n\n"
            "Steps:\n\n"
            "1. Ask the user for:\n"
            "   - Source DSN and dialect\n"
            "   - Target DSN and dialect\n"
            "   - Table names to migrate (or '%' for all)\n\n"
            "2. Call collect_stats with the source DSN, dialect, and tables.\n"
            "   Save the returned YAML — it contains both schema and statistics.\n\n"
            "3. Call transpile_ddl with:\n"
            "   - sql: the DDL extracted from the collected YAML\n"
            "   - source_dialect: source engine\n"
            "   - target_dialect: target engine\n"
            "   Show the transpiled DDL to the user for review.\n\n"
            "4. Once approved, apply the DDL to the target database.\n\n"
            "5. Call inject_stats with:\n"
            "   - dsn / dialect: target connection\n"
            "   - stats_yaml: the YAML from step 2\n"
            "   This bootstraps the optimizer before any real data arrives.\n\n"
            "6. Optionally call load_data to populate the target with synthetic rows\n"
            "   that match the source distributions, for query plan validation."
        )

    @server.prompt()
    def validate_migration() -> str:
        """
        Guided workflow: load synthetic data into both source and target schemas,
        then compare query plans to validate optimizer fidelity.
        """
        return (
            "You are validating that a migrated database produces equivalent query plans.\n\n"
            "Steps:\n\n"
            "1. Collect stats from the source: call collect_stats with the source DSN.\n\n"
            "2. Load synthetic data into the target: call load_data with the collected\n"
            "   schema YAML and the target DSN.\n\n"
            "3. Inject stats into the target: call inject_stats so the target optimizer\n"
            "   sees production-representative cardinalities.\n\n"
            "4. Report: tables loaded, rows per table, and injection summary.\n"
            "   Ask the user to run EXPLAIN queries on both source and target to\n"
            "   compare plan shapes — statschema ensures the cardinalities match;\n"
            "   plan equivalence confirms the migration is optimizer-ready."
        )
