-- Source-proved attached-channel input guards, in a separate namespace from CLI.
SELECT n.repository_key AS application_name,
       n.code_symbol_id::text || ':' || substring(command FROM 9) AS application_request_id,
       substring(command FROM 9) AS request_name,
       n.dotted_identity AS description,
       n.code_symbol_id,n.node_key,n.dotted_identity,n.repository_key,n.relative_path,
       n.coverage_status,n.coverage_reason,n.source_line
FROM code.control_flow_node_vw n
CROSS JOIN LATERAL unnest(n.commands) AS command
WHERE n.kind='condition' AND command LIKE 'request:%'
  AND EXISTS (SELECT 1 FROM code.relationship_vw r
              WHERE r.repository_key=n.repository_key AND r.relative_path=n.relative_path AND NOT r.is_test)
ORDER BY n.repository_key,n.code_symbol_id,n.source_line,command
