import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DB_AVAILABLE"),
    reason="Requires SQL Server — set TEST_DB_AVAILABLE=1"
)

def test_search_customers_empty_db():
    from database import Database
    db = Database()
    result = db.search_customers(search="nobody")
    assert result["count"] == 0
    assert result["customers"] == []
