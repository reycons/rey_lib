-- Registered application entries resolved against the index (#1304).
-- No curated flow list. Missing indexed entries remain visible as boundaries.
SELECT	architecture_flow_id,
	flow_key,
	label,
	entry_repository_key,
	entry_identity,
	max_depth,
	sort_order,
	entry_code_symbol_id,
	entry_relative_path,
	entry_symbol_kind,
	entry_start_line
FROM	code.object_map_flow_vw
ORDER BY sort_order, flow_key
