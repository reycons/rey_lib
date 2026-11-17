SELECT
	st.k,
	sum(st.x) AS total
FROM app.stage AS st
GROUP BY st.k;
