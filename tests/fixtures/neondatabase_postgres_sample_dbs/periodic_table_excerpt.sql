-- Excerpt from neondatabase/postgres-sample-dbs periodic_table.sql (https://github.com/neondatabase/postgres-sample-dbs)
-- Single table for pytest: parse + generate data (PostgreSQL pg_dump-style DDL).

CREATE TABLE public.periodic_table (
 "AtomicNumber" integer NOT NULL,
 "Element" text,
 "Symbol" text,
 "AtomicMass" numeric,
 "Period" integer,
 "Group" integer,
 "Phase" text,
 "Metal" boolean,
 "Nonmetal" boolean,
 "Metalloid" boolean,
 "Type" text,
 "Year" integer,
 PRIMARY KEY ("AtomicNumber")
);
