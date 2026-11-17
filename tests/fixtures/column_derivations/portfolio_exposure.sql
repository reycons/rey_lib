SELECT t.account_id,
    sum(t.quantity * t.price * l.multiplier) AS exposure
   FROM app.trades t
     JOIN app.security_lookup l ON t.security_id = l.security_id
  GROUP BY t.account_id;
