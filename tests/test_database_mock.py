def test_search_customers_returns_error_shape(mock_db):
    db, cur = mock_db
    cur.fetchall.return_value = []
    cur.description = [("CustomerID",), ("Organization",)]
    result = db.search_customers(search="Acme")
    assert "customers" in result
    assert result["count"] == 0

def test_get_customer_not_found(mock_db):
    db, cur = mock_db
    cur.fetchone.return_value = None
    result = db.get_customer(customer_id=99999)
    assert "error" in result
    assert "99999" in result["error"]

def test_limit_is_clamped_in_search_customers(mock_db):
    db, cur = mock_db
    cur.fetchall.return_value = []
    cur.description = []
    # Pass limit=500 — should be clamped to 100 before hitting DB
    db.search_customers(limit=500)
    call_args = cur.execute.call_args[0]
    assert 100 in call_args  # limit param should be 100, not 500
