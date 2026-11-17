CREATE PROCEDURE app.p(p_run bigint)
LANGUAGE plpgsql
AS $$
BEGIN
	INSERT INTO app.stage (x, k)
	SELECT
		s.a * d.m,
		s.k
	FROM app.src AS s
	JOIN app.dim AS d
		ON d.k = s.k
	WHERE s.run_id = p_run
	ON CONFLICT (k) DO UPDATE SET x = excluded.x;
END
$$;
