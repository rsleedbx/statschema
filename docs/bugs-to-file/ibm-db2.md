# Bugs to file — IBM DB2 / ibm_db_dbi

---

## Bug 1 — `ibm_db_dbi`: `cursor.execute('RUNSTATS …')` silently swallows SQL0104N

**Product**: `ibm_db_dbi` Python driver (IBM DB2 CE 11.5, driver version bundled with `ibm_db`)
**Where to file**: https://github.com/ibmdb/python-ibmdb/issues
**Severity**: High — causes silent data loss (stats never collected, no exception raised)

### Description

Calling `cursor.execute("RUNSTATS ON TABLE schema.T WITH DISTRIBUTION ...")` via
`ibm_db_dbi` does not raise a Python exception when the command fails with
`SQL0104N` (unexpected token). The error is silently discarded. The caller has
no way to know the statement did not execute.

The correct workaround is to wrap every `RUNSTATS` call in
`CALL SYSPROC.ADMIN_CMD(...)`, which `ibm_db_dbi` does execute correctly.
This requirement is not documented anywhere in the driver README, the PyPI
package page, or the inline docstrings.

### Steps to reproduce

```python
import ibm_db_dbi

conn = ibm_db_dbi.connect(
    "DATABASE=testdb;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;"
    "UID=db2inst1;PWD=testpass;", "", ""
)
cur = conn.cursor()
# This silently does nothing — SQL0104N is swallowed
cur.execute("RUNSTATS ON TABLE DB2INST1.CUSTOMER WITH DISTRIBUTION AND DETAILED INDEXES ALL")
conn.commit()
# Verify: SYSCAT.COLUMNS.COLCARD is still -1 (not updated)
```

### Expected behaviour

Either the statement executes successfully, or a `ProgrammingError` /
`OperationalError` is raised with the SQL0104N message.

### Actual behaviour

No exception is raised. The command silently does nothing. Subsequent reads
from `SYSCAT.COLUMNS` and `SYSCAT.COLDIST` show no updated statistics.

### Workaround

```python
cur.execute("CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE DB2INST1.CUSTOMER "
            "WITH DISTRIBUTION AND DETAILED INDEXES ALL')")
conn.commit()
```

### Impact in our codebase

`benchmarks/identity_test.py` `_analyze_db2()` — every RUNSTATS call was
silently skipped for all previous benchmark runs.  Fixed in commit (see
`git log --grep="ADMIN_CMD"`).

---

## Bug 2 — DB2 documentation: `TABLESAMPLE SYSTEM(n)` placement in `RUNSTATS` not specified

**Product**: IBM DB2 LUW 11.5 — RUNSTATS utility documentation
**Where to file**: IBM support / IBM Ideas portal (https://ideas.ibm.com)
**Severity**: Low — affects developer ergonomics only

### Description

The IBM documentation for `RUNSTATS` mentions `TABLESAMPLE SYSTEM(n)` but does
not clearly specify where in the statement it must appear relative to other
clauses.  Placing it before `AND DETAILED INDEXES ALL` produces `SQL0104N`.
The correct placement is after all other clauses:

```sql
-- Correct
RUNSTATS ON TABLE schema.T WITH DISTRIBUTION AND DETAILED INDEXES ALL TABLESAMPLE SYSTEM(25)

-- Wrong — SQL0104N
RUNSTATS ON TABLE schema.T TABLESAMPLE SYSTEM(25) WITH DISTRIBUTION AND DETAILED INDEXES ALL
```

### Suggested doc fix

Add a concrete complete example showing `TABLESAMPLE SYSTEM(n)` as the final
clause, with a note that it must follow `AND DETAILED INDEXES ALL` when both
are specified.

---

## Bug 3 — `ibm_db_dbi`: no documented list of statements unsupported via `cursor.execute()`

**Product**: `ibm_db_dbi` Python driver
**Where to file**: https://github.com/ibmdb/python-ibmdb/issues
**Severity**: Medium — produces silent failures for other DB2 utility commands

### Description

Several DB2 utility commands (RUNSTATS, REORG, REDISTRIBUTE, BACKUP) cannot be
executed via `cursor.execute()` and must go through `CALL SYSPROC.ADMIN_CMD()`.
The driver provides no error, warning, or documentation listing which statement
classes are unsupported.

### Suggested fix

The `ibm_db_dbi` README and/or docstring for `cursor.execute()` should include
a note such as:

> DB2 utility commands (RUNSTATS, REORG, BACKUP, REDISTRIBUTE) cannot be
> executed directly. Use `CALL SYSPROC.ADMIN_CMD('…')` instead.

Alternatively, the driver should raise `ProgrammingError` when it receives
`SQL0104N` from a `RUNSTATS` / utility statement rather than discarding it.
