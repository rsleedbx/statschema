#!/usr/bin/env bash
# Quick smoke-test for _common_lima.sh.
# Usage:  bash benchmarks/_test_common_lima.sh 2>&1 | tee /tmp/common-lima-test.log
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
source "${REPO_ROOT}/benchmarks/_common_lima.sh"

PASS=0; FAIL=0; SKIP=0
result() { local status=$1 name=$2 msg=${3:-}
    case $status in
        PASS) PASS=$((PASS+1)); echo "  ✓ $name" ;;
        FAIL) FAIL=$((FAIL+1)); echo "  ✗ $name${msg:+: $msg}" ;;
        SKIP) SKIP=$((SKIP+1)); echo "  ~ $name (skipped: $msg)" ;;
    esac
}

# ── 0. Lima VM ────────────────────────────────────────────────────────────────
echo ""
echo "=== 0. ensure_lima (create/start statschema VM) ==="
if ensure_lima; then
    result PASS "ensure_lima"
else
    result FAIL "ensure_lima"
    echo "Cannot continue without Lima VM.  Aborting."
    exit 1
fi
limactl list 2>/dev/null | grep statschema

# ── 1. Postgres ───────────────────────────────────────────────────────────────
echo ""
echo "=== 1. PostgreSQL ==="
if start_pg; then
    result PASS "start_pg"
    if sqlcli pg <<< '\q' 2>/dev/null; then
        result PASS "sqlcli pg (app user)"
    else
        result FAIL "sqlcli pg (app user)"
    fi
    if sqlclidba pg <<< '\q' 2>/dev/null; then
        result PASS "sqlclidba pg (postgres)"
    else
        result FAIL "sqlclidba pg (postgres)"
    fi
else
    result FAIL "start_pg"
fi

# ── 2. CockroachDB ────────────────────────────────────────────────────────────
echo ""
echo "=== 2. CockroachDB ==="
if start_crdb; then
    result PASS "start_crdb"
    if sqlclidba crdb <<< '\q' 2>/dev/null; then
        result PASS "sqlclidba crdb (root)"
    else
        result FAIL "sqlclidba crdb (root)"
    fi
else
    result FAIL "start_crdb"
fi

# ── 3. MySQL ──────────────────────────────────────────────────────────────────
echo ""
echo "=== 3. MySQL ==="
if start_mysql; then
    result PASS "start_mysql"
    if sqlclidba mysql <<< 'quit' 2>/dev/null; then
        result PASS "sqlclidba mysql (root)"
    else
        result FAIL "sqlclidba mysql (root)"
    fi
else
    result FAIL "start_mysql"
fi

# ── 4. SQL Server ─────────────────────────────────────────────────────────────
echo ""
echo "=== 4. SQL Server (x86_64 via Rosetta 2 in statschema VM) ==="
if start_sqlserver; then
    result PASS "start_sqlserver"
    if sqlclidba sqlserver <<< 'SELECT 1; GO' 2>/dev/null | grep -q "^1"; then
        result PASS "sqlclidba sqlserver (sa)"
    else
        result FAIL "sqlclidba sqlserver (sa)"
    fi
else
    result FAIL "start_sqlserver"
fi

# ── 5. Oracle ─────────────────────────────────────────────────────────────────
echo ""
echo "=== 5. Oracle (x86_64 via Rosetta 2 in statschema VM) ==="
if start_oracle; then
    result PASS "start_oracle"
    if sqlclidba oracle <<< 'quit' 2>/dev/null; then
        result PASS "sqlclidba oracle (system)"
    else
        result FAIL "sqlclidba oracle (system)"
    fi
else
    result FAIL "start_oracle"
fi

# ── 6. DB2 via Rosetta 2 (the key test) ──────────────────────────────────────
echo ""
echo "=== 6. DB2 via Rosetta 2 (statschema VM — testing if x86-64-v2 is satisfied) ==="
if start_db2; then
    result PASS "start_db2 (Rosetta 2)"
    if sqlclidba db2 <<< 'quit' 2>/dev/null; then
        result PASS "sqlclidba db2 (db2inst1)"
    else
        result FAIL "sqlclidba db2 (db2inst1)"
    fi
else
    result FAIL "start_db2 (Rosetta 2) — use db2lima (QEMU) as fallback"
fi

# ── 7. DB2 via Lima QEMU (proven fallback) ────────────────────────────────────
echo ""
echo "=== 7. DB2 via Lima QEMU (db2lima — proven fallback) ==="
if start_db2_lima; then
    result PASS "start_db2_lima"
    if sqlclidba db2lima <<< 'quit' 2>/dev/null; then
        result PASS "sqlclidba db2lima (db2inst1)"
    else
        result FAIL "sqlclidba db2lima (db2inst1)"
    fi
else
    result FAIL "start_db2_lima"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  PASSED: $PASS   FAILED: $FAIL   SKIPPED: $SKIP"
echo "========================================"
[[ $FAIL -eq 0 ]]
