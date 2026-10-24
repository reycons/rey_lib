-- The governed Tree groups one installation surfaces in one placement (#1218): enabled-only,
-- placement and display order are tree.f_tree_tabs's answer. Both arguments are bound.
SELECT	tree_group_id,
	tree_group_key,
	label,
	icon,
	panel_key,
	panel_configuration
FROM	tree.f_tree_tabs(:installation, :placement)
