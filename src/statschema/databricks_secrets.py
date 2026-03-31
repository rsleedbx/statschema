"""
Databricks secret reading utilities.

Works on Serverless notebooks, Classic shared-cluster notebooks, and
Databricks Connect from a local IDE — without the caller knowing which
environment they are in.

Reading priority:
  1. databricks.sdk.runtime.dbutils  — Serverless notebooks (DBR 14.1+)
  2. IPython notebook namespace       — Classic shared-cluster notebooks
  3. WorkspaceClient SDK              — Databricks Connect / any env where
                                         DATABRICKS_HOST + TOKEN are set
                                         (or ~/.databrickscfg is present)

The SDK path always returns a base64-encoded value that is decoded here so
callers always receive a plain string regardless of which path was used.

Secret JSON formats — two variants are accepted transparently:

  Flat env-var format (statschema native):
      {"PG_PASSWORD": "s3cr3t", "PG_DB": "mydb", ...}
      Keys must mirror the names in .env.example.  Returned as-is.

  LfcCredential v2 format (lfcdemolib-compatible):
      {"version": "v2", "db_type": "postgresql", "host_fqdn": "...",
       "catalog": "...", "user": "...", "password": "...", "port": 5432,
       "dba": {"user": "...", "password": "..."}, "cloud": {...}}
      Fields are mapped to statschema env-var names (see _V2_FIELD_MAP).
      This allows a single Databricks secret to be shared between statschema
      and lfcdemolib without duplication.

Typical usage — inline OmegaConf resolver in statschema.yaml:

    profiles:
      prod_postgres:
        host:     ${secrets:my_scope,mydb_creds,host}
        password: ${secrets:my_scope,mydb_creds,password}

Or directly in Python:

    from statschema.databricks_secrets import read_secret_string, parse_secret_json

    raw   = read_secret_string(scope="my_scope", key="mydb.example.com_json")
    creds = parse_secret_json(raw)
    # {"PG_HOST": "...", "PG_PASSWORD": "...", ...}
"""

from __future__ import annotations

import base64
import json
from typing import Any


# ---------------------------------------------------------------------------
# dbutils resolution — three paths, tried in priority order
# ---------------------------------------------------------------------------

def _dbutils_from_runtime() -> Any | None:
    """
    Serverless path: databricks.sdk.runtime (DBR 14.1+).
    This is the recommended path for serverless notebooks; it avoids the
    IPython dependency entirely.
    """
    try:
        from databricks.sdk.runtime import dbutils  # type: ignore[import]
        return dbutils
    except (ImportError, ModuleNotFoundError):
        return None


def _dbutils_from_ipython() -> Any | None:
    """
    Classic shared-cluster path: Databricks injects dbutils into the
    notebook globals, so it appears in the IPython user namespace and is
    accessible from any imported module.
    """
    try:
        import IPython
        ip = IPython.get_ipython()
        if ip is not None:
            dbutils = ip.user_ns.get("dbutils")
            if dbutils is not None:
                return dbutils
    except (ImportError, Exception):
        pass
    return None


def get_dbutils() -> Any | None:
    """
    Return a dbutils handle if one is available in the current environment.

    Returns None when running outside a Databricks cluster (e.g. local dev,
    Databricks Connect from an IDE).  Callers should fall back to the
    WorkspaceClient SDK path when this returns None.

    Priority:
      1. databricks.sdk.runtime  (Serverless, DBR 14.1+)
      2. IPython user namespace   (Classic shared cluster)
    """
    return _dbutils_from_runtime() or _dbutils_from_ipython()


# ---------------------------------------------------------------------------
# Low-level secret reading
# ---------------------------------------------------------------------------

def read_secret_string(scope: str, key: str) -> str:
    """
    Read a Databricks secret and return the plain-text string value.

    Tries dbutils first (serverless → shared cluster).  Falls back to
    WorkspaceClient SDK, which returns a base64-encoded value that is decoded
    transparently so callers always receive the raw string.

    Raises RuntimeError if neither path succeeds.
    """
    dbutils = get_dbutils()
    if dbutils is not None:
        try:
            # dbutils.secrets.get() returns a plain string — no base64 decode needed.
            return dbutils.secrets.get(scope=scope, key=key)
        except Exception:
            pass  # fall through to SDK path

    # SDK path — works wherever Databricks auth is configured.
    try:
        from databricks.sdk import WorkspaceClient  # type: ignore[import]
        resp = WorkspaceClient().secrets.get_secret(scope=scope, key=key)
        # Databricks SDK always base64-encodes the secret value on the wire.
        return base64.b64decode(resp.value).decode("utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Could not read Databricks secret '{scope}/{key}'. "
            f"Ensure databricks-sdk is installed and auth is configured "
            f"(DATABRICKS_HOST + DATABRICKS_TOKEN, or ~/.databrickscfg). "
            f"Original error: {e}"
        ) from e


# ---------------------------------------------------------------------------
# JSON parsing and format normalisation
# ---------------------------------------------------------------------------

# LfcCredential v2 field path → statschema .env var name, grouped by db_type.
# Dotted keys (e.g. "dba.user") are resolved from the nested dict at read time.
_V2_FIELD_MAP: dict[str, dict[str, str]] = {
    "postgresql": {
        "host_fqdn":    "PG_HOST",
        "port":         "PG_PORT",
        "catalog":      "PG_DB",
        "user":         "PG_USER",
        "password":     "PG_PASSWORD",
        "dba.user":     "PG_DBA_USER",
        "dba.password": "PG_DBA_PASSWORD",
    },
    "mysql": {
        "host_fqdn":    "MYSQL_HOST",
        "port":         "MYSQL_PORT",
        "catalog":      "MYSQL_DATABASE",
        "user":         "MYSQL_USER",
        "password":     "MYSQL_ROOT_PASS",
        "dba.user":     "MYSQL_DBA_USER",
        "dba.password": "MYSQL_DBA_PASSWORD",
    },
    "sqlserver": {
        "host_fqdn":    "SQLSERVER_HOST",
        "port":         "SQLSERVER_PORT",
        "catalog":      "SQLSERVER_DATABASE",
        "user":         "SQLSERVER_USER",
        "password":     "SQLSERVER_PASS",
        "dba.user":     "SQLSERVER_DBA_USER",
        "dba.password": "SQLSERVER_DBA_PASSWORD",
    },
    "oracle": {
        "host_fqdn":    "ORACLE_HOST",
        "port":         "ORACLE_PORT",
        "catalog":      "ORACLE_SERVICE",   # catalog = TNS alias / service name
        "user":         "ORACLE_USER",
        "password":     "ORACLE_PASS",
        "dba.user":     "ORACLE_DBA_USER",
        "dba.password": "ORACLE_DBA_PASSWORD",
    },
    "db2": {
        "host_fqdn":    "DB2_HOST",
        "port":         "DB2_PORT",
        "catalog":      "DB2_DATABASE",
        "user":         "DB2_USER",
        "password":     "DB2_PASS",
        "dba.user":     "DB2_DBA_USER",
        "dba.password": "DB2_DBA_PASSWORD",
    },
}

# Top-level v2 fields that are nested objects — excluded from passthrough.
_V2_NESTED_FIELDS = frozenset({"dba", "cloud", "options"})

# All scalar field names covered by _V2_FIELD_MAP across any db_type.
_V2_MAPPED_SCALARS = frozenset(
    k for mapping in _V2_FIELD_MAP.values() for k in mapping if "." not in k
)


def _v2_to_env(data: dict[str, Any]) -> dict[str, str]:
    """
    Convert an LfcCredential v2 dict to statschema .env var names.

    Mapped fields are translated using _V2_FIELD_MAP for the given db_type.
    Unmapped scalar fields (version, db_version, schema, replication_mode,
    any STATSCHEMA_* keys injected by the DBA) are kept under their original
    key so nothing is silently lost.
    Nested objects (dba, cloud, options) are handled only via the explicit
    dotted-path entries in _V2_FIELD_MAP.
    """
    db_type = str(data.get("db_type", "")).lower()
    field_map = _V2_FIELD_MAP.get(db_type, {})
    result: dict[str, str] = {}

    for v2_key, env_key in field_map.items():
        if "." in v2_key:
            parent, child = v2_key.split(".", 1)
            nested = data.get(parent)
            val = nested.get(child) if isinstance(nested, dict) else None
        else:
            val = data.get(v2_key)
        if val is not None:
            result[env_key] = str(val)

    # Passthrough: unmapped top-level scalar fields.
    for k, v in data.items():
        if k in _V2_NESTED_FIELDS or k in _V2_MAPPED_SCALARS:
            continue
        if v is not None:
            result[k] = str(v)

    return result


def parse_secret_json(raw: str) -> dict[str, str]:
    """
    Parse a Databricks secret JSON string into a flat ``dict[str, str]``.

    Two JSON formats are accepted:

    **Flat env-var format** (statschema native)::

        {"PG_PASSWORD": "s3cr3t", "PG_DB": "mydb", ...}

    Values are coerced to str and returned as-is.

    **LfcCredential v2 format** (lfcdemolib-compatible)::

        {
            "version": "v2",
            "db_type": "postgresql",
            "host_fqdn": "mydb.postgres.example.com",
            "catalog": "mydb",
            "user": "myuser",
            "password": "s3cr3t",
            "port": 5432,
            "dba": {"user": "postgres", "password": "adminpass"},
            "cloud": {"provider": "azure", "location": "East US"}
        }

    Fields are mapped to statschema .env var names via ``_V2_FIELD_MAP``.

    All values in the returned dict are str, so the result can be merged
    directly into os.environ.
    """
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Databricks secret is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"Databricks secret JSON must be a JSON object, got {type(data).__name__}"
        )

    if data.get("version") == "v2" and "db_type" in data:
        return _v2_to_env(data)

    # Flat env-var format — coerce values to str, drop None entries.
    return {k: str(v) for k, v in data.items() if v is not None}


# ---------------------------------------------------------------------------
# Convenience function — used by DatabricksSecretProvider in loader_context.py
# ---------------------------------------------------------------------------

def load_credentials(scope: str, key: str) -> dict[str, str]:
    """
    Read a Databricks secret and return a flat env-var dict.

    Single-call equivalent of ``parse_secret_json(read_secret_string(scope, key))``.
    This is the function that ``DatabricksSecretProvider.load()`` delegates to.
    """
    return parse_secret_json(read_secret_string(scope, key))
