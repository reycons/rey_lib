-- The Tree rows one expansion owns, in the Tree's presentation order (backlog 1207).
--
-- tree.f_tree_rows dispatches the expansion to its one provider (NULL is the
-- persisted provider; an unknown expansion is refused). Every value is bound and
-- every filter is optional: a NULL parameter applies no predicate. Ordering and
-- the bound are the caller's: a root read is ordered by the root's declared
-- sort_order first (NULL for every other read, so no effect there); node_key
-- breaks the final tie. A NULL row_limit is no limit.
SELECT	*
FROM	tree.f_tree_rows(
		CAST(:expansion_tvw AS character varying),
		CAST(:row_type AS character varying),
		CAST(:tree_group_id AS bigint),
		CAST(:tree_root_id AS bigint),
		CAST(:parent_node_key AS text)
	)
ORDER BY tree_group_id,
	CASE WHEN CAST(:row_type AS character varying) = 'root' THEN sort_order END NULLS LAST,
	tree_root_id NULLS FIRST,
	sort_rank NULLS LAST,
	sort_ts DESC NULLS LAST,
	sort_number NULLS LAST,
	sort_text NULLS LAST,
	node_key NULLS FIRST
LIMIT CAST(:row_limit AS bigint)
