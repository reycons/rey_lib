-- The object map's claims (backlog 860): one row per active claim, as
-- code.object_map_claim_vw states it. unresolved_edges is a sum of counts,
-- carried as an integer so it travels as JSON.
SELECT	canonical_object_id,
	object_key,
	responsibility,
	implementation_reference,
	owner_repository_key,
	owner_relative_path,
	owner_qualified_name,
	owner_code_symbol_id,
	owner_code_file_id,
	owner_subject_id,
	owner_kind,
	public_seam,
	authority,
	must_not_bypass,
	evaluation_outcome,
	evaluation_reason,
	consumer_files,
	consumer_packages,
	dependencies,
	region_exclusive,
	region_shared,
	unresolved_edges::bigint AS unresolved_edges
FROM	code.object_map_claim_vw
ORDER BY object_key, canonical_object_id
