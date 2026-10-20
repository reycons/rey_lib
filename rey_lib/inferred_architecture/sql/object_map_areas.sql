-- The Object Map's estate summary areas (backlog 864): one row per
-- latest-generation area, as code.object_map_area_vw states it.
SELECT	inferred_architecture_area_id,
	inferred_architecture_generation_id,
	area_key,
	area_label,
	claim_count,
	internal_relationship_count,
	crossing_relationship_count
FROM	code.object_map_area_vw
ORDER BY area_label, inferred_architecture_area_id
