from database import Database, _build_conn_str, _row_to_dict

def test_qident_escapes_brackets():
    assert Database._qident("table]name") == "[table]]name]"

def test_find_col_case_insensitive():
    cols = [{"column": "CustomerID"}, {"column": "Organization"}]
    assert Database._find_col(cols, ["customerid"]) == "CustomerID"
    assert Database._find_col(cols, ["Missing"]) is None

def test_text_columns_filters_by_type():
    cols = [
        {"column": "Name", "type": "nvarchar"},
        {"column": "Age", "type": "int"},
        {"column": "Notes", "type": "text"},
    ]
    result = Database._text_columns(cols)
    assert result == ["Name", "Notes"]

def test_date_key_priority():
    row = {"OrderDate": "2024-01-01", "HistoryTime": "2024-02-01"}
    assert Database._date_key(row) == "2024-02-01"  # HistoryTime wins
