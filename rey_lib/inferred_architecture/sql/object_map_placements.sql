-- The object map's placements (backlog 860): one row per latest-generation
-- membership placing a claim, as code.object_map_placement_vw states it.
SELECT	canonical_object_id,
	object_key,
	inferred_architecture_generation_id,
	inferred_architecture_area_id,
	area_key,
	area_label,
	inferred_architecture_membership_id,
	member_kind,
	placed_by_code_symbol_id,
	placed_by_code_file_id
FROM	code.object_map_placement_vw
ORDER BY canonical_object_id, inferred_architecture_area_id, inferred_architecture_membership_id
