"""
pytest tests for src/zerobus_ingest.py

Unit tests use unittest.mock to stub the ZeroBus SDK and Databricks SDK —
no real credentials or network access needed.

Integration tests are skipped unless ZEROBUS_TABLE_NAME is set.
Endpoint is auto-constructed from ~/.databrickscfg — no ZEROBUS_SERVER_ENDPOINT needed.

Test groups:
  TestSchemaFromUsername            - _schema_from_username() email -> schema derivation
  TestBuildQualifiedTableName       - build_qualified_table_name() FQN construction
  TestDatabricksSdkHeadersProvider  - get_headers(), session token, short-lived PAT
  TestGetSdkToken                   - WorkspaceClient dispatch, lifetime_seconds, errors
  TestHostFromWorkspaceClient       - host resolution (env var, SDK config)
  TestIngestConfig                  - from_workspace_client(), from_env(), missing vars
  TestIngestResult                  - dataclass behaviour
  TestIngestDataframe               - mock SDK: SDK auth, service-principal auth,
                                      batching, flush/close lifecycle, offsets
  TestIngestIntegration             - real ZeroBus call (skipped without credentials)
"""

import os
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.statschema.model import CanonicalColumn, CanonicalTableSchema
from pathlib import Path

from src.zbhelper.zerobus_ingest import (
    DatabricksSdkHeadersProvider,
    IngestConfig,
    IngestResult,
    _get_sdk_token,
    _host_from_workspace_client,
    _schema_from_username,
    build_qualified_table_name,
    ingest_dataframe,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _simple_table() -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name="phase1_table",
        columns=[CanonicalColumn(name="col_1", type="integer")],
    )


def _multi_col_table() -> CanonicalTableSchema:
    return CanonicalTableSchema(
        name="order",
        columns=[
            CanonicalColumn(name="order_id", type="integer"),
            CanonicalColumn(name="customer", type="string"),
            CanonicalColumn(name="amount", type="double"),
        ],
    )


class _FakeRow:
    def __init__(self, **kwargs):
        self._data = kwargs

    def asDict(self):
        return dict(self._data)


def _fake_df(rows: list[dict]):
    df = MagicMock()
    df.toLocalIterator.return_value = iter([_FakeRow(**r) for r in rows])
    return df


def _mock_sdk_patch(offsets=None):
    """Return a patch.multiple context that stubs the full ZeroBus SDK."""
    if offsets is None:
        offsets = list(range(1, 100))

    mock_stream = MagicMock()
    mock_stream.ingest_records_offset.side_effect = iter(offsets)

    mock_sdk_instance = MagicMock()
    mock_sdk_instance.create_stream.return_value = mock_stream
    # create_stream handles both SDK auth (headers_provider=) and SP auth (client_id/secret)

    class MockRecordType:
        PROTO = "PROTO"

    return (
        patch.multiple(
            "src.zbhelper.zerobus_ingest",
            ZerobusSdk=MagicMock(return_value=mock_sdk_instance),
            TableProperties=MagicMock(),
            StreamConfigurationOptions=MagicMock(),
            RecordType=MockRecordType,
        ),
        mock_stream,
        mock_sdk_instance,
    )


# ---------------------------------------------------------------------------
# TestDatabricksSdkHeadersProvider
# ---------------------------------------------------------------------------

class TestDatabricksSdkHeadersProvider:
    def test_get_headers_returns_authorization_and_table(self):
        provider = DatabricksSdkHeadersProvider(table_name="main.default.t")
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", return_value="tok123"):
            headers = provider.get_headers()
        header_dict = dict(headers)
        assert header_dict["authorization"] == "Bearer tok123"
        assert header_dict["x-databricks-zerobus-table-name"] == "main.default.t"

    def test_get_headers_passes_host(self):
        provider = DatabricksSdkHeadersProvider(table_name="c.s.t", host="https://ws")
        captured = {}
        def fake_token(**kw):
            captured.update(kw)
            return "t"
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", side_effect=fake_token):
            provider.get_headers()
        assert captured["host"] == "https://ws"

    def test_get_headers_passes_profile(self):
        provider = DatabricksSdkHeadersProvider(table_name="c.s.t", profile="myprofile")
        captured = {}
        def fake_token(**kw):
            captured.update(kw)
            return "t"
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", side_effect=fake_token):
            provider.get_headers()
        assert captured["profile"] == "myprofile"

    def test_get_headers_passes_lifetime_seconds(self):
        provider = DatabricksSdkHeadersProvider(table_name="c.s.t", lifetime_seconds=300)
        captured = {}
        def fake_token(**kw):
            captured.update(kw)
            return "t"
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", side_effect=fake_token):
            provider.get_headers()
        assert captured["lifetime_seconds"] == 300

    def test_get_headers_called_fresh_each_time(self):
        """Token is fetched on every call — SDK handles caching/refresh."""
        provider = DatabricksSdkHeadersProvider(table_name="c.s.t")
        n = {"count": 0}
        def fresh_token(**kw):
            n["count"] += 1
            return f"tok{n['count']}"
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", side_effect=fresh_token):
            h1 = dict(provider.get_headers())
            h2 = dict(provider.get_headers())
        assert h1["authorization"] == "Bearer tok1"
        assert h2["authorization"] == "Bearer tok2"
        assert n["count"] == 2

    def test_no_lifetime_uses_session_token_by_default(self):
        provider = DatabricksSdkHeadersProvider(table_name="c.s.t")
        captured = {}
        def fake_token(**kw):
            captured.update(kw)
            return "t"
        with patch("src.zbhelper.zerobus_ingest._get_sdk_token", side_effect=fake_token):
            provider.get_headers()
        assert captured.get("lifetime_seconds") is None


# ---------------------------------------------------------------------------
# TestGetSdkToken
# ---------------------------------------------------------------------------

class TestGetSdkToken:
    def _mock_workspace_client(self, auth_header="Bearer sessiontok"):
        mock_config = MagicMock()
        mock_config.authenticate.return_value = {"Authorization": auth_header}
        mock_config.host = "https://ws.databricks.com"
        mock_client = MagicMock()
        mock_client.config = mock_config
        return MagicMock(return_value=mock_client), mock_client

    def test_session_token_from_authenticate(self):
        WC, wc = self._mock_workspace_client("Bearer mytoken")
        with patch("databricks.sdk.WorkspaceClient", WC):
            token = _get_sdk_token()
        assert token == "mytoken"

    def test_lifetime_seconds_creates_pat(self):
        WC, wc = self._mock_workspace_client()
        wc.tokens.create.return_value = MagicMock(token_value="shortlived_pat")
        with patch("databricks.sdk.WorkspaceClient", WC):
            token = _get_sdk_token(lifetime_seconds=300)
        assert token == "shortlived_pat"
        wc.tokens.create.assert_called_once_with(
            comment="zerobus-ingest-temp",
            lifetime_seconds=300,
        )

    def test_lifetime_seconds_does_not_call_authenticate(self):
        WC, wc = self._mock_workspace_client()
        wc.tokens.create.return_value = MagicMock(token_value="t")
        with patch("databricks.sdk.WorkspaceClient", WC):
            _get_sdk_token(lifetime_seconds=60)
        wc.config.authenticate.assert_not_called()

    def test_session_token_does_not_create_pat(self):
        WC, wc = self._mock_workspace_client("Bearer tok")
        with patch("databricks.sdk.WorkspaceClient", WC):
            _get_sdk_token()
        wc.tokens.create.assert_not_called()

    def test_host_forwarded_to_workspace_client(self):
        WC, _ = self._mock_workspace_client()
        with patch("databricks.sdk.WorkspaceClient", WC):
            _get_sdk_token(host="https://custom.databricks.com")
        WC.assert_called_once_with(host="https://custom.databricks.com")

    def test_profile_forwarded_to_workspace_client(self):
        WC, _ = self._mock_workspace_client()
        with patch("databricks.sdk.WorkspaceClient", WC):
            _get_sdk_token(profile="myprofile")
        WC.assert_called_once_with(profile="myprofile")

    def test_no_bearer_raises_runtime_error(self):
        WC, wc = self._mock_workspace_client(auth_header="")
        with patch("databricks.sdk.WorkspaceClient", WC):
            with pytest.raises(RuntimeError, match="Bearer token"):
                _get_sdk_token()

    def test_missing_sdk_raises_import_error(self):
        with patch.dict("sys.modules", {"databricks.sdk": None}):
            with pytest.raises((ImportError, RuntimeError)):
                _get_sdk_token()


# ---------------------------------------------------------------------------
# TestHostFromWorkspaceClient
# ---------------------------------------------------------------------------

class TestHostFromWorkspaceClient:
    def test_explicit_host_returned_directly(self):
        host = _host_from_workspace_client(host="https://explicit.databricks.com")
        assert host == "https://explicit.databricks.com"

    def test_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://env-host.databricks.com")
        host = _host_from_workspace_client()
        assert host == "https://env-host.databricks.com"

    def test_reads_host_from_sdk_config(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        mock_config = MagicMock()
        mock_config.host = "https://sdk-host.databricks.com"
        mock_wc = MagicMock()
        mock_wc.config = mock_config
        WC = MagicMock(return_value=mock_wc)
        with patch("databricks.sdk.WorkspaceClient", WC):
            host = _host_from_workspace_client()
        assert host == "https://sdk-host.databricks.com"

    def test_profile_forwarded(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        mock_config = MagicMock()
        mock_config.host = "https://h"
        mock_wc = MagicMock()
        mock_wc.config = mock_config
        WC = MagicMock(return_value=mock_wc)
        with patch("databricks.sdk.WorkspaceClient", WC):
            _host_from_workspace_client(profile="myprofile")
        WC.assert_called_once_with(profile="myprofile")


# ---------------------------------------------------------------------------
# TestIngestConfig
# ---------------------------------------------------------------------------

class TestIngestConfig:
    _ENV_VARS = {
        "ZEROBUS_SERVER_ENDPOINT": "https://ws.zerobus.us-east-1.cloud.databricks.com",
        "DATABRICKS_WORKSPACE_URL": "https://ws.cloud.databricks.com",
        "DATABRICKS_CLIENT_ID": "my-client-id",
        "DATABRICKS_CLIENT_SECRET": "my-secret",
        "ZEROBUS_TABLE_NAME": "main.default.phase1_table",
    }

    # --- from_env (service-principal mode) ---

    def test_from_env_all_vars_set(self, monkeypatch):
        for k, v in self._ENV_VARS.items():
            monkeypatch.setenv(k, v)
        config = IngestConfig.from_env()
        assert config.server_endpoint == self._ENV_VARS["ZEROBUS_SERVER_ENDPOINT"]
        assert config.client_id == self._ENV_VARS["DATABRICKS_CLIENT_ID"]
        assert config.headers_provider is None

    def test_from_env_table_name_override(self, monkeypatch):
        for k, v in self._ENV_VARS.items():
            monkeypatch.setenv(k, v)
        config = IngestConfig.from_env(table_name="main.default.override")
        assert config.table_name == "main.default.override"

    @pytest.mark.parametrize("missing_var", [
        "ZEROBUS_SERVER_ENDPOINT", "DATABRICKS_WORKSPACE_URL",
        "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "ZEROBUS_TABLE_NAME",
    ])
    def test_from_env_missing_var_raises(self, monkeypatch, missing_var):
        for k, v in self._ENV_VARS.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv(missing_var)
        with pytest.raises(EnvironmentError, match=missing_var):
            IngestConfig.from_env()

    # --- from_workspace_client ---

    def _patched_client(self, monkeypatch):
        """Patch both host resolution and build_zerobus_endpoint for unit tests."""
        monkeypatch.delenv("ZEROBUS_SERVER_ENDPOINT", raising=False)
        return (
            patch("src.zbhelper.zerobus_ingest._host_from_workspace_client", return_value="https://ws.databricks.com"),
            patch("src.zbhelper.zerobus_ingest.build_zerobus_endpoint", return_value="https://123.zerobus.ws.databricks.com"),
        )

    def test_from_workspace_client_sets_headers_provider(self, monkeypatch):
        p_host, p_ep = self._patched_client(monkeypatch)
        with p_host, p_ep:
            config = IngestConfig.from_workspace_client("main.default.t")
        assert isinstance(config.headers_provider, DatabricksSdkHeadersProvider)
        assert config.client_id is None

    def test_from_workspace_client_no_lifetime_by_default(self, monkeypatch):
        p_host, p_ep = self._patched_client(monkeypatch)
        with p_host, p_ep:
            config = IngestConfig.from_workspace_client("main.default.t")
        assert config.headers_provider._lifetime_seconds is None

    def test_from_workspace_client_lifetime_seconds_forwarded(self, monkeypatch):
        p_host, p_ep = self._patched_client(monkeypatch)
        with p_host, p_ep:
            config = IngestConfig.from_workspace_client("main.default.t", lifetime_seconds=300)
        assert config.headers_provider._lifetime_seconds == 300

    def test_from_workspace_client_explicit_endpoint(self, monkeypatch):
        monkeypatch.delenv("ZEROBUS_SERVER_ENDPOINT", raising=False)
        with patch("src.zbhelper.zerobus_ingest._host_from_workspace_client", return_value="https://ws"):
            config = IngestConfig.from_workspace_client("main.default.t", server_endpoint="https://ep")
        assert config.server_endpoint == "https://ep"

    def test_from_workspace_client_env_endpoint_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("ZEROBUS_SERVER_ENDPOINT", "https://from-env")
        with patch("src.zbhelper.zerobus_ingest._host_from_workspace_client", return_value="https://ws"), \
             patch("src.zbhelper.zerobus_ingest.build_zerobus_endpoint") as mock_build:
            config = IngestConfig.from_workspace_client("main.default.t")
        assert config.server_endpoint == "https://from-env"
        mock_build.assert_not_called()

    def test_from_workspace_client_auto_constructs_endpoint(self, monkeypatch):
        """When ZEROBUS_SERVER_ENDPOINT is unset, endpoint is auto-constructed."""
        monkeypatch.delenv("ZEROBUS_SERVER_ENDPOINT", raising=False)
        with patch("src.zbhelper.zerobus_ingest._host_from_workspace_client", return_value="https://ws"), \
             patch("src.zbhelper.zerobus_ingest.build_zerobus_endpoint", return_value="https://123.zerobus.ws.databricks.com"):
            config = IngestConfig.from_workspace_client("main.default.t")
        assert config.server_endpoint == "https://123.zerobus.ws.databricks.com"

    def test_default_batch_size(self):
        config = IngestConfig(server_endpoint="https://ep", workspace_url="https://ws", table_name="c.s.t")
        assert config.batch_size == 500


# ---------------------------------------------------------------------------
# TestSchemaFromUsername
# ---------------------------------------------------------------------------

class TestSchemaFromUsername:
    def test_email_dots_replaced(self):
        assert _schema_from_username("robert.lee@databricks.com") == "robert_lee"

    def test_email_hyphens_replaced(self):
        assert _schema_from_username("robert-lee@company.com") == "robert_lee"

    def test_domain_stripped(self):
        assert _schema_from_username("user@example.com") == "user"

    def test_no_at_sign(self):
        assert _schema_from_username("plain_user") == "plain_user"

    def test_lowercased(self):
        assert _schema_from_username("Robert.Lee@Databricks.com") == "robert_lee"


# ---------------------------------------------------------------------------
# TestBuildQualifiedTableName
# ---------------------------------------------------------------------------

class TestBuildQualifiedTableName:
    def test_already_qualified_returned_unchanged(self):
        assert build_qualified_table_name("main.default.orders") == "main.default.orders"

    def test_explicit_catalog_and_schema(self):
        assert build_qualified_table_name("orders", catalog="dev", schema="myschema") == "dev.myschema.orders"

    def test_default_catalog_is_main(self):
        result = build_qualified_table_name("orders", schema="myschema")
        assert result.startswith("main.")

    def test_schema_table_form_prepends_catalog(self):
        assert build_qualified_table_name("myschema.orders", catalog="dev") == "dev.myschema.orders"

    def test_bare_table_derives_schema_from_sdk(self, monkeypatch):
        monkeypatch.delenv("ZEROBUS_SCHEMA", raising=False)
        mock_me = MagicMock()
        mock_me.user_name = "alice.bob@databricks.com"
        mock_wc = MagicMock()
        mock_wc.current_user.me.return_value = mock_me
        with patch("databricks.sdk.WorkspaceClient", return_value=mock_wc):
            result = build_qualified_table_name("orders")
        assert result == "main.alice_bob.orders"

    def test_sdk_failure_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("ZEROBUS_SCHEMA", raising=False)
        with patch("databricks.sdk.WorkspaceClient", side_effect=Exception("no creds")):
            result = build_qualified_table_name("orders")
        assert result == "main.default.orders"

    def test_explicit_schema_skips_sdk_call(self, monkeypatch):
        monkeypatch.delenv("ZEROBUS_SCHEMA", raising=False)
        with patch("databricks.sdk.WorkspaceClient") as mock_wc_cls:
            result = build_qualified_table_name("orders", schema="explicit")
        mock_wc_cls.assert_not_called()
        assert result == "main.explicit.orders"
    
    def test_zerobus_schema_env_var_used(self, monkeypatch):
        monkeypatch.setenv("ZEROBUS_SCHEMA", "project_schema")
        with patch("databricks.sdk.WorkspaceClient") as mock_wc_cls:
            result = build_qualified_table_name("orders")
        mock_wc_cls.assert_not_called()
        assert result == "main.project_schema.orders"


# ---------------------------------------------------------------------------
# TestIngestResult
# ---------------------------------------------------------------------------

class TestIngestResult:
    def test_batch_count(self):
        assert IngestResult(table_name="c.s.t", rows_sent=1000, offsets=[1, 2, 3]).batch_count == 3

    def test_batch_count_empty(self):
        assert IngestResult(table_name="c.s.t", rows_sent=0).batch_count == 0


# ---------------------------------------------------------------------------
# TestIngestDataframe — mock SDK, no credentials
# ---------------------------------------------------------------------------

class TestIngestDataframe:
    def _sdk_config(self, batch_size=500) -> IngestConfig:
        return IngestConfig(
            server_endpoint="https://fake.zerobus.databricks.com",
            workspace_url="https://fake.databricks.com",
            table_name="main.default.phase1_table",
            batch_size=batch_size,
            headers_provider=MagicMock(),
        )

    def _sp_config(self, batch_size=500) -> IngestConfig:
        return IngestConfig(
            server_endpoint="https://fake.zerobus.databricks.com",
            workspace_url="https://fake.databricks.com",
            table_name="main.default.phase1_table",
            batch_size=batch_size,
            client_id="fake-id",
            client_secret="fake-secret",
        )

    def test_sdk_auth_uses_headers_provider(self):
        pytest.importorskip("grpc_tools")
        ctx, _, mock_sdk = _mock_sdk_patch(offsets=[1])
        with ctx:
            ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), self._sdk_config())
        call_kwargs = mock_sdk.create_stream.call_args.kwargs
        assert call_kwargs.get("headers_provider") is not None
        assert call_kwargs.get("client_id") == "" 

    def test_sp_auth_uses_create_stream(self):
        pytest.importorskip("grpc_tools")
        ctx, _, mock_sdk = _mock_sdk_patch(offsets=[1])
        with ctx:
            ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), self._sp_config())
        call_kwargs = mock_sdk.create_stream.call_args.kwargs
        assert call_kwargs.get("headers_provider") is None
        assert call_kwargs.get("client_id") != "" 

    def test_sp_auth_passes_credentials(self):
        pytest.importorskip("grpc_tools")
        ctx, _, mock_sdk = _mock_sdk_patch(offsets=[1])
        config = self._sp_config()
        with ctx:
            ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), config)
        kwargs = mock_sdk.create_stream.call_args.kwargs
        assert kwargs["client_id"] == config.client_id
        assert kwargs["client_secret"] == config.client_secret

    def test_missing_auth_raises(self):
        pytest.importorskip("grpc_tools")
        ctx, _, _ = _mock_sdk_patch(offsets=[1])
        config = IngestConfig(server_endpoint="https://ep", workspace_url="https://ws", table_name="c.s.t")
        with ctx:
            with pytest.raises(ValueError, match="headers_provider"):
                ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), config)

    def test_rows_sent_matches_dataframe_size(self):
        pytest.importorskip("grpc_tools")
        ctx, _, _ = _mock_sdk_patch(offsets=[10])
        with ctx:
            result = ingest_dataframe(_fake_df([{"col_1": i} for i in range(10)]), _simple_table(), self._sdk_config())
        assert result.rows_sent == 10

    def test_batch_size_respected(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[1, 2, 3])
        with ctx:
            result = ingest_dataframe(_fake_df([{"col_1": i} for i in range(25)]), _simple_table(), self._sdk_config(batch_size=10))
        assert mock_stream.ingest_records_offset.call_count == 3
        assert result.rows_sent == 25

    def test_batch_size_kwarg_overrides_config(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[1, 2])
        with ctx:
            result = ingest_dataframe(_fake_df([{"col_1": i} for i in range(20)]), _simple_table(), self._sdk_config(batch_size=500), batch_size=15)
        assert mock_stream.ingest_records_offset.call_count == 2

    def test_flush_before_close(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[1])
        order = []
        mock_stream.flush.side_effect = lambda: order.append("flush")
        mock_stream.close.side_effect = lambda: order.append("close")
        with ctx:
            ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), self._sdk_config())
        assert order == ["flush", "close"]

    def test_close_called_on_exception(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch()
        mock_stream.ingest_records_offset.side_effect = RuntimeError("network error")
        with ctx:
            with pytest.raises(RuntimeError, match="network error"):
                ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), self._sdk_config())
        mock_stream.close.assert_called_once()

    def test_empty_dataframe(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[])
        with ctx:
            result = ingest_dataframe(_fake_df([]), _simple_table(), self._sdk_config())
        assert result.rows_sent == 0
        mock_stream.ingest_records_offset.assert_not_called()
        mock_stream.flush.assert_called_once()
        mock_stream.close.assert_called_once()

    def test_offsets_collected(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[10, 20, 30])
        with ctx:
            result = ingest_dataframe(_fake_df([{"col_1": i} for i in range(3)]), _simple_table(), self._sdk_config(batch_size=1))
        assert result.offsets == [10, 20, 30]

    def test_sdk_initialized_with_endpoint(self):
        pytest.importorskip("grpc_tools")
        mock_stream = MagicMock()
        mock_stream.ingest_records_offset.return_value = 1
        mock_sdk = MagicMock()
        mock_sdk.create_stream.return_value = mock_stream
        ZerobusSdk_mock = MagicMock(return_value=mock_sdk)

        class MockRecordType:
            PROTO = "PROTO"

        config = self._sdk_config()
        with patch.multiple("src.zbhelper.zerobus_ingest", ZerobusSdk=ZerobusSdk_mock,
                            TableProperties=MagicMock(), StreamConfigurationOptions=MagicMock(),
                            RecordType=MockRecordType):
            ingest_dataframe(_fake_df([{"col_1": 1}]), _simple_table(), config)

        ZerobusSdk_mock.assert_called_once_with(host=config.server_endpoint, unity_catalog_url=config.workspace_url)

    def test_multi_column_table(self):
        pytest.importorskip("grpc_tools")
        ctx, mock_stream, _ = _mock_sdk_patch(offsets=[1])
        with ctx:
            result = ingest_dataframe(
                _fake_df([{"order_id": 1, "customer": "Alice", "amount": 9.99}]),
                _multi_col_table(), self._sdk_config())
        assert result.rows_sent == 1
        sent = mock_stream.ingest_records_offset.call_args[0][0]
        assert isinstance(sent, list) and all(isinstance(b, bytes) for b in sent)


# ---------------------------------------------------------------------------
# TestIngestIntegration
# ---------------------------------------------------------------------------

def _integration_available() -> bool:
    """
    True when both Databricks credentials and ZeroBus endpoint can be resolved.

    The endpoint is auto-constructed from ~/.databrickscfg but may not be reachable
    on all workspace types (e.g. internal staging). Set ZEROBUS_SERVER_ENDPOINT to
    override and force-enable integration tests on non-standard workspaces.
    """
    if not (Path.home() / ".databrickscfg").exists():
        return False
    # Allow explicit override to force-enable on staging / non-standard workspaces
    if os.environ.get("ZEROBUS_SERVER_ENDPOINT"):
        return True
    # Auto-constructed endpoint: only enable for known production domains
    try:
        from databricks.sdk import WorkspaceClient
        hostname = WorkspaceClient().config.hostname
        # Skip internal staging/dogfood workspaces — ZeroBus is production-only
        return hostname.endswith(".cloud.databricks.com") and not any(
            part in hostname for part in ["staging", "dogfood", "e2-"]
        )
    except Exception:
        return False


@pytest.mark.skipif(
    not _integration_available(),
    reason=(
        "Integration tests require a production Databricks workspace with ZeroBus available. "
        "Prerequisites: ~/.databrickscfg configured (databricks auth login --configure-serverless). "
        "For staging/non-standard workspaces set ZEROBUS_SERVER_ENDPOINT explicitly."
    ),
)
class TestIngestIntegration:
    """
    End-to-end tests against a real ZeroBus endpoint.

    Auth: IngestConfig.from_workspace_client() uses ~/.databrickscfg.
    Endpoint and table name are both auto-constructed — no env vars required.

    Table name: build_qualified_table_name(table.name)
      - catalog : main  (or ZEROBUS_CATALOG env var)
      - schema  : from ZEROBUS_SCHEMA env var, or derived from current user email
                  e.g. export ZEROBUS_SCHEMA=robert_lee_zerobus_unittest

    To override endpoint (e.g. staging workspace):
      export ZEROBUS_SERVER_ENDPOINT=https://{id}.zerobus.staging.cloud.databricks.com

    Prerequisites:
      - Target Delta table exists in Unity Catalog with MODIFY + SELECT granted.
    """

    def _get_config(self, table: "CanonicalTableSchema", lifetime_seconds: Optional[int] = 300) -> IngestConfig:
        table_fqn = build_qualified_table_name(table.name)
        if os.environ.get("DATABRICKS_CLIENT_ID"):
            return IngestConfig.from_env(table_name=table_fqn)
        return IngestConfig.from_workspace_client(
            table_name=table_fqn,
            lifetime_seconds=lifetime_seconds,
        )

    def _get_spark(self):
        try:
            from databricks.connect import DatabricksSession
            return DatabricksSession.builder.getOrCreate()
        except Exception:
            pass
        pyspark = pytest.importorskip("pyspark")
        saved = {k: os.environ.pop(k) for k in list(os.environ)
                 if k == "SPARK_REMOTE" or k.startswith("DATABRICKS_CONNECT")}
        try:
            return pyspark.sql.SparkSession.builder.master("local[1]") \
                .appName("zerobus_integration").getOrCreate()
        finally:
            os.environ.update(saved)

    def test_phase1_ingest_snapshot(self):
        """1000 rows with short-lived PAT (lifetime_seconds=300)."""
        pytest.importorskip("dbldatagen")
        pytest.importorskip("grpc_tools")
        from src.statschema.dbldatagen_builder import build_dataframe_from_canonical

        table = CanonicalTableSchema(
            name="phase1_table",
            columns=[CanonicalColumn(name="col_1", type="integer")],
        )
        spark = self._get_spark()
        df = build_dataframe_from_canonical(spark, table, rows=1000, partitions=2, seed=42)
        result = ingest_dataframe(df, table, self._get_config(table, lifetime_seconds=300))
        assert result.rows_sent == 1000
        assert result.batch_count >= 1

    def test_phase1_ingest_incremental(self):
        """100 incremental rows with session OAuth (no PAT created)."""
        pytest.importorskip("dbldatagen")
        pytest.importorskip("grpc_tools")
        from src.statschema.dbldatagen_builder import build_dataframe_from_canonical

        table = CanonicalTableSchema(
            name="phase1_table",
            columns=[CanonicalColumn(name="col_1", type="integer")],
        )
        spark = self._get_spark()
        df = build_dataframe_from_canonical(spark, table, rows=100, partitions=1, seed=99)
        result = ingest_dataframe(df, table, self._get_config(table, lifetime_seconds=None))
        assert result.rows_sent == 100
