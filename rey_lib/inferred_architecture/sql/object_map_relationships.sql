-- The object map's claim relationships (backlog 860): one row per claim ->
-- linked claim, as code.object_map_relationship_vw states it.
SELECT	canonical_object_id,
	object_key,
	linked_canonical_object_id,
	linked_object_key,
	is_direct,
	entry_count,
	entry_code_symbol_ids
FROM	code.object_map_relationship_vw
ORDER BY canonical_object_id, linked_canonical_object_id
