-- A table's indexed neighbourhood. Foreign-key tables are one hop;
-- dependent database code/views and application code are at most two hops.
-- Names resolve only the requested root; every graph/bridge join uses ids.
WITH RECURSIVE root AS (
	SELECT o.db_node_id
	FROM code.db_object_vw AS o
	WHERE o.catalog_name = :catalog
		AND o.provider = :provider
		AND o.schema_name = :schema
		AND o.object_name = :table
		AND o.object_type = 'table'
), database_nodes AS (
	SELECT 'db:' || o.db_node_id AS id,
		CASE WHEN o.object_type IN ('table', 'view') THEN o.object_type ELSE 'code' END AS kind,
		o.schema_name || '.' || o.object_name || COALESCE(o.signature, '') AS label
	FROM code.db_object_vw AS o
	JOIN code.db_source AS s ON s.db_source_id = o.db_source_id
	WHERE s.catalog_name = :catalog AND s.provider = :provider
), application_nodes AS (
	SELECT DISTINCT 'file:' || f.code_file_id AS id,
		'code'::TEXT AS kind, p.repository_key || ':' || f.relative_path AS label
	FROM code.db_code_reference AS r
	JOIN code.file AS f ON f.code_file_id = r.code_file_id
	JOIN code.repository AS p ON p.repository_id = f.repository_id
	WHERE r.code_symbol_id IS NULL
), call_nodes AS (
	SELECT 'symbol:' || y.code_symbol_id AS id, 'code'::TEXT AS kind,
		r.repository_key || ':' || f.relative_path || ':' || y.qualified_name AS label
	FROM code.symbol AS y
	JOIN code.file AS f ON f.code_file_id = y.code_file_id
	JOIN code.repository AS r ON r.repository_id = f.repository_id
), all_nodes AS (
	SELECT * FROM database_nodes
	UNION
	SELECT * FROM application_nodes
	UNION
	SELECT * FROM call_nodes
), database_edges AS (
	SELECT 'reference:' || r.code_db_reference_id AS id,
		'db:' || r.source_node_id AS source, 'db:' || r.target_node_id AS target,
		r.reference_kind::TEXT AS kind
	FROM code.db_reference_vw AS r
	JOIN database_nodes AS s ON s.id = 'db:' || r.source_node_id
	JOIN database_nodes AS t ON t.id = 'db:' || r.target_node_id
), bridge_maps AS (
	SELECT DISTINCT installation, map_name, seam FROM code.db_bridge_coverage_vw
	WHERE installation = :installation
), bridge_edges AS (
	SELECT DISTINCT 'bridge:' || r.code_db_code_reference_id || ':' || b.code_db_binding_id AS id,
		CASE WHEN r.code_symbol_id IS NOT NULL THEN 'symbol:' || r.code_symbol_id
			ELSE 'file:' || r.code_file_id END AS source,
		'db:' || b.db_node_id AS target, 'invoke'::TEXT AS kind
	FROM code.db_code_reference AS r
	JOIN bridge_maps AS m ON m.seam = r.seam
	JOIN code.db_binding_vw AS b ON b.binding_name = r.binding_name
		AND b.map_name = m.map_name AND b.installation = m.installation
	JOIN database_nodes AS d ON d.id = 'db:' || b.db_node_id
	WHERE b.installation = :installation AND b.resolves_now
		AND (r.code_symbol_id IS NOT NULL OR r.code_file_id IS NOT NULL)
), call_edges AS (
	SELECT 'call:' || r.code_relationship_id AS id,
		'symbol:' || r.source_code_symbol_id AS source,
		CASE WHEN r.target_code_symbol_id IS NOT NULL THEN 'symbol:' || r.target_code_symbol_id
			ELSE 'file:' || r.target_code_file_id END AS target,
		'call'::TEXT AS kind
	FROM code.relationship_vw AS r
	WHERE r.is_resolved AND NOT r.is_test AND r.edge_kind IN ('call', 'internal_call')
		AND r.source_code_symbol_id IS NOT NULL
		AND (r.target_code_symbol_id IS NOT NULL OR r.target_code_file_id IS NOT NULL)
), all_edges AS (
	SELECT * FROM database_edges
	UNION ALL SELECT * FROM bridge_edges
	UNION ALL SELECT * FROM call_edges
), dependent(id, depth, path) AS (
	SELECT 'db:' || r.db_node_id, 0, ARRAY['db:' || r.db_node_id] FROM root AS r
	UNION ALL
	SELECT e.source, d.depth + 1, d.path || e.source
	FROM dependent AS d
	JOIN all_edges AS e ON e.target = d.id
	WHERE d.depth < 2 AND e.kind <> 'foreign_key' AND NOT e.source = ANY(d.path)
), linked_tables AS (
	SELECT e.source AS id FROM database_edges AS e JOIN root AS r ON e.target = 'db:' || r.db_node_id
	WHERE e.kind = 'foreign_key'
	UNION
	SELECT e.target FROM database_edges AS e JOIN root AS r ON e.source = 'db:' || r.db_node_id
	WHERE e.kind = 'foreign_key'
), reached AS (
	SELECT d.id, MIN(d.depth) AS depth FROM dependent AS d GROUP BY d.id
	UNION ALL
	SELECT t.id, 1 FROM linked_tables AS t
), distances AS (
	SELECT r.id, MIN(r.depth) AS depth FROM reached AS r GROUP BY r.id
), nodes AS (
	SELECT DISTINCT n.id, n.kind, n.label, d.depth
	FROM all_nodes AS n JOIN distances AS d ON d.id = n.id
), edges AS (
	SELECT e.* FROM all_edges AS e
	JOIN distances AS s ON s.id = e.source
	JOIN distances AS t ON t.id = e.target
	WHERE (e.kind = 'foreign_key' AND (s.depth = 0 OR t.depth = 0))
		OR (e.kind <> 'foreign_key' AND s.depth = t.depth + 1)
), node_rows AS (
	SELECT jsonb_agg(to_jsonb(n) ORDER BY n.depth, n.id) AS nodes FROM nodes AS n
), edge_rows AS (
	SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) AS edges FROM edges AS e
), root_rows AS (
	SELECT count(*) AS root_count, MIN('db:' || r.db_node_id) AS root_id FROM root AS r
)
SELECT r.root_count, r.root_id, COALESCE(n.nodes, '[]'::JSONB) AS nodes,
	COALESCE(e.edges, '[]'::JSONB) AS edges
FROM root_rows AS r CROSS JOIN node_rows AS n CROSS JOIN edge_rows AS e;
