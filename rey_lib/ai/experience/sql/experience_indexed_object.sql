-- #1344: one database object as the database index holds it -- every indexed object of
-- that schema and name, in every source, with each of its members' names. The verifier
-- decides from these rows: the object resolves only when exactly one is indexed and it
-- is in the named source; a member is present or absent only on that one object.
SELECT	o.code_db_object_id,
	o.source_key,
	m.member_name
FROM	code.db_object_vw AS o
LEFT JOIN code.db_member_vw AS m ON m.code_db_object_id = o.code_db_object_id
WHERE	o.schema_name = :schema
AND	o.object_name = :name
