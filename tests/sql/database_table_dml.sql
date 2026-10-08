-- Tables the DatabaseTable DML tests write to (backlog database.table_dml_surface).
-- The test splits this file on statement terminators, so comments carry none.

CREATE SEQUENCE main.event_id_seq START 1;

CREATE TABLE main.event (
	event_id BIGINT PRIMARY KEY DEFAULT nextval('main.event_id_seq'),
	kind VARCHAR,
	note VARCHAR,
	payload JSON
);

CREATE TABLE main.no_key (
	a INTEGER
);

CREATE TABLE main.two_keys (
	a INTEGER,
	b INTEGER,
	PRIMARY KEY (a, b)
);
