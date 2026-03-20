# ── Statschema dev shortcuts ───────────────────────────────────────────────
#
# Prerequisites
#   .venv_test  – Python 3.11 venv with requirements-test.txt (local Spark)
#   .venv       – Python 3.14 venv with requirements-dev.txt  (Databricks Connect)
#
# Quick start
#   make venv-test       # create / refresh .venv_test
#   make test            # run full suite with local PySpark
#   make test-fast       # run everything except Spark data-generation tests
#   make test-spark      # run only the Spark data-generation tests

VENV_TEST  := .venv_test
PYTHON_TEST := $(VENV_TEST)/bin/python
PYTEST_TEST := $(PYTHON_TEST) -m pytest

VENV_DEV   := .venv
PYTHON_DEV := $(VENV_DEV)/bin/python

# ---------------------------------------------------------------------------

.PHONY: venv-test test test-fast test-spark test-live-all test-live-roundtrip test-live-synth test-live-sqlserver test-live-mysql test-live-mariadb test-live-pg test-live-neon test-live-oracle test-live-mautic test-live-gitea test-live-adventureworks test-live-chinook test-live-oracle-hr test-live-stats-transpiler test-live-stats-databricks test-live-cockroachdb test-live-db2 lint clean

## Create / refresh the test venv (local PySpark, no databricks-connect)
venv-test:
	python3.11 -m venv $(VENV_TEST)
	$(VENV_TEST)/bin/pip install --upgrade pip
	$(VENV_TEST)/bin/pip install -r requirements-test.txt

## Run the full test suite (local PySpark, conftest.py sets JAVA_HOME)
test:
	$(PYTEST_TEST) tests/ -v

## Run everything except Spark data-generation and live-DB tests
test-fast:
	$(PYTEST_TEST) tests/ -v -k "not generate_data and not live"

## Run only the Spark data-generation tests
test-spark:
	$(PYTEST_TEST) tests/ -v -k "generate_data"

## Run live SQL Server tests (requires: limactl start sqlserver22)
## Credentials are loaded automatically from .env (copy .env.example → .env and fill in SQLSERVER_PASS).
## Override via env var: make test-live-sqlserver SQLSERVER_PASS=<password>
test-live-sqlserver:
	SQLSERVER_PASS=$(SQLSERVER_PASS) SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	$(PYTEST_TEST) tests/test_live_sqlserver.py -v

## Run ALL live DB round-trip tests (MySQL 5.7+8, PG 14+16, SQL Server 22 must be up)
## Credentials are loaded from .env automatically.  Override with env vars if needed.
## NeonDB + CockroachDB: skip automatically if containers are not running.
test-live-all:
	MYSQL57_PORT=$(or $(MYSQL57_PORT),3357) \
	MYSQL8_PORT=$(or $(MYSQL8_PORT),3384) \
	PG14_PORT=$(or $(PG14_PORT),5414) \
	PG16_PORT=$(or $(PG16_PORT),5416) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	ORACLE_HOST=$(or $(ORACLE_HOST),127.0.0.1) \
	ORACLE_PORT=$(or $(ORACLE_PORT),1521) \
	ORACLE_PASS=$(or $(ORACLE_PASS),oracle) \
	ORACLE_SERVICE=$(or $(ORACLE_SERVICE),XE) \
	CRDB_SINGLE_PORT=$(or $(CRDB_SINGLE_PORT),26257) \
	CRDB_MULTI_PORT=$(or $(CRDB_MULTI_PORT),26267) \
	MARIADB_LTS_PORT=$(or $(MARIADB_LTS_PORT),3310) \
	MARIADB_NEW_PORT=$(or $(MARIADB_NEW_PORT),3311) \
	DB2_HOST=$(or $(DB2_HOST),127.0.0.1) \
	DB2_PORT=$(or $(DB2_PORT),50000) \
	DB2_PASS=$(or $(DB2_PASS),testpass) \
	$(PYTEST_TEST) \
	  tests/test_live_roundtrip.py \
	  tests/test_live_mysql.py \
	  tests/test_live_mariadb.py \
	  tests/test_live_pg.py \
	  tests/test_live_sqlserver.py \
	  tests/test_live_oracle.py \
	  tests/test_live_mautic.py \
	  tests/test_live_gitea.py \
	  tests/test_live_adventureworks.py \
	  tests/test_live_chinook.py \
	  tests/test_live_oracle_hr.py \
	  tests/test_live_stats_transpiler.py \
	  tests/test_live_neon.py \
	  tests/test_live_cockroachdb.py \
	  tests/test_live_db2.py \
	  -v

## Run comprehensive live round-trip tests (all Phase 1+2 cases × all live DBs + cross-dialect pipeline)
## Requires: MySQL 5.7+8, PG 14+16, SQL Server 22.  Credentials from .env.
test-live-roundtrip:
	MYSQL57_PORT=$(or $(MYSQL57_PORT),3357) \
	MYSQL8_PORT=$(or $(MYSQL8_PORT),3384) \
	PG14_PORT=$(or $(PG14_PORT),5414) \
	PG16_PORT=$(or $(PG16_PORT),5416) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	$(PYTEST_TEST) tests/test_live_roundtrip.py -v

## Run live PostgreSQL tests (PG 14 and 16 containers must be running; see docs/local-databases.md)
## Usage: make test-live-pg
## Override ports: make test-live-pg PG14_PORT=5414 PG16_PORT=5416
test-live-pg:
	PG14_PORT=$(or $(PG14_PORT),5414) PG16_PORT=$(or $(PG16_PORT),5416) \
	$(PYTEST_TEST) tests/test_live_pg.py -v

## Run live MySQL tests (both 5.7 and 8.x containers must be running; see docs/local-databases.md)
## Usage: make test-live-mysql
## Override ports: make test-live-mysql MYSQL57_PORT=3357 MYSQL8_PORT=3384
test-live-mysql:
	MYSQL57_PORT=$(or $(MYSQL57_PORT),3357) MYSQL8_PORT=$(or $(MYSQL8_PORT),3384) \
	$(PYTEST_TEST) tests/test_live_mysql.py -v

## Usage: make test-live-mariadb
## Override ports: make test-live-mariadb MARIADB_LTS_PORT=3310 MARIADB_NEW_PORT=3311
test-live-mariadb:
	MARIADB_LTS_PORT=$(or $(MARIADB_LTS_PORT),3310) MARIADB_NEW_PORT=$(or $(MARIADB_NEW_PORT),3311) \
	$(PYTEST_TEST) tests/test_live_mariadb.py -v

## Usage: make test-live-db2
## Requires: limactl start --name=db2 config/lima/db2.yaml (first boot ~5-10 min)
## Override: make test-live-db2 DB2_PORT=50000 DB2_PASS=testpass
test-live-db2:
	DB2_HOST=$(or $(DB2_HOST),127.0.0.1) \
	DB2_PORT=$(or $(DB2_PORT),50000) \
	DB2_PASS=$(or $(DB2_PASS),testpass) \
	$(PYTEST_TEST) tests/test_live_db2.py -v

## Run live Oracle XE tests (requires: limactl start --name=oracle config/lima/oracle.yaml)
## Credentials are loaded from .env automatically.
test-live-oracle:
	ORACLE_HOST=$(or $(ORACLE_HOST),127.0.0.1) \
	ORACLE_PORT=$(or $(ORACLE_PORT),1521) \
	ORACLE_PASS=$(or $(ORACLE_PASS),oracle) \
	ORACLE_SERVICE=$(or $(ORACLE_SERVICE),XE) \
	$(PYTEST_TEST) tests/test_live_oracle.py -v

## Run live Gitea application tests (Gitea + pg16 Podman containers)
test-live-gitea:
	GITEA_PG_HOST=$(or $(GITEA_PG_HOST),127.0.0.1) \
	GITEA_PG_PORT=$(or $(GITEA_PG_PORT),5416) \
	GITEA_PG_USER=$(or $(GITEA_PG_USER),gitea) \
	GITEA_PG_PASS=$(or $(GITEA_PG_PASS),gitea123) \
	GITEA_PG_DB=$(or $(GITEA_PG_DB),gitea) \
	$(PYTEST_TEST) tests/test_live_gitea.py -v

## Run live AdventureWorks tests (AWLT 2022 + AW2022 full on SQL Server 22 Lima VM)
test-live-adventureworks:
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	$(PYTEST_TEST) tests/test_live_adventureworks.py -v

## Run live Chinook application tests (Chinook on SQL Server 22 Lima VM)
test-live-chinook:
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	$(PYTEST_TEST) tests/test_live_chinook.py -v

## Run live Oracle HR/CO sample schema tests (Oracle XE Lima VM)
test-live-oracle-hr:
	ORACLE_HOST=$(or $(ORACLE_HOST),127.0.0.1) \
	ORACLE_PORT=$(or $(ORACLE_PORT),1521) \
	ORACLE_PASS=$(or $(ORACLE_PASS),oracle) \
	$(PYTEST_TEST) tests/test_live_oracle_hr.py -v

## Run live Neon tests (Neon Local proxy; see docs/local-databases.md — neondatabase/neon_local, not neondatabase/neon)
## Requires: podman run neon_local with NEON_API_KEY / NEON_PROJECT_ID; port mapped to NEON_LOCAL_PORT (default 55433)
test-live-neon:
	$(PYTEST_TEST) tests/test_live_neon.py -v

## Run live CockroachDB tests (single-node and/or multi-region containers; see docs/local-databases.md)
## Each topology skips automatically if its port is unreachable.
test-live-cockroachdb:
	CRDB_SINGLE_PORT=$(or $(CRDB_SINGLE_PORT),26257) \
	CRDB_MULTI_PORT=$(or $(CRDB_MULTI_PORT),26267) \
	$(PYTEST_TEST) tests/test_live_cockroachdb.py -v

## Run live Mautic application tests (Mautic 5 + mysql8 containers; see docs/local-databases.md)
## Usage: make test-live-mautic
## Override: make test-live-mautic MAUTIC_MYSQL_PORT=3384
test-live-mautic:
	MAUTIC_MYSQL_HOST=$(or $(MAUTIC_MYSQL_HOST),127.0.0.1) \
	MAUTIC_MYSQL_PORT=$(or $(MAUTIC_MYSQL_PORT),3384) \
	MAUTIC_MYSQL_USER=$(or $(MAUTIC_MYSQL_USER),mautic) \
	MAUTIC_MYSQL_PASS=$(or $(MAUTIC_MYSQL_PASS),mauticpass) \
	MAUTIC_MYSQL_DB=$(or $(MAUTIC_MYSQL_DB),mautic) \
	$(PYTEST_TEST) tests/test_live_mautic.py -v

## Run live stats transpiler tests (proves stats injection works on PG18, Oracle, SQL Server)
## Prerequisites: pg18 Podman container, Oracle XE Lima VM, SQL Server 22 Lima VM
## pg18:  podman run -d --name pg18 -e POSTGRES_PASSWORD=postgres -p 5418:5432 postgres:18
test-live-stats-transpiler:
	ORACLE_HOST=$(or $(ORACLE_HOST),127.0.0.1) \
	ORACLE_PORT=$(or $(ORACLE_PORT),1521) \
	ORACLE_PASS=$(or $(ORACLE_PASS),oracle) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	$(PYTEST_TEST) tests/test_live_stats_transpiler.py -v

## Run Databricks stats injection tests using local PySpark + delta-spark (no live Databricks cluster needed)
## The tests use table_format="parquet" locally (delta-spark 4.x ANALYZE TABLE workaround).
## Prerequisites: delta-spark is included in requirements-test.txt (make venv-test installs it).
test-live-stats-databricks:
	$(PYTEST_TEST) tests/test_live_stats_transpiler.py::TestDatabricksStatsInjection -v

## Run live synthetic data pipeline tests (MySQL 8, PG 16, SQL Server 22; Spark + dbldatagen required)
## Credentials are loaded from .env automatically.
test-live-synth:
	MYSQL8_PORT=$(or $(MYSQL8_PORT),3384) \
	PG16_PORT=$(or $(PG16_PORT),5416) \
	SQLSERVER_PASS=$(SQLSERVER_PASS) \
	SQLSERVER_PORT=$(or $(SQLSERVER_PORT),14330) \
	$(PYTEST_TEST) tests/test_live_synth.py -v

## Run a single test file
# Usage: make test-file FILE=tests/test_ddl_roundtrip.py
test-file:
	$(PYTEST_TEST) $(FILE) -v

lint:
	$(VENV_TEST)/bin/ruff check src/ tests/ || true

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
