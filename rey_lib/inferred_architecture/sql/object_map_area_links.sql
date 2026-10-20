-- The Object Map's estate summary links (backlog 864): one row per ordered
-- pair of different areas, as code.object_map_area_link_vw states it.
SELECT	from_area_id,
	from_area_label,
	to_area_id,
	to_area_label,
	relationship_count,
	direct_relationship_count
FROM	code.object_map_area_link_vw
ORDER BY from_area_id, to_area_id
