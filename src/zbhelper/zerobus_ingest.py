"""
ZeroBus ingest: serialize a Spark DataFrame to protobuf and send to ZeroBus.

Authentication modes
--------------------
1. Databricks SDK (default for local dev)
   Uses the same credentials already in ~/.databrickscfg — no service principal
   needed.  WorkspaceClient().config.authenticate() returns the current Bearer
   token; WorkspaceClient().tokens.create(lifetime_seconds=N) creates a
   short-lived PAT that auto-expires.

   Usage:
       config = IngestConfig.from_workspace_client(table_name="main.default.t")
       result = ingest_dataframe(df, table, config)

2. Service principal (CI / production)
   Requires DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET env vars.

   Usage:
       config = IngestConfig.from_env(table_name="main.default.t")
       result = ingest_dataframe(df, table, config)

ZeroBus endpoint
----------------
The endpoint is auto-constructed from the workspace hostname:
    https://{workspace_id}.zerobus.{workspace_hostname}

Example:
    workspace: https://e2-dogfood.staging.cloud.databricks.com
    zerobus:   https://6051921418418893.zerobus.e2-dogfood.staging.cloud.databricks.com

ZEROBUS_SERVER_ENDPOINT env var overrides the auto-constructed URL if set.

Required env vars for service-principal mode (from_env):
    DATABRICKS_CLIENT_ID       - service principal application ID
    DATABRICKS_CLIENT_SECRET   - service principal secret
    ZEROBUS_SERVER_ENDPOINT    - ZeroBus gRPC endpoint URL
    DATABRICKS_WORKSPACE_URL   - Databricks workspace URL
    ZEROBUS_TABLE_NAME         - catalog.schema.table
"""

import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from src.statschema.model import CanonicalTableSchema
from src.zbhelper.protobuf_converter import compile_proto, schema_to_proto_str, serialize_row

# ZeroBus SDK — optional at import time (not available in .venv_test).
# Module-level names allow unittest.mock.patch.multiple to replace them in tests.
try:
    from zerobus import (
        ZerobusSdk,
        TableProperties,
        StreamConfigurationOptions,
        RecordType,
        HeadersProvider,
    )
except ImportError:  # pragma: no cover
    ZerobusSdk = None              # type: ignore[assignment,misc]
    TableProperties = None         # type: ignore[assignment,misc]
    StreamConfigurationOptions = None  # type: ignore[assignment,misc]
    RecordType = None              # type: ignore[assignment,misc]
    HeadersProvider = object       # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# SDK-based auth (uses ~/.databrickscfg — same credentials as Databricks Connect)
# ---------------------------------------------------------------------------

class DatabricksSdkHeadersProvider(HeadersProvider):
    """
    ZeroBus HeadersProvider backed by the Databricks Python SDK.

    On every get_headers() call, WorkspaceClient().config.authenticate() returns
    fresh auth headers — the SDK handles token caching and refresh automatically.
    No subprocess, no JSON parsing, no separate service principal needed.

    Optionally creates a short-lived PAT (lifetime_seconds) instead of using the
    session OAuth token.  The PAT auto-expires, so no manual cleanup is needed.
    """

    def __new__(cls, *args, **kwargs):
        # The Rust HeadersProvider.__new__ requires being called without forwarded
        # args, so we intercept __new__ and call it with cls only.
        return HeadersProvider.__new__(cls)

    def __init__(
        self,
        table_name: str,
        *,
        host: Optional[str] = None,
        profile: Optional[str] = None,
        lifetime_seconds: Optional[int] = None,
    ) -> None:
        super().__init__()
        self._table_name = table_name
        self._host = host
        self._profile = profile
        self._lifetime_seconds = lifetime_seconds

    def get_headers(self) -> List[Tuple[str, str]]:
        token = _get_sdk_token(
            host=self._host,
            profile=self._profile,
            lifetime_seconds=self._lifetime_seconds,
        )
        return [
            ("authorization", f"Bearer {token}"),
            ("x-databricks-zerobus-table-name", self._table_name),
        ]


def _get_sdk_token(
    host: Optional[str] = None,
    profile: Optional[str] = None,
    lifetime_seconds: Optional[int] = None,
) -> str:
    """
    Get a Bearer token via the Databricks Python SDK.

    WorkspaceClient automatically resolves credentials from ~/.databrickscfg,
    environment variables (DATABRICKS_HOST, DATABRICKS_TOKEN), or instance
    profiles — the same priority order as Databricks Connect.

    If lifetime_seconds is given, creates a short-lived PAT via the Tokens API
    (auto-expires, no manual cleanup needed).  Otherwise uses the current
    OAuth session token from config.authenticate().

    Raises:
        ImportError:  if databricks-sdk is not installed.
        RuntimeError: if no credentials are configured.
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise ImportError(
            "databricks-sdk is not installed. Run: pip install databricks-sdk"
        )

    kwargs: dict = {}
    if host:
        kwargs["host"] = host
    if profile:
        kwargs["profile"] = profile

    w = WorkspaceClient(**kwargs)

    if lifetime_seconds is not None:
        # Short-lived PAT: auto-expires, no cleanup needed
        response = w.tokens.create(
            comment="zerobus-ingest-temp",
            lifetime_seconds=lifetime_seconds,
        )
        return response.token_value

    # Use the current session OAuth token (SDK handles refresh)
    auth_headers = w.config.authenticate()
    auth_value = auth_headers.get("Authorization", "")
    if not auth_value.startswith("Bearer "):
        raise RuntimeError(
            "Could not retrieve a Bearer token from WorkspaceClient. "
            "Run 'databricks auth login --configure-serverless' to configure credentials."
        )
    return auth_value.removeprefix("Bearer ")


def _host_from_workspace_client(
    host: Optional[str] = None,
    profile: Optional[str] = None,
) -> str:
    """Read the workspace host via the SDK (falls back to DATABRICKS_HOST env var)."""
    if host:
        return host
    if os.environ.get("DATABRICKS_HOST"):
        return os.environ["DATABRICKS_HOST"]
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise ImportError("databricks-sdk is not installed. Run: pip install databricks-sdk")
    kwargs: dict = {}
    if profile:
        kwargs["profile"] = profile
    return WorkspaceClient(**kwargs).config.host



def _schema_from_username(username: str) -> str:
    """
    Derive a Unity Catalog schema name from a Databricks username/email.

    robert.lee@databricks.com  ->  robert_lee
    robert-lee@company.com     ->  robert_lee
    """
    local = username.split("@")[0]
    return local.replace(".", "_").replace("-", "_").lower()


def build_qualified_table_name(
    table_name: str,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    host: Optional[str] = None,
    profile: Optional[str] = None,
) -> str:
    """
    Build a fully-qualified Unity Catalog table name (catalog.schema.table).

    If table_name already contains two dots it is returned unchanged.

    Schema resolution order:
      1. schema= argument
      2. ZEROBUS_SCHEMA env var  (e.g. export ZEROBUS_SCHEMA=robert_lee_zerobus_unittest)
      3. Derived from current Databricks user email via SDK:
         robert.lee@databricks.com  ->  robert_lee
      4. Falls back to "default" if SDK call fails

    Args:
        table_name: bare name, schema.table, or already-qualified catalog.schema.table.
        catalog:    Unity Catalog catalog (default: main, or ZEROBUS_CATALOG env var).
        schema:     Unity Catalog schema  (default: from ZEROBUS_SCHEMA or current user).
        host:       Override workspace host (default: from ~/.databrickscfg).
        profile:    ~/.databrickscfg profile (default: DEFAULT section).

    Returns:
        Fully-qualified catalog.schema.table string.
    """
    if table_name.count(".") == 2:
        return table_name

    effective_catalog = catalog or os.environ.get("ZEROBUS_CATALOG") or "main"

    if schema is None:
        schema = os.environ.get("ZEROBUS_SCHEMA")

    if schema is None:
        try:
            from databricks.sdk import WorkspaceClient
            kwargs: dict = {}
            if host:
                kwargs["host"] = host
            if profile:
                kwargs["profile"] = profile
            w = WorkspaceClient(**kwargs)
            username = w.current_user.me().user_name or ""
            schema = _schema_from_username(username) if username else "default"
        except Exception:
            schema = "default"

    if table_name.count(".") == 1:
        return f"{effective_catalog}.{table_name}"

    return f"{effective_catalog}.{schema}.{table_name}"


def build_zerobus_endpoint(
    host: Optional[str] = None,
    profile: Optional[str] = None,
) -> str:
    """
    Auto-construct the ZeroBus gRPC endpoint URL from the workspace hostname.

    Formula:  https://{workspace_id}.zerobus.{workspace_hostname}

    Both workspace_id and hostname are derived from ~/.databrickscfg via the
    Databricks SDK — no region or other parameters needed.

    Example:
        workspace: https://e2-dogfood.staging.cloud.databricks.com
        zerobus:   https://6051921418418893.zerobus.e2-dogfood.staging.cloud.databricks.com

    Args:
        host:    Override workspace host (default: from ~/.databrickscfg).
        profile: ~/.databrickscfg profile name (default: DEFAULT section).

    Returns:
        Full ZeroBus endpoint URL.

    Raises:
        ImportError: if databricks-sdk is not installed.
    """
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        raise ImportError("databricks-sdk is not installed. Run: pip install databricks-sdk")

    kwargs: dict = {}
    if host:
        kwargs["host"] = host
    if profile:
        kwargs["profile"] = profile

    w = WorkspaceClient(**kwargs)
    workspace_id = w.get_workspace_id()
    # Strip the workspace-specific first label from the hostname.
    # e2-dogfood.staging.cloud.databricks.com -> staging.cloud.databricks.com
    # dbc-xxxx.us-west-2.cloud.databricks.com -> us-west-2.cloud.databricks.com
    # This matches the TLS wildcard cert (*.staging.cloud.databricks.com)
    # and the documented endpoint format ({id}.zerobus.{region}.cloud.databricks.com).
    hostname = w.config.hostname
    domain = hostname.split(".", 1)[1] if "." in hostname else hostname
    return f"https://{workspace_id}.zerobus.{domain}"

# ---------------------------------------------------------------------------
# IngestConfig
# ---------------------------------------------------------------------------

@dataclass
class IngestConfig:
    """
    Connection and auth parameters for a ZeroBus ingest session.

    Fields
    ------
    server_endpoint  : ZeroBus gRPC endpoint URL
    workspace_url    : Databricks workspace URL (for SDK init)
    table_name       : Fully-qualified table (catalog.schema.table)
    batch_size       : Rows per ingest_records_offset call (default 500)
    client_id        : Service principal app ID  (from_env mode only)
    client_secret    : Service principal secret   (from_env mode only)
    headers_provider : DatabricksSdkHeadersProvider (SDK mode) or None
    """

    server_endpoint: str
    workspace_url: str
    table_name: str
    batch_size: int = 500
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    headers_provider: Any = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # SDK auth (default for local dev — uses ~/.databrickscfg)
    # ------------------------------------------------------------------

    @classmethod
    def from_workspace_client(
        cls,
        table_name: str,
        *,
        server_endpoint: Optional[str] = None,
        host: Optional[str] = None,
        profile: Optional[str] = None,
        lifetime_seconds: Optional[int] = None,
        batch_size: int = 500,
    ) -> "IngestConfig":
        """
        Build IngestConfig using the Databricks Python SDK — no service principal needed.

        WorkspaceClient() automatically picks up credentials from ~/.databrickscfg,
        DATABRICKS_HOST + DATABRICKS_TOKEN, or any other SDK-supported auth source.
        The same credentials already used by Databricks Connect just work.

        The ZeroBus endpoint is resolved in order:
          1. server_endpoint argument
          2. ZEROBUS_SERVER_ENDPOINT env var
          3. Auto-constructed: https://{workspace_id}.zerobus.{workspace_hostname}
             (workspace_id and hostname both come from ~/.databrickscfg via SDK)

        Args:
            table_name:       Fully-qualified table name (catalog.schema.table).
            server_endpoint:  ZeroBus endpoint URL (overrides auto-construction).
            host:             Override workspace host (default: from SDK config).
            profile:          ~/.databrickscfg profile (default: DEFAULT section).
            lifetime_seconds: If set, creates a short-lived PAT that auto-expires
                              (e.g. 300 = 5 min).  If None, uses the current
                              session OAuth token (SDK handles refresh).
            batch_size:       Rows per batch (default 500).

        Raises:
            ImportError: if databricks-sdk is not installed.
        """
        workspace_host = _host_from_workspace_client(host=host, profile=profile)
        endpoint = (
            server_endpoint
            or os.environ.get("ZEROBUS_SERVER_ENDPOINT", "")
            or build_zerobus_endpoint(host=workspace_host, profile=profile)
        )
        return cls(
            server_endpoint=endpoint,
            workspace_url=workspace_host,
            table_name=table_name,
            batch_size=batch_size,
            headers_provider=DatabricksSdkHeadersProvider(
                table_name=table_name,
                host=workspace_host,
                profile=profile,
                lifetime_seconds=lifetime_seconds,
            ),
        )

    # ------------------------------------------------------------------
    # Service-principal auth (CI / production)
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, table_name: Optional[str] = None) -> "IngestConfig":
        """
        Build IngestConfig from standard ZeroBus environment variables.

        Required env vars:
            DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET,
            ZEROBUS_SERVER_ENDPOINT, DATABRICKS_WORKSPACE_URL

        Optional:
            ZEROBUS_TABLE_NAME  (or pass table_name directly)
        """
        def _require(var: str) -> str:
            val = os.environ.get(var)
            if not val:
                raise EnvironmentError(
                    f"Required environment variable {var!r} is not set. "
                    "See docs/implementation.md § Environment Variables."
                )
            return val

        return cls(
            server_endpoint=_require("ZEROBUS_SERVER_ENDPOINT"),
            workspace_url=_require("DATABRICKS_WORKSPACE_URL"),
            client_id=_require("DATABRICKS_CLIENT_ID"),
            client_secret=_require("DATABRICKS_CLIENT_SECRET"),
            table_name=table_name or _require("ZEROBUS_TABLE_NAME"),
        )


# ---------------------------------------------------------------------------
# IngestResult
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    """Summary of a completed ingest operation."""

    table_name: str
    rows_sent: int
    offsets: list[Optional[int]] = field(default_factory=list)

    @property
    def batch_count(self) -> int:
        return len(self.offsets)


# ---------------------------------------------------------------------------
# ingest_dataframe
# ---------------------------------------------------------------------------

def ingest_dataframe(
    df: Any,
    table: CanonicalTableSchema,
    config: IngestConfig,
    *,
    batch_size: Optional[int] = None,
) -> IngestResult:
    """
    Serialize every row of a Spark DataFrame to protobuf and ingest into ZeroBus.

    Auth priority:
      - config.headers_provider set (from_workspace_client) → CLI/SDK OAuth
      - config.client_id / client_secret set (from_env) → service principal
    """
    if ZerobusSdk is None:
        raise ImportError(
            "The 'zerobus' package is not installed. "
            "Run: pip install databricks-zerobus-ingest-sdk"
        )

    effective_batch_size = batch_size or config.batch_size

    proto_str = schema_to_proto_str(table)
    msg_class = compile_proto(proto_str, table.name)
    descriptor_bytes = msg_class.DESCRIPTOR.file.serialized_pb
    table_props = TableProperties(config.table_name, descriptor_bytes)

    options = StreamConfigurationOptions(
        record_type=RecordType.PROTO,
        max_inflight_records=1000,
        recovery=True,
        recovery_timeout_ms=15000,
        recovery_backoff_ms=2000,
        recovery_retries=3,
    )

    sdk = ZerobusSdk(host=config.server_endpoint, unity_catalog_url=config.workspace_url)

    if config.headers_provider is not None:
        # SDK auth: pass headers_provider; client_id/secret not needed
        stream = sdk.create_stream(
            client_id="",
            client_secret="",
            table_properties=table_props,
            options=options,
            headers_provider=config.headers_provider,
        )
    else:
        if not config.client_id or not config.client_secret:
            raise ValueError(
                "IngestConfig has neither headers_provider nor client_id/client_secret. "
                "Use IngestConfig.from_workspace_client() or IngestConfig.from_env()."
            )
        stream = sdk.create_stream(
            client_id=config.client_id,
            client_secret=config.client_secret,
            table_properties=table_props,
            options=options,
        )

    rows_sent = 0
    offsets: list[Optional[int]] = []

    try:
        batch: list[bytes] = []
        for row in df.toLocalIterator():
            batch.append(serialize_row(row, msg_class))
            if len(batch) >= effective_batch_size:
                offset = stream.ingest_records_offset(batch)
                offsets.append(offset)
                rows_sent += len(batch)
                batch = []

        if batch:
            offset = stream.ingest_records_offset(batch)
            offsets.append(offset)
            rows_sent += len(batch)

        stream.flush()
    finally:
        stream.close()

    return IngestResult(table_name=config.table_name, rows_sent=rows_sent, offsets=offsets)
