WITH r AS (
	SELECT
		s.a * 2 AS doubled
	FROM app.src AS s
)
SELECT
	r.doubled
FROM r;
