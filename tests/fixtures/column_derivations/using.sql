SELECT
	k
FROM app.src AS s
JOIN app.dim AS d
	USING (k);
