CREATE PROCEDURE app.p()
LANGUAGE plpgsql
AS $$
DECLARE
	v numeric;
BEGIN
	INSERT INTO app.stage (x)
	VALUES (v);
END
$$;
