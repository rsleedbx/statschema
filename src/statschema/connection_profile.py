"""
connection_profile — Named connection profiles loaded from statschema.yaml.

Each profile in statschema.yaml maps to a ConnectionProfile dataclass.
OmegaConf provides YAML parsing, type validation, and lazy interpolation:

  ${oc.env:MY_VAR}               → read environment variable at access time
  ${secrets:scope,key}           → fetch Databricks secret (scope/key)
  ${secrets:scope,key,field,...} → fetch secret JSON key path

Usage::

    profile = ConnectionProfile.from_yaml("statschema.yaml", "prod_sqlserver")
    ctx = DeploymentContext.from_profile(profile)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from omegaconf import MISSING, OmegaConf, DictConfig


# ---------------------------------------------------------------------------
# Secrets resolver — registered once at import time
# ---------------------------------------------------------------------------

def _secrets_resolver(*args: str) -> str:
    """
    OmegaConf resolver: ${secrets:scope,key[,field1,field2,...]}

    - scope, key    → Databricks secret scope and key name
    - field1, ...   → optional JSON path within the secret value

    Cached per (scope, key) so the Databricks network call happens only once.
    """
    scope, key, *fields = args
    from statschema.databricks_secrets import read_secret_string  # type: ignore[import]
    value = read_secret_string(scope, key)
    if fields:
        import json
        obj = json.loads(value)
        for f in fields:
            obj = obj[f]
        return str(obj)
    return str(value)


try:
    OmegaConf.register_new_resolver("secrets", _secrets_resolver, use_cache=True)
except ValueError as exc:
    if "already registered" not in str(exc):
        raise


# ---------------------------------------------------------------------------
# Dialect enum
# ---------------------------------------------------------------------------

class Dialect(str, Enum):
    POSTGRES    = "postgres"
    COCKROACHDB = "cockroachdb"
    NEON        = "neon"
    LAKEBASE    = "lakebase"      # Databricks Postgres-wire endpoint
    MYSQL       = "mysql"
    MARIADB     = "mariadb"
    SQLSERVER   = "sqlserver"
    ORACLE      = "oracle"
    DB2         = "db2"
    LAKEHOUSE   = "lakehouse"     # Databricks Lakehouse (Unity Catalog / Spark runtime)
    SPARK       = "spark"         # standalone open-source Spark (non-Databricks)


# ---------------------------------------------------------------------------
# ConnectionProfile dataclass (OmegaConf Structured Config)
# ---------------------------------------------------------------------------

@dataclass
class ConnectionProfile:
    """
    Typed representation of a single named profile from statschema.yaml.

    Fields marked ``MISSING`` are required — OmegaConf raises ``MissingMandatoryValue``
    at access time if they are absent from the YAML.

    Example YAML entry::

        profiles:
          prod_sqlserver:
            dialect: sqlserver
            host: ${oc.env:SQLSERVER_HOST}
            loader: native_bulk
            database: mydb
            username: sa
            password: ${secrets:my_scope,sqlserver_creds,password}
    """

    # Required
    # dialect is stored as str so YAML can use lowercase values ("postgres", "sqlserver", …).
    # load_profile() converts it to Dialect after parsing.
    dialect: str = MISSING   # type: ignore[assignment]
    host:    str = MISSING   # type: ignore[assignment]
    loader:  str = MISSING   # type: ignore[assignment]  # slot name validated by DataLoader

    # Connection
    port:     Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    tls:      bool          = False

    # Server version — used by _method_min_server_version checks
    version:  Optional[str] = None

    # Shared staging dirs (used by server_file slots across all dialects)
    client_staging_dir: Optional[str] = None   # where statschema writes
    server_staging_dir: Optional[str] = None   # where the DB server reads

    # Oracle-specific
    oracle_directory:     Optional[str] = None  # Oracle DIRECTORY object name
    oracle_sqlldr_binary: Optional[str] = None  # path to sqlldr binary

    # Lakehouse / cloud
    cloud_staging_uri: Optional[str] = None
    cloud_format:      str           = "parquet"

    # Full connection URL — used when host/port decomposition is insufficient
    # (e.g. Neon cloud's postgresql://… string).  Takes precedence over
    # host/port fields when set.
    url: Optional[str] = None

    # Lakebase — OAuth endpoint resource path
    # (projects/<p>/branches/<b>/endpoints/<e>)
    endpoint: Optional[str] = None

    # Topology — auto-detected from other fields when omitted
    topology:           Optional[str] = None
    server_is_emulated: bool          = False


# ---------------------------------------------------------------------------
# Factory — load a named profile from statschema.yaml
# ---------------------------------------------------------------------------

def _load_profile_dict(yaml_path: str, profile_name: str) -> DictConfig:
    """
    Load ``profiles.<profile_name>`` from *yaml_path* and return it as a
    DictConfig merged onto the ConnectionProfile schema.

    Raises:
        FileNotFoundError        — yaml_path does not exist.
        KeyError                 — profile_name not found under ``profiles:``.
        omegaconf.ValidationError — type mismatch in the YAML.
        omegaconf.MissingMandatoryValue — required field absent.
        omegaconf.ConfigAttributeError  — unknown key in YAML profile.
    """
    raw: DictConfig = OmegaConf.load(yaml_path)  # type: ignore[assignment]
    if "profiles" not in raw:
        raise KeyError(f"statschema.yaml has no top-level 'profiles:' key")
    if profile_name not in raw["profiles"]:
        available = list(raw["profiles"].keys())
        raise KeyError(
            f"Profile {profile_name!r} not found in {yaml_path}. "
            f"Available: {available}"
        )
    profile_raw: DictConfig = raw["profiles"][profile_name]

    # Merge with structured schema — validates types and flags unknown keys
    schema: DictConfig = OmegaConf.structured(ConnectionProfile)
    merged: DictConfig = OmegaConf.merge(schema, profile_raw)
    return merged


def load_profile(yaml_path: str, profile_name: str) -> ConnectionProfile:
    """
    Parse *yaml_path*, extract ``profiles.<profile_name>``, validate it against
    the ConnectionProfile schema, and return a resolved ConnectionProfile instance.

    Interpolations (``${oc.env:…}``, ``${secrets:…}``) are resolved lazily at
    attribute access time, so secrets are only fetched when first used.

    The ``dialect`` field accepts lowercase values as written in YAML
    (e.g. ``postgres``, ``sqlserver``) and is converted to the ``Dialect`` enum.
    """
    merged = _load_profile_dict(yaml_path, profile_name)
    # to_object() triggers interpolation resolution and returns a plain Python object
    profile: ConnectionProfile = OmegaConf.to_object(merged)  # type: ignore[assignment]

    # Convert dialect string → Dialect enum (YAML uses lowercase values)
    raw_dialect = profile.dialect
    try:
        profile.dialect = Dialect(raw_dialect)  # type: ignore[assignment]
    except ValueError:
        valid = [d.value for d in Dialect]
        raise ValueError(
            f"Unknown dialect {raw_dialect!r} in profile {profile_name!r}. "
            f"Valid values: {valid}"
        )

    return profile
