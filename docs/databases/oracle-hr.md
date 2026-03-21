# Oracle HR / CO sample schemas — application-level testing

← [All databases](../local-databases.md)

**Requires:** [Oracle XE](oracle.md) (Lima VM, port 1521)

Oracle's canonical sample schemas from [oracle-samples/db-sample-schemas](https://github.com/oracle-samples/db-sample-schemas):

- **HR (Human Resources)** — 7 tables: REGIONS → COUNTRIES → LOCATIONS → DEPARTMENTS → JOBS → EMPLOYEES → JOB_HISTORY
- **CO (Customer Orders)** — 7 tables: CUSTOMERS → STORES → PRODUCTS → ORDERS → SHIPMENTS → ORDER_ITEMS → INVENTORY

Types: `NUMBER(p,s)`, `VARCHAR2`, `CHAR`, `DATE`, `TIMESTAMP`, `INTERVAL`, `CLOB`, FK relationships.

## One-time setup

```bash
# Download the create scripts
curl -sL "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/human_resources/hr_create.sql" \
  -o /tmp/hr_create.sql
curl -sL "https://raw.githubusercontent.com/oracle-samples/db-sample-schemas/main/customer_orders/co_create.sql" \
  -o /tmp/co_create.sql

# Create users and schemas via Python (handles SQL*Plus directives)
.venv_test/bin/python - << 'EOF'
import oracledb, re

def run_sql_script(conn, script_path, skip_views=True):
    with open(script_path) as f:
        content = f.read()
    content = re.sub(r'^(SET|Prompt|SPOOL|HOST|COLUMN|TTITLE|PAUSE|DEFINE|ACCEPT|REMARK)\b.*$',
                     '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'^rem\b.*$', '', content, flags=re.IGNORECASE|re.MULTILINE)
    content = re.sub(r'--.*$', '', content, flags=re.MULTILINE)
    cur = conn.cursor()
    for stmt in [s.strip() for s in content.split(';') if len(s.strip()) > 5]:
        if skip_views and re.match(r'CREATE\s+OR\s+REPLACE\s+VIEW', stmt, re.I): continue
        if re.match(r'COMMENT\s+ON', stmt, re.I): continue
        try:
            cur.execute(stmt)
            conn.commit()
        except Exception as e:
            if 'ORA-00955' not in str(e):  # ignore "already exists"
                print(f"SKIP: {str(e)[:60]}")

sys_conn = oracledb.connect(user='system', password='oracle', dsn='127.0.0.1:1521/XE')
cur = sys_conn.cursor()
for user in ['hr', 'oe', 'co']:
    try: cur.execute(f'DROP USER {user} CASCADE')
    except: pass
for stmt in [
    "CREATE USER hr IDENTIFIED BY hr",
    "GRANT CONNECT, RESOURCE, CREATE VIEW TO hr",
    "ALTER USER hr QUOTA UNLIMITED ON USERS",
    "CREATE USER co IDENTIFIED BY co",
    "GRANT CONNECT, RESOURCE, CREATE VIEW, CREATE SEQUENCE TO co",
    "ALTER USER co QUOTA UNLIMITED ON USERS",
]:
    cur.execute(stmt)
sys_conn.commit()
sys_conn.close()

run_sql_script(oracledb.connect(user='hr', password='hr', dsn='127.0.0.1:1521/XE'), '/tmp/hr_create.sql')
run_sql_script(oracledb.connect(user='co', password='co', dsn='127.0.0.1:1521/XE'), '/tmp/co_create.sql')
print("HR and CO schemas created")
EOF
```

## Run the tests

```bash
make test-live-oracle-hr
```

Expected output: **20 passed**.
