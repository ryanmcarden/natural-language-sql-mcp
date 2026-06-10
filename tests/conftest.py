import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_db():
    """Database instance with a mocked pyodbc connection."""
    with patch("database.pyodbc.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        
        from database import Database
        db = Database()
        db._local.conn = mock_conn  # inject directly
        yield db, mock_cursor
