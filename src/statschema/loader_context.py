"""
loader_context — DeploymentContext.

DeploymentContext captures *where* statschema is running relative to the
database: the topology (remote, shared_fs, spark_embedded, …) plus any
staging-area coordinates (Oracle directory object, cloud URI, live Spark
session).

The primary constructor is ``from_profile()``.  Connection credentials
(host, port, username, password, database, endpoint) are carried on the
context so loaders that spawn subprocesses (OracleSqlldrLoader) can build
connection strings without reading os.environ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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
    #   STATSCHEMA__CLIENT_STAGING_DIR  — where statschema writes staging files
    #   STATSCHEMA__SERVER_STAGING_DIR  — where the DB server reads them
    #
    # If client_staging_dir is unset, server_staging_dir is used for writes too.

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

    # --- Loader method selection ---
    # Set by from_profile() via ConnectionProfile.loader, or read from
    # STATSCHEMA__LOADER env var in from_env().
    # DataLoader.load() reads ctx.loader to choose the bulk-load slot.
    # No fallback — raises immediately if unset or unsupported.
    loader: str | None = None

    # DB server version string (e.g. "2022", "23c", "16").
    # Used by DataLoader._method_min_server_version checks.
    server_version: str | None = None

    # Binary paths (dialect-specific, set by profile or env var)
    oracle_sqlldr_binary: str | None = None

    # --- Connection credentials (carried for subprocess loaders, e.g. sqlldr) ---
    # These are populated by from_profile() so that loaders which spawn external
    # binaries (OracleSqlldrLoader) can build connection strings without reading
    # os.environ.
    host:     str | None = None
    port:     int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    endpoint: str | None = None   # Lakebase OAuth endpoint resource path

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_profile(cls, profile: "Any") -> "DeploymentContext":
        """
        Build a DeploymentContext from a ConnectionProfile.

        This is the primary constructor for production use.  All configuration
        comes from the typed profile object — no os.environ reads at call time.
        """
        topology = profile.topology or cls._infer_topology_from_profile(profile)
        dialect = getattr(profile, "dialect", "")
        return cls(
            topology=topology,  # type: ignore[arg-type]
            client_staging_dir=profile.client_staging_dir,
            server_staging_dir=profile.server_staging_dir,
            oracle_directory=profile.oracle_directory,
            oracle_sqlldr_binary=profile.oracle_sqlldr_binary,
            cloud_staging_uri=profile.cloud_staging_uri,
            cloud_format=profile.cloud_format or "parquet",
            server_is_emulated=profile.server_is_emulated or False,
            is_databricks=dialect in ("databricks", "lakehouse"),
            loader=profile.loader,
            server_version=profile.version,
            host=profile.host,
            port=profile.port,
            database=profile.database,
            username=profile.username,
            password=profile.password,
            endpoint=getattr(profile, "endpoint", None),
        )

    @staticmethod
    def _infer_topology_from_profile(profile: "Any") -> str:
        """Infer topology from profile fields when profile.topology is None."""
        if profile.cloud_staging_uri:
            return "cloud_staged"
        if profile.client_staging_dir or profile.server_staging_dir:
            return "shared_fs"
        return "remote"

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
