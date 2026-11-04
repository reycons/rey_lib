-- Source-proved command guards, including locally registered hosted objects.
-- Shared protocol guards belong to an application only where an indexed call
-- proves that application constructs the canonical HostedObjects owner.
WITH requests AS (
    SELECT n.*,
           CASE WHEN command LIKE 'request-false:%' THEN substring(command FROM 15)
                ELSE substring(command FROM 9) END AS request_name,
           CASE WHEN command LIKE 'request-false:%' THEN 'false' ELSE 'true' END AS request_branch
    FROM code.control_flow_node_vw n
    CROSS JOIN LATERAL unnest(n.commands) AS command
    WHERE n.kind='condition' AND (command LIKE 'request:%' OR command LIKE 'request-false:%')
      AND EXISTS (SELECT 1 FROM code.relationship_vw r
                  WHERE r.repository_key=n.repository_key AND r.relative_path=n.relative_path AND NOT r.is_test)
), hosts AS (
    SELECT DISTINCT repository_key, target_identity
    FROM code.relationship_vw
    WHERE edge_kind='call' AND NOT is_test
      AND target_repository_key='rey_lib'
      AND target_identity='rey_lib.run.hosted_objects.HostedObjects'
), inputs AS (
    SELECT repository_key AS application_name, r.* FROM requests r
    UNION ALL
    SELECT h.repository_key AS application_name, r.*
    FROM requests r JOIN hosts h ON r.dotted_identity LIKE h.target_identity || '.%'
    WHERE h.repository_key<>r.repository_key
)
SELECT application_name,
       code_symbol_id::text || ':' || request_name AS application_request_id,
       request_name, request_branch, dotted_identity AS description,
       code_symbol_id,node_key,dotted_identity,repository_key,relative_path,
       coverage_status,coverage_reason,source_line
FROM inputs
ORDER BY application_name,code_symbol_id,source_line,request_name
