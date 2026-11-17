CREATE FUNCTION app.f()
RETURNS TABLE (total numeric)
LANGUAGE plpgsql
AS $$
BEGIN
	RETURN QUERY
	SELECT
		sum(s.a)
	FROM app.src AS s;
END
$$;
