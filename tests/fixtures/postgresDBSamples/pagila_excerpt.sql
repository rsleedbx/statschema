-- Excerpt from morenoh149/postgresDBSamples pagila-schema.sql (https://github.com/morenoh149/postgresDBSamples)
-- Tables only for pytest: parse + generate data (PostgreSQL DDL).

CREATE TABLE actor (
 actor_id integer NOT NULL,
 first_name character varying(45) NOT NULL,
 last_name character varying(45) NOT NULL,
 last_update timestamp without time zone DEFAULT now() NOT NULL,
 PRIMARY KEY (actor_id)
);

CREATE TABLE category (
 category_id integer NOT NULL,
 name character varying(25) NOT NULL,
 last_update timestamp without time zone DEFAULT now() NOT NULL,
 PRIMARY KEY (category_id)
);

CREATE TABLE country (
 country_id integer NOT NULL,
 country character varying(50) NOT NULL,
 last_update timestamp without time zone DEFAULT now() NOT NULL,
 PRIMARY KEY (country_id)
);

CREATE TABLE city (
 city_id integer NOT NULL,
 city character varying(50) NOT NULL,
 country_id smallint NOT NULL,
 last_update timestamp without time zone DEFAULT now() NOT NULL,
 PRIMARY KEY (city_id)
);
