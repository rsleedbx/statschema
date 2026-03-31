"""
Tests for ConnectionProfile and DeploymentContext.from_profile().

All tests run offline (no DB, no network) — secrets are mocked.
"""

from __future__ import annotations

import textwrap
import tempfile
from unittest.mock import patch

import pytest
from omegaconf import MissingMandatoryValue, OmegaConf

from statschema.connection_profile import ConnectionProfile, Dialect, load_profile
from statschema.loader_context import DeploymentContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path, content: str) -> str:
    p = tmp_path / "statschema.yaml"
    p.write_text(textwrap.dedent(content))
    return str(p)


# ---------------------------------------------------------------------------
# load_profile — happy path
# ---------------------------------------------------------------------------

class TestLoadProfileHappyPath:
    def test_minimal_required_fields(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              dev:
                dialect: postgres
                host: localhost
                loader: client_stream
        """)
        profile = load_profile(yaml_path, "dev")
        assert profile.dialect == Dialect.POSTGRES
        assert profile.host == "localhost"
        assert profile.loader == "client_stream"
        assert profile.port is None
        assert profile.database is None
        assert profile.tls is False

    def test_all_optional_fields(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              full:
                dialect: sqlserver
                host: db.example.com
                loader: native_bulk
                port: 1433
                database: mydb
                username: sa
                password: secret
                tls: true
                version: "2022"
                client_staging_dir: /tmp/client
                server_staging_dir: /tmp/server
                oracle_directory: MY_DIR
                oracle_sqlldr_binary: /usr/bin/sqlldr
                cloud_staging_uri: s3://bucket/prefix/
                cloud_format: csv
                topology: shared_fs
                server_is_emulated: true
        """)
        profile = load_profile(yaml_path, "full")
        assert profile.dialect == Dialect.SQLSERVER
        assert profile.port == 1433
        assert profile.tls is True
        assert profile.version == "2022"
        assert profile.client_staging_dir == "/tmp/client"
        assert profile.server_staging_dir == "/tmp/server"
        assert profile.oracle_directory == "MY_DIR"
        assert profile.oracle_sqlldr_binary == "/usr/bin/sqlldr"
        assert profile.cloud_staging_uri == "s3://bucket/prefix/"
        assert profile.cloud_format == "csv"
        assert profile.topology == "shared_fs"
        assert profile.server_is_emulated is True

    def test_oc_env_interpolation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_PG_HOST", "pg.internal")
        monkeypatch.setenv("TEST_PG_PASS", "hunter2")
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              env_profile:
                dialect: postgres
                host: ${oc.env:TEST_PG_HOST}
                loader: client_stream
                password: ${oc.env:TEST_PG_PASS}
        """)
        profile = load_profile(yaml_path, "env_profile")
        assert profile.host == "pg.internal"
        assert profile.password == "hunter2"

    def test_secrets_interpolation(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              secret_profile:
                dialect: sqlserver
                host: db.internal
                loader: native_bulk
                password: ${secrets:my_scope,my_key}
        """)
        # Re-register resolver with a mock so it's used during OmegaConf resolution
        OmegaConf.clear_resolver("secrets")
        OmegaConf.register_new_resolver(
            "secrets",
            lambda *args: "resolved_secret",
            use_cache=True,
        )
        profile = load_profile(yaml_path, "secret_profile")
        assert profile.password == "resolved_secret"

    def test_secrets_json_path_interpolation(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              json_secret_profile:
                dialect: oracle
                host: oracle.internal
                loader: server_file
                password: ${secrets:scope,key,credentials,password}
        """)
        import json

        def _mock_read_secret(scope, key):
            return json.dumps({"credentials": {"password": "deep_secret"}})

        with patch("statschema.databricks_secrets.read_secret_string", _mock_read_secret):
            OmegaConf.clear_resolver("secrets")
            from statschema.connection_profile import _secrets_resolver
            OmegaConf.register_new_resolver("secrets", _secrets_resolver, use_cache=False)
            profile = load_profile(yaml_path, "json_secret_profile")
            assert profile.password == "deep_secret"

    def test_all_dialects_parse(self, tmp_path):
        dialects = [
            "postgres", "cockroachdb", "neon", "lakebase",
            "mysql", "sqlserver", "oracle", "db2", "lakehouse", "spark",
        ]
        for dialect in dialects:
            yaml_path = _write_yaml(tmp_path / f"{dialect}.yaml" if False else tmp_path, f"""
                profiles:
                  p:
                    dialect: {dialect}
                    host: localhost
                    loader: batch_insert
            """)
            profile = load_profile(yaml_path, "p")
            assert profile.dialect.value == dialect


# ---------------------------------------------------------------------------
# load_profile — error cases
# ---------------------------------------------------------------------------

class TestLoadProfileErrors:
    def test_missing_required_dialect(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              incomplete:
                host: localhost
                loader: client_stream
        """)
        with pytest.raises(MissingMandatoryValue):
            load_profile(yaml_path, "incomplete")

    def test_missing_required_host(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              incomplete:
                dialect: postgres
                loader: client_stream
        """)
        with pytest.raises(MissingMandatoryValue):
            load_profile(yaml_path, "incomplete")

    def test_missing_required_loader(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              incomplete:
                dialect: postgres
                host: localhost
        """)
        with pytest.raises(MissingMandatoryValue):
            load_profile(yaml_path, "incomplete")

    def test_unknown_profile_name(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              real:
                dialect: postgres
                host: localhost
                loader: client_stream
        """)
        with pytest.raises(KeyError, match="ghost"):
            load_profile(yaml_path, "ghost")

    def test_unknown_key_raises(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              bad:
                dialect: postgres
                host: localhost
                loader: client_stream
                this_key_does_not_exist: true
        """)
        with pytest.raises(Exception):
            # OmegaConf raises ConfigAttributeError or similar for struct-merge
            load_profile(yaml_path, "bad")

    def test_wrong_type_port(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              bad_type:
                dialect: postgres
                host: localhost
                loader: client_stream
                port: not_an_int
        """)
        with pytest.raises(Exception):
            load_profile(yaml_path, "bad_type")

    def test_wrong_type_tls(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              bad_tls:
                dialect: postgres
                host: localhost
                loader: client_stream
                tls: maybe
        """)
        with pytest.raises(Exception):
            load_profile(yaml_path, "bad_tls")

    def test_unknown_dialect_raises(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              unknown_dialect:
                dialect: teradata
                host: localhost
                loader: client_stream
        """)
        with pytest.raises(Exception):
            load_profile(yaml_path, "unknown_dialect")

    def test_no_profiles_key_raises(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            something_else:
              foo: bar
        """)
        with pytest.raises(KeyError, match="profiles"):
            load_profile(yaml_path, "foo")

    def test_file_not_found(self, tmp_path):
        with pytest.raises(Exception):
            load_profile(str(tmp_path / "nonexistent.yaml"), "p")


# ---------------------------------------------------------------------------
# DeploymentContext.from_profile()
# ---------------------------------------------------------------------------

class TestFromProfile:
    def _minimal_profile(self, **overrides):
        """Return a ConnectionProfile with required fields set."""
        from dataclasses import replace
        base = ConnectionProfile(
            dialect=Dialect.POSTGRES,
            host="localhost",
            loader="client_stream",
        )
        # apply overrides by re-assigning attributes (dataclass)
        for k, v in overrides.items():
            object.__setattr__(base, k, v)
        return base

    def test_basic_mapping(self):
        profile = self._minimal_profile()
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.loader == "client_stream"
        assert ctx.topology == "remote"
        assert ctx.server_version is None

    def test_loader_forwarded(self):
        profile = self._minimal_profile(loader="native_bulk")
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.loader == "native_bulk"

    def test_server_version_forwarded(self):
        profile = self._minimal_profile(version="2022")
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.server_version == "2022"

    def test_staging_dirs_forwarded(self):
        profile = self._minimal_profile(
            client_staging_dir="/tmp/client",
            server_staging_dir="/tmp/server",
        )
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.client_staging_dir == "/tmp/client"
        assert ctx.server_staging_dir == "/tmp/server"

    def test_topology_inferred_shared_fs(self):
        profile = self._minimal_profile(client_staging_dir="/tmp/staging")
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.topology == "shared_fs"

    def test_topology_inferred_cloud_staged(self):
        profile = self._minimal_profile(cloud_staging_uri="s3://bucket/prefix/")
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.topology == "cloud_staged"

    def test_topology_explicit_overrides_inference(self):
        profile = self._minimal_profile(
            topology="remote",
            client_staging_dir="/tmp/staging",  # would infer shared_fs without explicit
        )
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.topology == "remote"

    def test_oracle_fields_forwarded(self):
        profile = self._minimal_profile(
            dialect=Dialect.ORACLE,
            oracle_directory="MY_DIR",
            oracle_sqlldr_binary="/usr/bin/sqlldr",
        )
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.oracle_directory == "MY_DIR"
        assert ctx.oracle_sqlldr_binary == "/usr/bin/sqlldr"

    def test_server_is_emulated_forwarded(self):
        profile = self._minimal_profile(server_is_emulated=True)
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.server_is_emulated is True

    def test_is_databricks_inferred_from_databricks_dialect(self):
        profile = self._minimal_profile(dialect=Dialect.LAKEHOUSE)
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.is_databricks is True

    def test_is_databricks_false_for_non_databricks_dialect(self):
        for dialect in (Dialect.POSTGRES, Dialect.MYSQL, Dialect.SQLSERVER,
                        Dialect.ORACLE, Dialect.DB2):
            profile = self._minimal_profile(dialect=dialect)
            ctx = DeploymentContext.from_profile(profile)
            assert ctx.is_databricks is False, f"expected False for {dialect}"

    def test_cloud_format_forwarded(self):
        profile = self._minimal_profile(
            cloud_staging_uri="s3://b/",
            cloud_format="csv",
        )
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.cloud_format == "csv"

    def test_credentials_forwarded(self):
        profile = self._minimal_profile(
            host="db.example.com",
            port=5432,
            database="mydb",
            username="alice",
            password="s3cr3t",
        )
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.host == "db.example.com"
        assert ctx.port == 5432
        assert ctx.database == "mydb"
        assert ctx.username == "alice"
        assert ctx.password == "s3cr3t"
        assert ctx.endpoint is None

    def test_endpoint_forwarded(self, tmp_path):
        yaml_path = _write_yaml(tmp_path, """
            profiles:
              lb:
                dialect: lakebase
                host: adb-xxx.azuredatabricks.net
                loader: client_stream
                database: databricks_postgres
                username: alice
                password: token123
                endpoint: projects/p/branches/b/endpoints/e
        """)
        profile = load_profile(yaml_path, "lb")
        ctx = DeploymentContext.from_profile(profile)
        assert ctx.endpoint == "projects/p/branches/b/endpoints/e"
