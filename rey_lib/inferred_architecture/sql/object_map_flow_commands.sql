-- CLI inputs proved by source guards or literal-dispatch handlers. Governed
-- declarations remain visible without a binding; source inputs need no seed list.
WITH governed AS (
    SELECT DISTINCT application_name,application_command_id::text,command_name,description,ordinal
    FROM code.object_map_flow_command_vw
), bindings AS (
    SELECT n.* FROM code.control_flow_node_vw n
    WHERE n.kind IN ('condition','entry') AND cardinality(n.commands)>0
      AND EXISTS (SELECT 1 FROM code.relationship_vw r
                  WHERE r.repository_key=n.repository_key AND r.relative_path=n.relative_path AND NOT r.is_test)
), declarations AS (
    SELECT * FROM governed
    UNION ALL
    SELECT DISTINCT n.repository_key,
           'indexed:' || n.repository_key || ':' || command,
           command, NULL::text, 0
    FROM bindings n CROSS JOIN LATERAL unnest(n.commands) AS command
    WHERE command NOT LIKE 'request:%'
      AND NOT EXISTS (SELECT 1 FROM governed d
                      WHERE d.application_name=n.repository_key AND d.command_name=command)
)
SELECT d.application_name,d.application_command_id,d.command_name,d.description,d.ordinal,
       n.code_symbol_id,n.node_key,n.dotted_identity,n.repository_key,n.relative_path,
       n.coverage_status,n.coverage_reason,n.source_line
FROM declarations d
LEFT JOIN bindings n ON n.repository_key=d.application_name AND d.command_name=ANY(n.commands)
  AND (n.kind='entry' OR NOT EXISTS (
      SELECT 1 FROM bindings handler WHERE handler.repository_key=d.application_name
      AND handler.kind='entry' AND d.command_name=ANY(handler.commands)))
ORDER BY d.application_name,d.ordinal,d.application_command_id,n.dotted_identity,n.node_key
