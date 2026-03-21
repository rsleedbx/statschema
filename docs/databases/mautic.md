# Mautic — application-level testing (MySQL)

← [All databases](../local-databases.md)

**Requires:** [MySQL](mysql.md) (`mysql8` container running on port 3384)

[Mautic](https://www.mautic.org/) (~108 MySQL tables).  Types include: `BIGINT UNSIGNED`, `TINYINT(1)`, `LONGTEXT`, `DATETIME`, `VARCHAR`, `DOUBLE`, `INT`.  Schema includes virtual/generated columns.

## One-time setup

```bash
# 1 – Create the mautic database on the existing mysql8 container
podman exec mysql8 mysql -uroot -ptestpass -e "
  CREATE DATABASE IF NOT EXISTS mautic CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER IF NOT EXISTS 'mautic'@'%' IDENTIFIED BY 'mauticpass';
  GRANT ALL PRIVILEGES ON mautic.* TO 'mautic'@'%';
  FLUSH PRIVILEGES;
"

# 2 – Create a network so Mautic can reach mysql8 by container name
podman network create mautic_net 2>/dev/null || true
podman network connect mautic_net mysql8 2>/dev/null || true

# 3 – Pull and start Mautic 5
podman pull docker.io/mautic/mautic:5-apache
podman run -d \
  --name mautic \
  --network mautic_net \
  -p 8080:80 \
  -e MAUTIC_DB_HOST=mysql8 \
  -e MAUTIC_DB_PORT=3306 \
  -e MAUTIC_DB_NAME=mautic \
  -e MAUTIC_DB_USER=mautic \
  -e MAUTIC_DB_PASSWORD=mauticpass \
  docker.io/mautic/mautic:5-apache

# 4 – Install Mautic via CLI (skips the web wizard, ~4 s)
podman exec mautic php /var/www/html/bin/console mautic:install \
  --force \
  --db_driver=pdo_mysql \
  --db_host=mysql8 \
  --db_port=3306 \
  --db_name=mautic \
  --db_user=mautic \
  --db_password=mauticpass \
  --admin_email=admin@example.com \
  "--admin_password=Mautic1234\!" \
  --admin_firstname=Admin \
  --admin_lastname=User \
  "http://127.0.0.1:8080"

# 5 – Fix Doctrine migration metadata (needed after CLI install)
podman exec mautic bash -c "
  cd /var/www/html
  php bin/console doctrine:migrations:sync-metadata-storage
  php bin/console doctrine:migrations:version --add --all --no-interaction
  php bin/console cache:warmup
"

# 6 – Insert 10 test contacts
podman exec mysql8 mysql -umautic -pmauticpass mautic -e "
INSERT INTO leads (is_published, date_added, date_modified, date_identified,
                   firstname, lastname, email, company, points) VALUES
  (1,NOW(),NOW(),NOW(),'Alice','Anderson','alice@example.com','Acme Corp',10),
  (1,NOW(),NOW(),NOW(),'Bob','Baker','bob@example.com','Beta Inc',20),
  (1,NOW(),NOW(),NOW(),'Carol','Clark','carol@example.com','Contoso Ltd',15),
  (1,NOW(),NOW(),NOW(),'David','Davis','david@example.com','Dunder Mifflin',5),
  (1,NOW(),NOW(),NOW(),'Eva','Evans','eva@example.com','Extensive Ent',30),
  (1,NOW(),NOW(),NOW(),'Frank','Fisher','frank@example.com','Fabco Inc',25),
  (1,NOW(),NOW(),NOW(),'Grace','Garcia','grace@example.com','Gamma Corp',8),
  (1,NOW(),NOW(),NOW(),'Henry','Harris','henry@example.com','Heroic Ltd',12),
  (1,NOW(),NOW(),NOW(),'Irene','Irving','irene@example.com','Ideal Systems',18),
  (1,NOW(),NOW(),NOW(),'James','Johnson','james@example.com','Jubilee Corp',22);
"
```

## Run the tests

```bash
make test-live-mautic
# or directly:
.venv_test/bin/python -m pytest tests/test_live_mautic.py -v
```

Expected output: **19 passed**.

## Known Mautic-specific behaviours

| Mautic column pattern | Canonical type | Notes |
|-----------------------|----------------|-------|
| `BIGINT UNSIGNED AUTO_INCREMENT` | `long` | PK on all entity tables |
| `TINYINT(1)` | `integer` | boolean flags (is_published, is_read, …) |
| `LONGTEXT` | `string` | JSON blobs (COMMENT '(DC2Type:array)') |
| `GENERATED ALWAYS AS … VIRTUAL` | column dropped | |
| `INT UNSIGNED` | `long` | Foreign key references (owner_id, stage_id, …) |

## Multi-instance / shard candidates

These tables grow large in production and are typically sharded:

| Table | Columns | Shard reason |
|-------|---------|--------------|
| `email_stats` | 21 | One row per sent email, very high volume |
| `campaign_lead_event_log` | 14 | One row per campaign action |
| `page_hits` | 26 | One row per page view |
| `audit_log` | 10 | Append-only audit trail |

Example canonical YAML (4-shard email_stats):

```yaml
version: "1.0"
tables:
  - name: email_stats
    instance_count: 4
    instance_suffix_format: "_{:04d}"
    columns:
      - {name: id,            type: long,    auto_increment: true, primary_key: true, not_null: true}
      - {name: email_id,      type: long}
      - {name: lead_id,       type: long}
      - {name: date_sent,     type: datetime, not_null: true}
      # … other columns …
```

After `load_canonical(yaml_str, expand=True)` this produces four tables named `email_stats_0001` through `email_stats_0004`.

## Mautic API note

The Mautic REST API requires OAuth2 by default.  HTTP Basic Auth can be enabled by adding to `config/local.php`:

```php
'api_enabled'           => true,
'api_enable_basic_auth' => true,
```

Tests insert rows via SQL (step 6).
