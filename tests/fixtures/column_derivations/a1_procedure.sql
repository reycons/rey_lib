CREATE PROCEDURE app.p()
LANGUAGE plpgsql
AS $$
BEGIN
	INSERT INTO app.stage (x, k)
	SELECT
		s.a * d.m,
		s.k
	FROM app.src AS s
	JOIN app.dim AS d
		ON d.k = s.k;
END
$$;
