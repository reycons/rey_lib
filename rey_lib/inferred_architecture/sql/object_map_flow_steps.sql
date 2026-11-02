-- Canonical regions own membership. Shared membership stays plural.
WITH owners AS (
    SELECT member.target_code_symbol_id,
           array_agg(DISTINCT claim.object_key ORDER BY claim.object_key) AS owner_keys
    FROM code.canonical_region_vw member
    JOIN code.object_map_claim_vw claim USING (canonical_object_id)
    WHERE member.role IN ('owner','exclusive','shared')
      AND member.target_code_symbol_id IS NOT NULL
    GROUP BY member.target_code_symbol_id
), targets AS (
    SELECT DISTINCT code_edge_id,target_code_symbol_id,target_repository_key,to_reference
    FROM code.relationship_vw
)
SELECT n.*,e.to_key,e.kind AS transition_kind,
       target.target_repository_key,target.to_reference AS callee_reference,
       declaration.text AS target_label,
       to_jsonb(declaration)->>'symbol_name' AS target_symbol_name,
       to_jsonb(declaration)->>'symbol_kind' AS target_symbol_kind,
       to_jsonb(declaration)->>'owner_name' AS target_owner_name,
       target_owners.owner_keys AS target_owner_keys,
       source_owners.owner_keys AS source_owner_keys
FROM code.control_flow_node_vw n
LEFT JOIN code.control_flow_edge_vw e
  ON e.code_symbol_id=n.code_symbol_id AND e.from_key=n.node_key
LEFT JOIN targets target ON target.code_edge_id=n.code_edge_id
  AND (target.target_code_symbol_id=n.target_code_symbol_id
       OR (target.target_code_symbol_id IS NULL AND n.target_code_symbol_id IS NULL))
LEFT JOIN code.control_flow_node_vw declaration
  ON declaration.code_symbol_id=n.target_code_symbol_id AND (declaration.kind='entry' OR declaration.node_key IS NULL)
LEFT JOIN owners target_owners ON target_owners.target_code_symbol_id=n.target_code_symbol_id
LEFT JOIN owners source_owners ON source_owners.target_code_symbol_id=n.code_symbol_id
WHERE n.code_symbol_id=:symbol_id
ORDER BY n.node_key,e.to_key,e.kind
