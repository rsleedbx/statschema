"""
loader_context — DeploymentContext and CredentialProvider chain.

DeploymentContext captures *where* statschema is running relative to the
database: the topology (remote, shared_fs, spark_embedded, …) plus any
staging-area coordinates (Oracle directory object, cloud URI, live Spark
session).

CredentialProvider is a Protocol that lets multiple sources supply
credentials; the chain merges them lowest-precedence first so local .env
values always win over remote secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# CredentialProvider protocol + built-in implementations
# ---------------------------------------------------------------------------

class CredentialProvider:
    """
    All credential sources implement one method: load() → dict[str, str].

    Keys mirror .env variable names (STATSCHEMA_*, PG_PASSWORD, …).
    Absent keys are not set to empty string — only known keys are returned.
    Callers merge multiple providers via dict.update() (last wins).
    """

    def load(self) -> dict[str, str]:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def for_backend(backend: str, **kwargs: Any) -> "CredentialProvider":
        """
        Factory: resolve a backend name to a concrete provider.

        Phase 1 supports: "databricks"
        Phase 2 will add: "aws" | "vault" | "azure" | "gcp"
        """
        _registry: dict[str, type] = {
            "databricks": DatabricksSecretProvider,
            # Phase 2 — add class body, then uncomment:
            # "aws":    AwsSecretsManagerProvider,
            # "vault":  HashiCorpVaultProvider,
            # "azure":  AzureKeyVaultProvider,
            # "gcp":    GcpSecretManagerProvider,
        }
        if backend not in _registry:
            raise ValueError(
                f"Unknown STATSCHEMA_SECRETS_BACKEND={backend!r}. "
                f"Phase 1 valid values: {list(_registry)}"
            )
        return _registry[backend](**kwargs)


class EnvCredentialProvider(CredentialProvider):
    """Loads credentials from os.environ (populated by python-dotenv from .env)."""

    def load(self) -> dict[str, str]:
        return dict(os.environ)


class DatabricksSecretProvider(CredentialProvider):
    """
    Fetches a Databricks secret (JSON blob) from a named scope + key.

    Delegates to databricks_secrets.load_credentials(), which handles:
    - Serverless notebooks   (databricks.sdk.runtime.dbutils, DBR 14.1+)
    - Classic shared cluster (IPython user namespace)
    - Databricks Connect     (WorkspaceClient SDK + base64 decode)

    JSON format: either the flat statschema env-var format
    {"PG_PASSWORD": "...", ...} or LfcCredential v2 format
    {"version": "v2", "db_type": "postgresql", "host_fqdn": ..., ...}.
    Both are normalised to statschema .env var names automatically.
    """

    def __init__(self, scope: str, key: str):
        self.scope = scope
        self.key = key

    def load(self) -> dict[str, str]:
        from statschema.databricks_secrets import load_credentials  # type: ignore[import]
        return load_credentials(self.scope, self.key)


# ---------------------------------------------------------------------------
# DeploymentContext
# ---------------------------------------------------------------------------

@dataclass
class DeploymentContext:
    """
    Describes *where* statschema is running relative to the target database.

    A single typed object that loaders interrogate via ``can_use()``.

    Topology values (Phase 1):
      remote            — statschema on a client host, DB reachable over TCP
      shared_fs         — a filesystem path is readable by both statschema and the
                          DB server process; statschema does NOT need to be on the
                          same host.  Covers: local disk, NFS/CIFS mount, Lima virtfs
                          bind-mount, Docker/Podman bind-mount, SSH-FUSE.  The key
                          property is that the DB server process can open the file
                          by path — same principle as Oracle DIRECTORY objects,
                          DB2 "server-side LOAD", and SQL Server BULK INSERT.
      embedded          — statschema inside the DB process
      spark_embedded    — live SparkSession in-process (notebook / cluster job)
      spark_connect     — remote community Spark via gRPC (Spark 3.4+)
      databricks_connect— remote Databricks cluster via Databricks Connect SDK
      cloud_staged      — statschema writes to S3/ADLS/GCS; DB ingests from there

    Phase 2 topology values (not yet used):
      connector_managed — connector library owns staging (Snowflake write_pandas)
      binary_sidecar    — external binary required (Teradata tbuild)
    """

    topology: Literal[
        "remote",
        "shared_fs",
        "embedded",
        "spark_embedded",
        "spark_connect",
        "databricks_connect",
        "cloud_staged",
    ] = "remote"

    # --- Shared-filesystem staging paths ---
    # The same physical directory is mounted at (potentially) different paths on
    # the statschema host and the DB server host.  Example:
    #
    #   NFS server exports /exports/staging
    #     → statschema host mounts it at  /mnt/db_staging   (client_staging_dir)
    #     → DB server mounts it at        /mnt/shared       (server_staging_dir)
    #
    # For Lima/Podman dev setups both sides see the same path, so setting only
    # server_staging_dir (or neither) is sufficient.
    #
    # Env vars:
    #   STATSCHEMA_CLIENT_STAGING_DIR  — where statschema writes staging files
    #   STATSCHEMA_SERVER_STAGING_DIR  — where the DB server reads them
    #
    # If client_staging_dir is unset, server_staging_dir is used for writes too
    # (backward-compatible with the old single-path behaviour).

    client_staging_dir: str | None = None
    """Filesystem path where statschema writes staging files (statschema's mount point)."""

    server_staging_dir: str | None = None
    """Filesystem path where the DB server reads staging files (DB server's mount point)."""

    # --- Oracle shared-filesystem staging ---
    oracle_directory: str | None = None  # Oracle DIRECTORY object name

    # Set True when the DB server runs under QEMU x86_64 emulation (e.g. Lima on
    # Apple Silicon).  Process-forking loaders (sqlldr, External Tables) are ~7×
    # slower than direct_path_load under QEMU because each fork triggers a full JIT
    # recompile cycle; OracleSqlldrLoader / OracleExternalTableLoader check this flag
    # in can_use() and yield to OracleDirectPathLoader when it is set.
    server_is_emulated: bool = False

    # --- Spark / Lakehouse ---
    spark_session: Any | None = None    # SparkSession or DatabricksSession
    is_databricks: bool = False         # auto-set by from_spark_session()
    cloud_staging_uri: str | None = None  # "s3://bucket/prefix/" or "adls://…"
    cloud_format: str = "parquet"       # parquet | csv | delta

    # Phase 2: iam_role: str | None = None  (Redshift / Aurora S3-backed COPY)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def _ctx_from_dict(cls, d: dict[str, str]) -> "DeploymentContext":
        """
        Resolve topology and build a DeploymentContext from any key-value dict.

        Priority order (first match wins):
        1. STATSCHEMA_TOPOLOGY set explicitly → use it directly.
        2. STATSCHEMA_CLOUD_STAGING_URI set   → topology="cloud_staged".
        3. STATSCHEMA_SERVER_STAGING_DIR or STATSCHEMA_CLIENT_STAGING_DIR set
                                              → topology="shared_fs".
        4. Default                            → topology="remote".
        """
        def g(k: str) -> str:
            return d.get(k, "").strip()

        is_emulated = g("STATSCHEMA_SERVER_IS_EMULATED").lower() in ("1", "true", "yes")

        client_dir = g("STATSCHEMA_CLIENT_STAGING_DIR") or None
        server_dir = g("STATSCHEMA_SERVER_STAGING_DIR") or None

        explicit = g("STATSCHEMA_TOPOLOGY")
        if explicit:
            return cls(
                topology=explicit,  # type: ignore[arg-type]
                cloud_staging_uri=g("STATSCHEMA_CLOUD_STAGING_URI") or None,
                oracle_directory=g("ORACLE_SERVER_DIRECTORY") or None,
                client_staging_dir=client_dir,
                server_staging_dir=server_dir,
                server_is_emulated=is_emulated,
                is_databricks=bool(g("DATABRICKS_HOST")),
            )

        cloud_uri = g("STATSCHEMA_CLOUD_STAGING_URI")
        if cloud_uri:
            return cls(
                topology="cloud_staged",
                cloud_staging_uri=cloud_uri,
                server_is_emulated=is_emulated,
                is_databricks=bool(g("DATABRICKS_HOST")),
            )

        if server_dir or client_dir:
            return cls(
                topology="shared_fs",
                oracle_directory=g("ORACLE_SERVER_DIRECTORY") or None,
                client_staging_dir=client_dir,
                server_staging_dir=server_dir,
                server_is_emulated=is_emulated,
            )

        return cls(
            topology="remote",
            oracle_directory=g("ORACLE_SERVER_DIRECTORY") or None,
            client_staging_dir=client_dir,
            server_staging_dir=server_dir,
            server_is_emulated=is_emulated,
        )

    @classmethod
    def from_providers(
        cls,
        providers: "list[CredentialProvider]",
    ) -> "DeploymentContext":
        """
        Build a DeploymentContext by merging credentials from an ordered list
        of CredentialProvider instances (left-to-right; later providers win).

        Standard ordering — lowest to highest precedence:
            [DatabricksSecretProvider(...), EnvCredentialProvider()]
        → secret provides the base; env vars override individual keys for
          local dev / overrides.
        """
        merged: dict[str, str] = {}
        for p in providers:
            merged.update(p.load())
        return cls._ctx_from_dict(merged)

    @classmethod
    def from_env(cls) -> "DeploymentContext":
        """
        Build a DeploymentContext using auto-detected credential providers.

        Provider chain (lowest → highest precedence, later entries win):
        1. Secret backend — if STATSCHEMA_SECRETS_BACKEND is set, instantiate
           that provider with STATSCHEMA_SECRETS_SCOPE / STATSCHEMA_SECRETS_KEY.
           Defaults to 'databricks' when scope+key are present and no backend
           is named explicitly.
        2. EnvCredentialProvider — os.environ always layers on top, so a local
           .env can override individual keys from any secret backend.

        If no secrets are configured, only EnvCredentialProvider is used —
        identical behaviour to the original implementation.
        """
        providers: list[CredentialProvider] = []

        scope = os.environ.get("STATSCHEMA_SECRETS_SCOPE", "").strip()
        key = os.environ.get("STATSCHEMA_SECRETS_KEY", "").strip()
        backend = os.environ.get("STATSCHEMA_SECRETS_BACKEND", "").strip()

        if scope and key:
            backend = backend or "databricks"
            providers.append(CredentialProvider.for_backend(backend, scope=scope, key=key))

        providers.append(EnvCredentialProvider())   # always last — highest precedence
        return cls.from_providers(providers)

    @classmethod
    def from_spark_session(cls, spark: Any) -> "DeploymentContext":
        """
        Auto-detect topology and is_databricks from a live SparkSession.

        Detection priority:
        1. DatabricksSession (databricks-connect package) → databricks_connect
        2. pyspark.sql.connect session (community Spark Connect) → spark_connect
        3. Databricks cluster env (spark.databricks.service.name) → spark_embedded
        4. Plain SparkSession → spark_embedded (community)
        """
        try:
            from importlib import import_module
            DatabricksSession = import_module("databricks.connect").DatabricksSession
            if isinstance(spark, DatabricksSession):
                return cls(
                    topology="databricks_connect",
                    spark_session=spark,
                    is_databricks=True,
                )
        except (ImportError, AttributeError):
            pass

        is_connect = type(spark).__module__.startswith("pyspark.sql.connect")
        try:
            is_databricks = bool(
                spark.conf.get("spark.databricks.service.name", None)
            )
        except Exception:
            is_databricks = False

        topology: str
        if is_connect:
            topology = "spark_connect"
        elif is_databricks:
            topology = "spark_embedded"
        else:
            topology = "spark_embedded"

        return cls(
            topology=topology,  # type: ignore[arg-type]
            spark_session=spark,
            is_databricks=is_databricks,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def has_shared_fs(self) -> bool:
        """Return True when the DB server process can read a local path written by statschema.

        True for ``shared_fs`` (NFS, bind-mount, virtfs, local disk) and
        ``embedded`` (statschema runs inside the DB process — always has FS access).
        """
        return self.topology in ("shared_fs", "embedded")

    def staging_write_dir(self) -> str | None:
        """Return the directory where statschema should write staging files.

        Uses ``client_staging_dir`` when set; falls back to ``server_staging_dir``
        for single-path setups where both sides use the same mount point.
        """
        return self.client_staging_dir or self.server_staging_dir

    def to_server_path(self, client_path: str) -> str:
        """Translate an absolute client-side staging path to the server-side equivalent.

        When ``client_staging_dir`` and ``server_staging_dir`` are different mount
        points for the same NFS share, this replaces the client prefix with the
        server prefix.  Example::

            ctx = DeploymentContext(
                client_staging_dir="/mnt/a/statschema",
                server_staging_dir="/mnt/b/statschema",
            )
            ctx.to_server_path("/mnt/a/statschema/data.csv")
            # → "/mnt/b/statschema/data.csv"

        Returns ``client_path`` unchanged when both dirs are the same or either
        is unset (same-path assumption).
        """
        c = self.client_staging_dir
        s = self.server_staging_dir
        if not c or not s or c == s:
            return client_path
        if client_path.startswith(c):
            return s + client_path[len(c):]
        return client_path

    def has_spark(self) -> bool:
        return self.spark_session is not None or self.topology in (
            "spark_embedded", "spark_connect", "databricks_connect"
        )
