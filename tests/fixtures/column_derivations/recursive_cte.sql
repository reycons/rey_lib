WITH RECURSIVE w AS (
	SELECT
		n.id
	FROM app.node AS n
	UNION ALL
	SELECT
		n.id
	FROM app.node AS n
	JOIN w
		ON n.parent_id = w.id
)
SELECT
	w.id
FROM w;
