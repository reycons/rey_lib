-- Existing canonical regions determine which call sites support this mapped link.
-- No second reachability resolver: the region view owns the reachable members.
WITH selected AS (
	SELECT r.canonical_object_id, r.linked_canonical_object_id, r.entry_code_symbol_ids
	FROM code.object_map_relationship_vw AS r
	WHERE r.canonical_object_id IN (:sources)
		AND r.linked_canonical_object_id IN (:targets)
), sites AS (
	SELECT DISTINCT s.canonical_object_id, s.linked_canonical_object_id,
		e.code_relationship_id, e.code_edge_id, e.repository_key, e.relative_path,
		e.source_line, e.source_column, e.source_identity, e.target_identity,
		e.edge_kind, e.resolution
	FROM selected AS s
	JOIN code.canonical_region_vw AS member
		ON member.canonical_object_id = s.canonical_object_id
		AND member.role IN ('owner', 'exclusive', 'shared')
	JOIN code.relationship_vw AS e
		ON (member.target_code_symbol_id IS NOT NULL
			AND e.source_code_symbol_id = member.target_code_symbol_id)
		OR (member.target_code_symbol_id IS NULL AND member.target_kind = 'module'
			AND e.source_code_symbol_id IS NULL AND e.code_file_id = member.target_code_file_id)
	WHERE e.target_code_symbol_id = ANY(s.entry_code_symbol_ids)
		AND NOT e.is_test AND e.is_resolved
		AND e.edge_kind IN ('call', 'internal_call', 'inherits', 'property_access', 'implements')
)
SELECT * FROM sites
ORDER BY repository_key, relative_path, source_line, source_column, code_relationship_id;
