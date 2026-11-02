-- Follow the source-declared approved catalogue through resolved index edges.
-- A matching operation/function name alone never establishes a binding.
WITH catalogues AS (
    SELECT r.repository_key, min(r.target_code_symbol_id) AS factory_symbol_id
    FROM code.relationship_vw r
    JOIN code.edge_vw e USING (code_edge_id)
    WHERE r.edge_kind = 'registration' AND NOT r.is_test
      AND e.evidence = 'ast.Dict.key:approved_implementations'
    GROUP BY r.repository_key
    HAVING count(*) = 1 AND bool_and(r.is_resolved)
), bindings AS (
    SELECT o.application_name, o.application_operation_id,
           min(r.target_code_symbol_id) AS code_symbol_id
    FROM code.object_map_flow_operation_vw o
    JOIN catalogues c ON c.repository_key = o.application_name
    JOIN code.relationship_vw r ON r.source_code_symbol_id = c.factory_symbol_id
    JOIN code.edge_vw e ON e.code_edge_id = r.code_edge_id
    WHERE r.edge_kind = 'registration'
      AND r.is_resolved AND NOT r.is_test
      AND e.evidence = 'ast.Dict.key:' || o.operation_name
    GROUP BY o.application_name, o.application_operation_id
    HAVING count(DISTINCT r.target_code_symbol_id) = 1
       AND count(DISTINCT c.factory_symbol_id) = 1
)
SELECT o.application_name,o.application_operation_id,o.operation_name,o.description,o.ordinal,
       b.code_symbol_id,n.dotted_identity,n.repository_key,n.relative_path,
       n.coverage_status,n.coverage_reason
FROM code.object_map_flow_operation_vw o
LEFT JOIN bindings b USING (application_name,application_operation_id)
LEFT JOIN code.control_flow_node_vw n ON n.code_symbol_id=b.code_symbol_id
  AND (n.kind='entry' OR n.node_key IS NULL)
ORDER BY o.application_name,o.ordinal,o.application_operation_id
