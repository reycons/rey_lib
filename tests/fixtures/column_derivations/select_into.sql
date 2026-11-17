CREATE FUNCTION app.f()
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
	v integer;
BEGIN
	SELECT
		s.a
	INTO v
	FROM app.src AS s;
	RETURN v;
END
$$;
