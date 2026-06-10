from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

import pyodbc
from dotenv import load_dotenv
load_dotenv()


def _build_conn_str() -> str:
    driver = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server")
    server = os.getenv("DB_SERVER", "localhost")
    db     = os.getenv("DB_NAME",   "embroidery")
    user   = os.getenv("DB_USER",   "sa")
    pwd    = os.getenv("DB_PASSWORD", "")
    trust  = os.getenv("DB_TRUST_CERT", "yes")
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={db};"
        f"UID={user};"
        f"PWD={pwd};"
        f"TrustServerCertificate={trust};"
    )


def _row_to_dict(cursor, row) -> dict:
    return {col[0]: val for col, val in zip(cursor.description, row)}


def _rows_to_list(cursor, rows) -> list:
    return [_row_to_dict(cursor, r) for r in rows]

def _i(val) -> int:
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0

def _f(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


import threading as _threading

class Database:
    def __init__(self) -> None:
        self._conn_str = _build_conn_str()
        # Thread-local storage: each thread gets its own pyodbc connection.
        # This is required when handlers run in a thread pool (e.g. Starlette
        # sync routes) — pyodbc connections are not thread-safe.
        self._local = _threading.local()
        # Schema caches are read-only after first population, safe to share.
        self._table_exists_cache: dict[str, bool] = {}
        self._columns_cache: dict[str, list[dict]] = {}

    def warm(self) -> None:
        """Pre-establish a DB connection on the calling thread."""
        self._get_persistent()

    def _get_persistent(self) -> "pyodbc.Connection":
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pyodbc.connect(self._conn_str, autocommit=True)
            self._local.conn = conn
        return conn

    @contextmanager
    def _conn(self):
        """Yield this thread's persistent connection; reconnect on DB errors."""
        conn = self._get_persistent()
        try:
            yield conn
        except pyodbc.Error:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
            raise



    # --- Internal intelligence helpers ---

    @staticmethod
    def _qident(name: str) -> str:
        return "[" + str(name).replace("]", "]]" ) + "]"

    def _table_exists(self, cur, table_name: str) -> bool:
        key = table_name.lower()
        if key not in self._table_exists_cache:
            cur.execute(
                """
                SELECT 1
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
                  AND LOWER(TABLE_NAME) = LOWER(?)
                """,
                table_name,
            )
            self._table_exists_cache[key] = cur.fetchone() is not None
        return self._table_exists_cache[key]

    def _columns_for_table(self, cur, table_name: str) -> list[dict]:
        key = table_name.lower()
        if key not in self._columns_cache:
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE LOWER(TABLE_NAME) = LOWER(?)
                ORDER BY ORDINAL_POSITION
                """,
                table_name,
            )
            self._columns_cache[key] = [{"column": r[0], "type": r[1]} for r in cur.fetchall()]
        return self._columns_cache[key]

    @staticmethod
    def _find_col(columns: list[dict] | list[str], candidates: list[str]) -> str | None:
        names = [c["column"] if isinstance(c, dict) else str(c) for c in columns]
        low = {n.lower(): n for n in names}
        for candidate in candidates:
            hit = low.get(candidate.lower())
            if hit:
                return hit
        return None

    @staticmethod
    def _text_columns(columns: list[dict], preferred: list[str] | None = None) -> list[str]:
        text_types = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
        names = [c["column"] for c in columns if str(c.get("type", "")).lower() in text_types]
        if preferred:
            low = {n.lower(): n for n in names}
            picked = [low[p.lower()] for p in preferred if p.lower() in low]
            return picked or names
        return names

    @staticmethod
    def _date_key(row: dict) -> Any:
        for k in ("HistoryTime", "ContactDate", "ContactTime", "HistoryDate", "DateCreated", "CreatedDate", "DateEntered", "EntryDate", "OrderDate", "InvoiceDate", "DateAssigned"):
            if row.get(k) is not None:
                return row.get(k)
        return None

    def _resolve_customer_id(self, cur, customer_id: int | None = None, search: str = "", order_no: str = "") -> dict:
        if customer_id is not None:
            cur.execute("SELECT TOP (1) CustomerID, Organization FROM Customers WHERE CustomerID = ?", customer_id)
            r = cur.fetchone()
            return {"customer_id": customer_id, "customer_name": r[1] if r else None, "source": "customer_id"}
        if order_no:
            cur.execute(
                """
                SELECT TOP (1) c.CustomerID, c.Organization
                FROM Orders o
                JOIN Customers c ON c.CustomerID = o.CustomerID
                WHERE o.OrderNo = ?
                """,
                order_no,
            )
            r = cur.fetchone()
            if r:
                return {"customer_id": r[0], "customer_name": r[1], "source": "order_no"}
        if search:
            like = f"%{search}%"
            cur.execute(
                """
                SELECT TOP (1) c.CustomerID, c.Organization
                FROM Customers c
                WHERE c.Organization LIKE ? OR c.Email LIKE ? OR c.CustomerCode LIKE ?
                ORDER BY CASE WHEN c.Organization = ? THEN 0 ELSE 1 END, c.LastOrderDate DESC
                """,
                like, like, like, search,
            )
            r = cur.fetchone()
            if r:
                return {"customer_id": r[0], "customer_name": r[1], "source": "customer_search"}
            cur.execute(
                """
                SELECT TOP (1) c.CustomerID, c.Organization
                FROM Contacts ct
                JOIN Customers c ON c.CustomerID = ct.CustID
                WHERE ct.FullName LIKE ? OR ct.Email LIKE ? OR ct.Telephone LIKE ? OR ct.Mobile LIKE ?
                ORDER BY ct.LastContact DESC
                """,
                like, like, like, like,
            )
            r = cur.fetchone()
            if r:
                return {"customer_id": r[0], "customer_name": r[1], "source": "contact_search"}
        return {"customer_id": None, "customer_name": None, "source": None}


    # --- Customers ---
def search_customers(self, search: str = "", limit: int = 20) -> dict:
    limit = min(max(1, int(limit or 20)), 100)   # ← add this
    with self._conn() as conn:
        
        with self._conn() as conn:
            cur = conn.cursor()
            if search:
                cur.execute(
                    "SELECT TOP (?) CustomerID, CustomerCode, Organization, "
                    "PhoneNumber, Email, City, State, ZipCode, "
                    "CustomerType, StartDate, LastOrderDate, Terms "
                    "FROM Customers "
                    "WHERE Organization LIKE ? OR Email LIKE ? "
                    "ORDER BY Organization",
                    limit, f"%{search}%", f"%{search}%",
                )
            else:
                cur.execute(
                    "SELECT TOP (?) CustomerID, CustomerCode, Organization, "
                    "PhoneNumber, Email, City, State, ZipCode, "
                    "CustomerType, StartDate, LastOrderDate, Terms "
                    "FROM Customers ORDER BY Organization",
                    limit,
                )
            rows = cur.fetchall()
            return {"customers": _rows_to_list(cur, rows), "count": len(rows)}


    def get_customer(self, customer_id: int) -> dict:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT CustomerID, CustomerCode, Organization, "
                "Address1, Address2, City, State, ZipCode, Country, "
                "PhoneNumber, FaxNumber, Email, WebURL, "
                "CustomerType, Terms, StartDate, LastOrderDate, "
                "Taxable, UPSNo, FedExNo, NoEmployees "
                "FROM Customers WHERE CustomerID = ?",
                customer_id,
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"Customer {customer_id} not found"}
            customer = _row_to_dict(cur, row)

            cur.execute(
                "SELECT ContactID, FirstName, LastName, FullName, "
                "Email, Telephone, Mobile, Fax, "
                "WebUserName, StickyNote, DateCreated, LastContact "
                "FROM Contacts WHERE CustID = ? "
                "ORDER BY LastName, FirstName",
                customer_id,
            )
            customer["contacts"] = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                "SELECT TOP (10) OrderID, OrderNo, OrderDate, OrderStatus, ItemStatus, "
                "PlannedShipDate, InHandsDate, DateShipped, "
                "TrackingNumber, InvoiceNumber, Description "
                "FROM Orders WHERE CustomerID = ? ORDER BY OrderDate DESC",
                customer_id,
            )
            customer["recent_orders"] = _rows_to_list(cur, cur.fetchall())
            return customer

    # --- Orders ---


    def list_orders(self, status: str = "", customer_id=None, search: str = "", limit: int = 20) -> dict:
        conditions, params = [], [limit]
        if status:
            conditions.append("o.OrderStatus = ?")
            params.append(status)
        if customer_id is not None:
            conditions.append("o.CustomerID = ?")
            params.append(customer_id)
        if search:
            conditions.append("(o.OrderNo LIKE ? OR c.Organization LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT TOP (?) o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, "
                f"o.ItemStatus, o.PlannedShipDate, o.InHandsDate, "
                f"o.DateShipped, o.TrackingNumber, o.InvoiceNumber, "
                f"o.Rush, o.Description, "
                f"c.CustomerID, c.Organization AS CustomerName, c.Email AS CustomerEmail "
                f"FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID "
                f"{where} ORDER BY o.OrderDate DESC",
                *params,
            )
            rows = cur.fetchall()
            return {"orders": _rows_to_list(cur, rows), "count": len(rows)}

    def get_order(self, order_no: str) -> dict:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, "
                "o.ItemStatus, o.EnteredBy, o.Description, o.CustomerPONo, "
                "o.PlannedShipDate, o.InHandsDate, o.EventDate, "
                "o.DateShipped, o.TrackingNumber, o.InvoiceNumber, "
                "o.QuoteNumber, o.Comments, o.Rush, o.Discount, "
                "o.EstimatedShipping, o.ActualShipping, "
                "o.IsEmbroideryOrder, o.IsHeadwearOrder, o.IsGarmentOrder, "
                "o.IsScreenPrintOrder, o.IsLeatherPatchOrder, "
                "o.QtyForPricing, o.QtyCompleted, o.QtySpoiled, "
                "c.CustomerID, c.Organization AS CustomerName, "
                "c.PhoneNumber AS CustomerPhone, c.Email AS CustomerEmail, "
                "ct.FullName AS ContactName, ct.Email AS ContactEmail, "
                "ct.Telephone AS ContactPhone "
                "FROM Orders o "
                "JOIN Customers c ON o.CustomerID = c.CustomerID "
                "LEFT JOIN Contacts ct ON o.ContactID = ct.ContactID "
                "WHERE o.OrderNo = ?",
                order_no,
            )
            row = cur.fetchone()
            if not row:
                return {"error": f"Order '{order_no}' not found"}
            order = _row_to_dict(cur, row)
            order_id = order["OrderID"]

            cur.execute(
                "SELECT OrderDetailID, EmbType, Description, ProdNo, "
                "Size, Color, Quantity, Price, PriceUOM, Total, Comments "
                "FROM OrderDetails WHERE OrderID = ? ORDER BY OrderDetailID",
                order_id,
            )
            order["line_items"] = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                "SELECT EmbDataID, DesignType, Description, Location, "
                "DesignNo, GraphicNo, TextToAdd, TextNotes, "
                "ColorNotes, ModificationsNeeded "
                "FROM EmbData WHERE OrderID = ?",
                order_id,
            )
            order["embroidery_specs"] = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                "SELECT DecorationID, DesignNo, DecType, GraphicNo, "
                "Location1, Text, Details, Exception "
                "FROM DecorationDetails WHERE OrderID = ?",
                order_id,
            )
            order["decoration_details"] = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                "SELECT InvoiceID, InvoiceNo, InvoiceDate, DueDate, "
                "EmbTotal, SalesTax, Shipping, InvoiceTotal "
                "FROM Invoices WHERE OrderID = ?",
                order_id,
            )
            invoice_rows = cur.fetchall()
            order["invoice"] = _row_to_dict(cur, invoice_rows[0]) if invoice_rows else None

            cur.execute(
                "SELECT AssignmentID, MachineNo, DateAssigned "
                "FROM Assignments WHERE OrderNo = ?",
                order_no,
            )
            order["machine_assignments"] = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                "SELECT TOP (10) HistoryType, HistorySource, HistoryTime, "
                "[User], Description, Notes "
                "FROM OrderHistory WHERE OrderNo = ? ORDER BY HistoryTime DESC",
                order_no,
            )
            order["recent_history"] = _rows_to_list(cur, cur.fetchall())
            return order

    def get_order_history(self, order_no: str, limit: int = 50) -> dict:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT TOP (?) HistoryType, HistorySource, HistoryTime, "
                "[User], Description, Notes "
                "FROM OrderHistory WHERE OrderNo = ? ORDER BY HistoryTime DESC",
                limit, order_no,
            )
            rows = cur.fetchall()
            return {"order_no": order_no, "history": _rows_to_list(cur, rows), "count": len(rows)}

    def get_contact_history(
        self,
        order_no: str = "",
        customer_id: int | None = None,
        contact_id: int | None = None,
        search: str = "",
        limit: int = 50,
        describe: bool = False,
    ) -> dict:
        """
        Look up ContactHistory records associated with an order, customer/client,
        contact, or search term. This is read-only and adapts to the actual
        ContactHistory column names in the database.
        """
        limit = min(max(1, int(limit or 50)), 500)

        def qident(name: str) -> str:
            return "[" + str(name).replace("]", "]]" ) + "]"

        def first_existing(cols_lower: dict[str, str], candidates: list[str]) -> str | None:
            for c in candidates:
                hit = cols_lower.get(c.lower())
                if hit:
                    return hit
            return None

        with self._conn() as conn:
            cur = conn.cursor()

            # Find the table, case-insensitive. If it is named slightly differently,
            # return likely candidates instead of failing silently.
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
                  AND LOWER(TABLE_NAME) = LOWER(?)
                """,
                "ContactHistory",
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    SELECT TABLE_NAME
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_NAME LIKE '%Contact%'
                       OR TABLE_NAME LIKE '%History%'
                    ORDER BY TABLE_NAME
                    """
                )
                return {
                    "error": "ContactHistory table not found.",
                    "possible_tables": [r[0] for r in cur.fetchall()],
                    "hint": "Run get_contact_history(describe=True) after confirming the exact table name if your CRM uses a different history table name.",
                }

            table = row[0]
            tq = qident(table)

            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
                ORDER BY ORDINAL_POSITION
                """,
                table,
            )
            col_info = [{"column": r[0], "type": r[1]} for r in cur.fetchall()]
            cols = [c["column"] for c in col_info]
            cols_lower = {c.lower(): c for c in cols}

            if describe:
                cur.execute(f"SELECT TOP (3) * FROM {tq}")
                sample = _rows_to_list(cur, cur.fetchall())
                return {"table": table, "columns": col_info, "sample_rows": sample}

            order_id_col = first_existing(cols_lower, ["OrderID"])
            order_no_col = first_existing(cols_lower, ["OrderNo"])
            customer_col = first_existing(cols_lower, ["CustomerID", "CustID"])
            contact_col  = first_existing(cols_lower, ["ContactID"])

            date_col = first_existing(cols_lower, [
                "HistoryTime", "ContactDate", "ContactTime", "HistoryDate",
                "DateCreated", "CreatedDate", "DateEntered", "EntryDate",
                "LastContact", "TimeStamp", "ModifiedDate"
            ])
            pk_col = first_existing(cols_lower, ["ContactHistoryID", "HistoryID", "ID"])
            order_by_col = date_col or pk_col or (cols[0] if cols else None)

            # Resolve order context up front so order_no can still work even if
            # ContactHistory only stores CustomerID/CustID or ContactID.
            resolved_order = None
            if order_no:
                cur.execute(
                    """
                    SELECT TOP (1) OrderID, OrderNo, CustomerID, ContactID
                    FROM Orders
                    WHERE OrderNo = ?
                    """,
                    order_no,
                )
                r = cur.fetchone()
                if not r:
                    return {"error": f"Order '{order_no}' not found"}
                resolved_order = {
                    "OrderID": r[0],
                    "OrderNo": r[1],
                    "CustomerID": r[2],
                    "ContactID": r[3],
                }

            joins = []
            if order_id_col:
                joins.append(f"LEFT JOIN Orders o ON o.OrderID = ch.{qident(order_id_col)}")
            elif order_no_col:
                joins.append(f"LEFT JOIN Orders o ON o.OrderNo = ch.{qident(order_no_col)}")
            else:
                joins.append("LEFT JOIN Orders o ON 1 = 0")

            if contact_col:
                joins.append(f"LEFT JOIN Contacts ct ON ct.ContactID = ch.{qident(contact_col)}")
            else:
                joins.append("LEFT JOIN Contacts ct ON 1 = 0")

            if customer_col:
                joins.append(f"LEFT JOIN Customers c ON c.CustomerID = ch.{qident(customer_col)}")
            elif contact_col:
                joins.append("LEFT JOIN Customers c ON c.CustomerID = ct.CustID")
            elif order_id_col or order_no_col:
                joins.append("LEFT JOIN Customers c ON c.CustomerID = o.CustomerID")
            else:
                joins.append("LEFT JOIN Customers c ON 1 = 0")

            where, params = [], [limit]

            if order_no:
                if order_no_col:
                    where.append(f"ch.{qident(order_no_col)} = ?")
                    params.append(order_no)
                elif order_id_col:
                    where.append(f"ch.{qident(order_id_col)} = ?")
                    params.append(resolved_order["OrderID"])
                elif contact_col and resolved_order.get("ContactID"):
                    where.append(f"ch.{qident(contact_col)} = ?")
                    params.append(resolved_order["ContactID"])
                elif customer_col and resolved_order.get("CustomerID"):
                    where.append(f"ch.{qident(customer_col)} = ?")
                    params.append(resolved_order["CustomerID"])
                else:
                    return {
                        "error": "Could not relate ContactHistory to this order.",
                        "resolved_order": resolved_order,
                        "contact_history_columns": cols,
                    }

            if customer_id is not None:
                if customer_col:
                    where.append(f"ch.{qident(customer_col)} = ?")
                    params.append(customer_id)
                elif contact_col:
                    where.append("ct.CustID = ?")
                    params.append(customer_id)
                elif order_id_col or order_no_col:
                    where.append("o.CustomerID = ?")
                    params.append(customer_id)
                else:
                    return {
                        "error": "ContactHistory has no CustomerID/CustID, ContactID, OrderID, or OrderNo column to filter by customer.",
                        "contact_history_columns": cols,
                    }

            if contact_id is not None:
                if contact_col:
                    where.append(f"ch.{qident(contact_col)} = ?")
                    params.append(contact_id)
                else:
                    return {
                        "error": "ContactHistory has no ContactID column to filter by contact.",
                        "contact_history_columns": cols,
                    }

            if search:
                text_types = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
                text_cols = [c["column"] for c in col_info if c["type"].lower() in text_types]
                search_clauses = [f"ch.{qident(c)} LIKE ?" for c in text_cols]
                params.extend([f"%{search}%"] * len(text_cols))
                search_clauses.extend([
                    "c.Organization LIKE ?",
                    "c.Email LIKE ?",
                    "ct.FullName LIKE ?",
                    "ct.Email LIKE ?",
                    "o.OrderNo LIKE ?",
                ])
                params.extend([f"%{search}%"] * 5)
                where.append("(" + " OR ".join(search_clauses) + ")")

            if not where:
                return {
                    "error": "Provide at least one filter: order_no, customer_id, contact_id, or search.",
                    "contact_history_columns": cols,
                }

            where_sql = "WHERE " + " AND ".join(where)
            order_sql = f"ORDER BY ch.{qident(order_by_col)} DESC" if order_by_col else ""

            sql = f"""
                SELECT TOP (?)
                    ch.*,
                    o.OrderNo       AS RelatedOrderNo,
                    o.OrderDate     AS RelatedOrderDate,
                    o.OrderStatus   AS RelatedOrderStatus,
                    c.CustomerID    AS RelatedCustomerID,
                    c.Organization  AS RelatedCustomerName,
                    ct.ContactID    AS RelatedContactID,
                    ct.FullName     AS RelatedContactName,
                    ct.Email        AS RelatedContactEmail
                FROM {tq} ch
                {' '.join(joins)}
                {where_sql}
                {order_sql}
            """
            cur.execute(sql, *params)
            rows = cur.fetchall()

            return {
                "filters": {
                    "order_no": order_no or None,
                    "customer_id": customer_id,
                    "contact_id": contact_id,
                    "search": search or None,
                },
                "resolved_order": resolved_order,
                "history": _rows_to_list(cur, rows),
                "count": len(rows),
                "table": table,
            }


    # --- Internal intelligence tools ---

    def get_customer_360(
        self,
        customer_id: int | None = None,
        search: str = "",
        order_no: str = "",
        days: int = 365,
        limit: int = 20,
    ) -> dict:
        """Full customer/client briefing: profile, contacts, orders, revenue, notes, risks, products."""
        days = min(max(1, int(days or 365)), 3650)
        limit = min(max(1, int(limit or 20)), 100)
        with self._conn() as conn:
            cur = conn.cursor()
            resolved = self._resolve_customer_id(cur, customer_id=customer_id, search=search, order_no=order_no)
            cid = resolved.get("customer_id")
            if cid is None:
                return {"error": "Customer not found. Provide customer_id, customer search text, or order_no.", "resolved": resolved}

            profile = self.get_customer(cid)

            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT o.OrderID) AS OrderCount,
                    MIN(o.OrderDate)          AS FirstOrderDate,
                    MAX(o.OrderDate)          AS LastOrderDate,
                    SUM(COALESCE(od.Total, 0)) AS TotalRevenue,
                    AVG(CASE WHEN od.Total IS NOT NULL THEN CAST(od.Total AS float) END) AS AvgLineTotal
                FROM Orders o
                LEFT JOIN OrderDetails od ON od.OrderID = o.OrderID
                WHERE o.CustomerID = ?
                  AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND o.OrderNo NOT LIKE 'T%'
                  AND o.OrderStatus NOT LIKE '%cancel%'
                """,
                cid, -days,
            )
            r = cur.fetchone()
            revenue_summary = {
                "period_days": days,
                "order_count": _i(r[0]) if r else 0,
                "first_order_date": r[1] if r else None,
                "last_order_date": r[2] if r else None,
                "total_revenue": round(_f(r[3]), 2) if r else 0,
                "avg_line_total": round(_f(r[4]), 2) if r else 0,
            }

            cur.execute(
                """
                SELECT TOP (?) o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus,
                       o.PlannedShipDate, o.InHandsDate, o.DateShipped, o.TrackingNumber,
                       o.InvoiceNumber, o.Rush, o.Description,
                       SUM(COALESCE(od.Total, 0)) AS OrderTotal
                FROM Orders o
                LEFT JOIN OrderDetails od ON od.OrderID = o.OrderID
                WHERE o.CustomerID = ? AND o.OrderNo NOT LIKE 'T%'
                GROUP BY o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus,
                         o.PlannedShipDate, o.InHandsDate, o.DateShipped, o.TrackingNumber,
                         o.InvoiceNumber, o.Rush, o.Description
                ORDER BY o.OrderDate DESC
                """,
                limit, cid,
            )
            recent_orders = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                """
                SELECT TOP (?) od.ProdNo, p.Brand, p.Title, p.Category,
                       SUM(COALESCE(od.Quantity, 0)) AS TotalQty,
                       SUM(COALESCE(od.Total, 0))    AS TotalRevenue,
                       COUNT(DISTINCT o.OrderID)     AS OrderCount,
                       MAX(o.OrderDate)              AS LastOrderDate
                FROM Orders o
                JOIN OrderDetails od ON od.OrderID = o.OrderID
                LEFT JOIN Products p ON p.ProdNo = od.ProdNo
                WHERE o.CustomerID = ?
                  AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND od.ProdNo IS NOT NULL AND od.ProdNo <> ''
                  AND o.OrderNo NOT LIKE 'T%'
                  AND o.OrderStatus NOT LIKE '%cancel%'
                GROUP BY od.ProdNo, p.Brand, p.Title, p.Category
                ORDER BY TotalRevenue DESC
                """,
                limit, cid, -days,
            )
            top_products = _rows_to_list(cur, cur.fetchall())

            cur.execute(
                """
                SELECT TOP (?) o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus,
                       o.PlannedShipDate, o.InHandsDate, o.Rush, o.QtySpoiled, o.Comments,
                       c.Organization AS CustomerName,
                       DATEDIFF(day, o.OrderDate, GETDATE()) AS AgeDays,
                       DATEDIFF(day, GETDATE(), COALESCE(o.PlannedShipDate, o.InHandsDate)) AS DaysUntilDue
                FROM Orders o
                JOIN Customers c ON c.CustomerID = o.CustomerID
                WHERE o.CustomerID = ?
                  AND o.OrderNo NOT LIKE 'T%'
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus NOT IN ('Invoiced', 'Tentative', 'Completed', 'Void')
                ORDER BY
                    CASE WHEN COALESCE(o.PlannedShipDate, o.InHandsDate) < GETDATE() THEN 0 ELSE 1 END,
                    COALESCE(o.PlannedShipDate, o.InHandsDate), o.OrderDate DESC
                """,
                limit, cid,
            )
            open_orders = _rows_to_list(cur, cur.fetchall())

        contact_history = self.get_contact_history(customer_id=cid, limit=min(limit, 25))
        return {
            "resolved": resolved,
            "profile": profile,
            "revenue_summary": revenue_summary,
            "recent_orders": recent_orders,
            "open_orders": open_orders,
            "top_products": top_products,
            "contact_history": contact_history,
        }

    def search_all_internal_notes(
        self,
        search: str,
        order_no: str = "",
        customer_id: int | None = None,
        days: int = 365,
        limit: int = 100,
        match_mode: str = "any",
    ) -> dict:
        """Search staff notes/comments across ContactHistory, OrderHistory, Orders, OrderDetails, EmbData, and DecorationDetails."""
        search = str(search or "").strip()
        if not search:
            return {"error": "search is required"}
        days = min(max(1, int(days or 365)), 3650)
        limit = min(max(1, int(limit or 100)), 500)
        mode = (match_mode or "any").lower()
        if mode not in {"any", "all", "phrase"}:
            mode = "any"
        terms = [search] if mode == "phrase" else [t for t in search.replace(',', ' ').split() if t]
        terms = terms[:8]

        def build_clause(alias: str, cols: list[str], prefix_params: list) -> tuple[str, list]:
            if not cols:
                return "1=0", []
            groups = []
            params = []
            for term in terms:
                per_term = " OR ".join([f"{alias}.{self._qident(c)} LIKE ?" for c in cols])
                groups.append(f"({per_term})")
                params.extend([f"%{term}%"] * len(cols))
            joiner = " AND " if mode == "all" else " OR "
            return "(" + joiner.join(groups) + ")", params

        results: list[dict] = []
        with self._conn() as conn:
            cur = conn.cursor()
            resolved = self._resolve_customer_id(cur, customer_id=customer_id, order_no=order_no)
            cid = resolved.get("customer_id") if customer_id is not None or order_no else customer_id

            # ContactHistory uses its own dynamic tool because its relationship columns vary by CRM version.
            try:
                ch = self.get_contact_history(order_no=order_no, customer_id=cid, search=search, limit=min(limit, 100))
                for row in ch.get("history", [])[:limit]:
                    results.append({"source_table": ch.get("table", "ContactHistory"), "source_type": "contact_history", **row})
            except Exception as exc:
                results.append({"source_table": "ContactHistory", "source_type": "error", "error": str(exc)})

            configs = [
                {
                    "table": "OrderHistory", "alias": "oh",
                    "preferred": ["Description", "Notes", "HistoryType", "HistorySource", "User"],
                    "select": "oh.*, o.OrderID AS RelatedOrderID, o.OrderNo AS RelatedOrderNo, c.CustomerID AS RelatedCustomerID, c.Organization AS RelatedCustomerName",
                    "from": "OrderHistory oh LEFT JOIN Orders o ON o.OrderNo = oh.OrderNo LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                    "order_filter": "oh.OrderNo = ?",
                    "customer_filter": "o.CustomerID = ?",
                    "date_filter": "oh.HistoryTime >= DATEADD(day, ?, GETDATE())",
                    "order_by": "oh.HistoryTime DESC",
                },
                {
                    "table": "Orders", "alias": "o",
                    "preferred": ["Comments", "Description", "CustomerPONo", "OrderStatus", "ItemStatus"],
                    "select": "o.OrderID AS RelatedOrderID, o.OrderNo AS RelatedOrderNo, o.OrderDate, o.OrderStatus, o.Comments, o.Description, c.CustomerID AS RelatedCustomerID, c.Organization AS RelatedCustomerName",
                    "from": "Orders o LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                    "order_filter": "o.OrderNo = ?",
                    "customer_filter": "o.CustomerID = ?",
                    "date_filter": "o.OrderDate >= DATEADD(day, ?, GETDATE())",
                    "order_by": "o.OrderDate DESC",
                },
                {
                    "table": "OrderDetails", "alias": "od",
                    "preferred": ["Comments", "Description", "ProdNo", "Color", "Size", "EmbType"],
                    "select": "od.*, o.OrderNo AS RelatedOrderNo, o.OrderDate, c.CustomerID AS RelatedCustomerID, c.Organization AS RelatedCustomerName",
                    "from": "OrderDetails od JOIN Orders o ON o.OrderID = od.OrderID LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                    "order_filter": "o.OrderNo = ?",
                    "customer_filter": "o.CustomerID = ?",
                    "date_filter": "o.OrderDate >= DATEADD(day, ?, GETDATE())",
                    "order_by": "o.OrderDate DESC",
                },
                {
                    "table": "EmbData", "alias": "ed",
                    "preferred": ["TextNotes", "ColorNotes", "Description", "TextToAdd", "ModificationsNeeded", "DesignNo", "Location"],
                    "select": "ed.*, o.OrderNo AS RelatedOrderNo, o.OrderDate, c.CustomerID AS RelatedCustomerID, c.Organization AS RelatedCustomerName",
                    "from": "EmbData ed JOIN Orders o ON o.OrderID = ed.OrderID LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                    "order_filter": "o.OrderNo = ?",
                    "customer_filter": "o.CustomerID = ?",
                    "date_filter": "o.OrderDate >= DATEADD(day, ?, GETDATE())",
                    "order_by": "o.OrderDate DESC",
                },
                {
                    "table": "DecorationDetails", "alias": "dd",
                    "preferred": ["Details", "Exception", "Text", "DesignNo", "GraphicNo", "Location1", "DecType"],
                    "select": "dd.*, o.OrderNo AS RelatedOrderNo, o.OrderDate, c.CustomerID AS RelatedCustomerID, c.Organization AS RelatedCustomerName",
                    "from": "DecorationDetails dd JOIN Orders o ON o.OrderID = dd.OrderID LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                    "order_filter": "o.OrderNo = ?",
                    "customer_filter": "o.CustomerID = ?",
                    "date_filter": "o.OrderDate >= DATEADD(day, ?, GETDATE())",
                    "order_by": "o.OrderDate DESC",
                },
            ]

            for cfg in configs:
                if not self._table_exists(cur, cfg["table"]):
                    continue
                cols = self._columns_for_table(cur, cfg["table"])
                text_cols = self._text_columns(cols, cfg["preferred"])
                clause, clause_params = build_clause(cfg["alias"], text_cols, [])
                where = [clause, cfg["date_filter"]]
                params: list = [limit, *clause_params, -days]
                if order_no:
                    where.append(cfg["order_filter"])
                    params.append(order_no)
                if cid is not None:
                    where.append(cfg["customer_filter"])
                    params.append(cid)
                sql = f"""
                    SELECT TOP (?) {cfg['select']}
                    FROM {cfg['from']}
                    WHERE {' AND '.join(where)}
                    ORDER BY {cfg['order_by']}
                """
                try:
                    cur.execute(sql, *params)
                    for row in _rows_to_list(cur, cur.fetchall()):
                        results.append({"source_table": cfg["table"], "source_type": "note_match", **row})
                except Exception as exc:
                    results.append({"source_table": cfg["table"], "source_type": "error", "error": str(exc)})

        def sort_key(x):
            v = self._date_key(x)
            return str(v) if v is not None else ""
        results = sorted(results, key=sort_key, reverse=True)[:limit]
        return {"search": search, "match_mode": mode, "filters": {"order_no": order_no or None, "customer_id": customer_id, "days": days}, "results": results, "count": len(results)}

    def find_order_risks(
        self,
        days: int = 120,
        limit: int = 50,
        customer_id: int | None = None,
        include_invoiced: bool = False,
    ) -> dict:
        """Find open or recently active orders that look risky: overdue, rush, stalled, spoiled, no tracking, or complaint-like notes."""
        days = min(max(1, int(days or 120)), 1095)
        limit = min(max(1, int(limit or 50)), 200)
        wheres = ["o.OrderDate >= DATEADD(day, ?, GETDATE())", "o.OrderNo NOT LIKE 'T%'"]
        params: list = [limit, -days]
        if not include_invoiced:
            wheres.append("o.OrderStatus NOT LIKE '%cancel%'")
            wheres.append("o.OrderStatus NOT IN ('Invoiced', 'Tentative', 'Completed', 'Void')")
        if customer_id is not None:
            wheres.append("o.CustomerID = ?")
            params.append(customer_id)
        where_sql = " AND ".join(wheres)
        risk_terms = ["angry", "refund", "late", "wrong", "complaint", "cancel", "past due", "rush", "upset", "problem"]
        history_like = " OR ".join(["oh.Notes LIKE ? OR oh.Description LIKE ?" for _ in risk_terms])
        params.extend([f"%{t}%" for t in risk_terms for _ in range(2)])
        sql = f"""
            SELECT TOP (?)
                o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus,
                o.PlannedShipDate, o.InHandsDate, o.DateShipped, o.TrackingNumber,
                o.Rush, o.QtySpoiled, o.Comments,
                c.CustomerID, c.Organization AS CustomerName,
                DATEDIFF(day, o.OrderDate, GETDATE()) AS AgeDays,
                DATEDIFF(day, GETDATE(), COALESCE(o.PlannedShipDate, o.InHandsDate)) AS DaysUntilDue,
                SUM(COALESCE(od.Total, 0)) AS OrderTotal,
                MAX(CASE WHEN {history_like} THEN 1 ELSE 0 END) AS HasRiskHistory,
                (
                    CASE WHEN COALESCE(o.PlannedShipDate, o.InHandsDate) < GETDATE() THEN 40 ELSE 0 END +
                    CASE WHEN o.Rush = 1 THEN 15 ELSE 0 END +
                    CASE WHEN DATEDIFF(day, o.OrderDate, GETDATE()) > 31 THEN 20 ELSE 0 END +
                    CASE WHEN COALESCE(o.QtySpoiled, 0) > 0 THEN 15 ELSE 0 END +
                    CASE WHEN o.DateShipped IS NOT NULL AND (o.TrackingNumber IS NULL OR o.TrackingNumber = '') THEN 10 ELSE 0 END +
                    MAX(CASE WHEN {history_like} THEN 25 ELSE 0 END)
                ) AS RiskScore
            FROM Orders o
            JOIN Customers c ON c.CustomerID = o.CustomerID
            LEFT JOIN OrderDetails od ON od.OrderID = o.OrderID
            LEFT JOIN OrderHistory oh ON oh.OrderNo = o.OrderNo
            WHERE {where_sql}
            GROUP BY o.OrderID, o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus,
                     o.PlannedShipDate, o.InHandsDate, o.DateShipped, o.TrackingNumber,
                     o.Rush, o.QtySpoiled, o.Comments, c.CustomerID, c.Organization
            HAVING (
                CASE WHEN COALESCE(o.PlannedShipDate, o.InHandsDate) < GETDATE() THEN 40 ELSE 0 END +
                CASE WHEN o.Rush = 1 THEN 15 ELSE 0 END +
                CASE WHEN DATEDIFF(day, o.OrderDate, GETDATE()) > 31 THEN 20 ELSE 0 END +
                CASE WHEN COALESCE(o.QtySpoiled, 0) > 0 THEN 15 ELSE 0 END +
                CASE WHEN o.DateShipped IS NOT NULL AND (o.TrackingNumber IS NULL OR o.TrackingNumber = '') THEN 10 ELSE 0 END +
                MAX(CASE WHEN {history_like} THEN 25 ELSE 0 END)
            ) > 0
            ORDER BY RiskScore DESC, COALESCE(o.PlannedShipDate, o.InHandsDate), o.OrderDate DESC
        """
        # The risk-history expression appears three times, so its parameters appear three times.
        final_params = [params[0], params[1]]
        if customer_id is not None:
            final_params.append(customer_id)
        term_params = [f"%{t}%" for t in risk_terms for _ in range(2)]
        final_params = [limit, -days]
        if customer_id is not None:
            final_params.append(customer_id)
        # Rebuild in the actual textual order: SELECT history_like, WHERE params, SELECT MAX history_like, HAVING history_like.
        final_params = [limit, *term_params, -days]
        if customer_id is not None:
            final_params.append(customer_id)
        final_params.extend(term_params)
        final_params.extend(term_params)
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, *final_params)
            rows = _rows_to_list(cur, cur.fetchall())
        return {"period_days": days, "count": len(rows), "orders": rows}

    def explain_order_timeline(self, order_no: str, limit: int = 100) -> dict:
        """Create a readable timeline for an order from order header, OrderHistory, ContactHistory, assignments, invoice, and shipping fields."""
        if not str(order_no or "").strip():
            return {"error": "order_no is required"}
        order = self.get_order(str(order_no))
        if order.get("error"):
            return order
        history = self.get_order_history(str(order_no), limit=limit).get("history", [])
        contacts = self.get_contact_history(order_no=str(order_no), limit=min(limit, 100)).get("history", [])
        events: list[dict] = []
        def add_event(date, source, title, details=None, actor=None):
            if date is not None or title:
                events.append({"date": date, "source": source, "title": title, "details": details, "actor": actor})
        add_event(order.get("OrderDate"), "Orders", "Order placed", order.get("Description"), order.get("EnteredBy"))
        add_event(order.get("PlannedShipDate"), "Orders", "Planned ship date", None, None)
        add_event(order.get("InHandsDate"), "Orders", "In-hands date", None, None)
        add_event(order.get("DateShipped"), "Orders", "Shipped", order.get("TrackingNumber"), None)
        inv = order.get("invoice") or {}
        add_event(inv.get("InvoiceDate"), "Invoices", "Invoiced", f"Invoice {inv.get('InvoiceNo')} total {inv.get('InvoiceTotal')}", None)
        for a in order.get("machine_assignments", []) or []:
            add_event(a.get("DateAssigned"), "Assignments", f"Assigned to machine {a.get('MachineNo')}", None, None)
        for h in history:
            title = h.get("Description") or h.get("HistoryType") or "Order history"
            add_event(h.get("HistoryTime"), "OrderHistory", title, h.get("Notes"), h.get("User"))
        for ch in contacts:
            date = self._date_key(ch)
            title = ch.get("Subject") or ch.get("Description") or ch.get("ContactType") or ch.get("HistoryType") or "Contact history"
            detail = ch.get("Notes") or ch.get("Note") or ch.get("Comments") or ch.get("Comment") or ch.get("Memo")
            add_event(date, "ContactHistory", title, detail, ch.get("User") or ch.get("EnteredBy"))
        events = sorted(events, key=lambda e: str(e.get("date")) if e.get("date") is not None else "", reverse=False)
        return {"order_no": order_no, "order": order, "timeline": events, "count": len(events)}

    def get_reorder_opportunities(
        self,
        days_since_last_order: int = 180,
        min_prior_orders: int = 2,
        min_total_revenue: float = 500,
        category: str = "",
        brand: str = "",
        limit: int = 50,
    ) -> dict:
        """Customers with prior buying history whose last order is old enough to justify a reorder call/email."""
        days_since_last_order = min(max(1, int(days_since_last_order or 180)), 3650)
        min_prior_orders = min(max(1, int(min_prior_orders or 2)), 999)
        limit = min(max(1, int(limit or 50)), 200)
        wheres = ["o.OrderNo NOT LIKE 'T%'", "o.OrderStatus NOT LIKE '%cancel%'"]
        params: list = [limit]
        if category:
            wheres.append("p.Category LIKE ?")
            params.append(f"%{category}%")
        if brand:
            wheres.append("p.Brand LIKE ?")
            params.append(f"%{brand}%")
        sql = f"""
            SELECT TOP (?)
                c.CustomerID, c.Organization AS CustomerName, c.Email, c.PhoneNumber,
                COUNT(DISTINCT o.OrderID) AS OrderCount,
                MAX(o.OrderDate) AS LastOrderDate,
                DATEDIFF(day, MAX(o.OrderDate), GETDATE()) AS DaysSinceLastOrder,
                SUM(COALESCE(od.Total, 0)) AS TotalRevenue,
                MAX(od.ProdNo) AS ExampleProdNo,
                MAX(p.Brand) AS ExampleBrand,
                MAX(p.Category) AS ExampleCategory
            FROM Customers c
            JOIN Orders o ON o.CustomerID = c.CustomerID
            JOIN OrderDetails od ON od.OrderID = o.OrderID
            LEFT JOIN Products p ON p.ProdNo = od.ProdNo
            WHERE {' AND '.join(wheres)}
            GROUP BY c.CustomerID, c.Organization, c.Email, c.PhoneNumber
            HAVING COUNT(DISTINCT o.OrderID) >= ?
               AND SUM(COALESCE(od.Total, 0)) >= ?
               AND DATEDIFF(day, MAX(o.OrderDate), GETDATE()) >= ?
            ORDER BY TotalRevenue DESC
        """
        params.extend([min_prior_orders, min_total_revenue, days_since_last_order])
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, *params)
            rows = _rows_to_list(cur, cur.fetchall())
        return {"filters": {"days_since_last_order": days_since_last_order, "min_prior_orders": min_prior_orders, "min_total_revenue": min_total_revenue, "category": category or None, "brand": brand or None}, "opportunities": rows, "count": len(rows)}

    def get_customer_product_history(
        self,
        customer_id: int | None = None,
        search: str = "",
        days: int = 1095,
        limit: int = 50,
    ) -> dict:
        """Show what a customer usually buys: products, categories, colors, sizes, designs, quantities, and month pattern."""
        days = min(max(1, int(days or 1095)), 3650)
        limit = min(max(1, int(limit or 50)), 200)
        with self._conn() as conn:
            cur = conn.cursor()
            resolved = self._resolve_customer_id(cur, customer_id=customer_id, search=search)
            cid = resolved.get("customer_id")
            if cid is None:
                return {"error": "Customer not found", "resolved": resolved}
            base_params = [cid, -days]
            cur.execute(
                """
                SELECT TOP (?) od.ProdNo, p.Brand, p.Title, p.Category,
                       SUM(COALESCE(od.Quantity, 0)) AS TotalQty,
                       SUM(COALESCE(od.Total, 0)) AS TotalRevenue,
                       COUNT(DISTINCT o.OrderID) AS OrderCount,
                       AVG(CAST(COALESCE(od.Quantity, 0) AS float)) AS AvgQty,
                       MAX(o.OrderDate) AS LastOrderDate
                FROM Orders o
                JOIN OrderDetails od ON od.OrderID = o.OrderID
                LEFT JOIN Products p ON p.ProdNo = od.ProdNo
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND od.ProdNo IS NOT NULL AND od.ProdNo <> '' AND o.OrderNo NOT LIKE 'T%'
                GROUP BY od.ProdNo, p.Brand, p.Title, p.Category
                ORDER BY TotalRevenue DESC
                """,
                limit, *base_params,
            )
            products = _rows_to_list(cur, cur.fetchall())
            cur.execute(
                """
                SELECT TOP (20) COALESCE(p.Category, '(unknown)') AS Category,
                       SUM(COALESCE(od.Quantity, 0)) AS TotalQty,
                       SUM(COALESCE(od.Total, 0)) AS TotalRevenue,
                       COUNT(DISTINCT o.OrderID) AS OrderCount
                FROM Orders o JOIN OrderDetails od ON od.OrderID = o.OrderID
                LEFT JOIN Products p ON p.ProdNo = od.ProdNo
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE()) AND o.OrderNo NOT LIKE 'T%'
                GROUP BY COALESCE(p.Category, '(unknown)')
                ORDER BY TotalRevenue DESC
                """,
                *base_params,
            )
            categories = _rows_to_list(cur, cur.fetchall())
            cur.execute(
                """
                SELECT TOP (20) od.Color, SUM(COALESCE(od.Quantity, 0)) AS TotalQty, COUNT(*) AS LineCount
                FROM Orders o JOIN OrderDetails od ON od.OrderID = o.OrderID
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND od.Color IS NOT NULL AND od.Color <> '' AND o.OrderNo NOT LIKE 'T%'
                GROUP BY od.Color ORDER BY TotalQty DESC
                """,
                *base_params,
            )
            colors = _rows_to_list(cur, cur.fetchall())
            cur.execute(
                """
                SELECT TOP (20) od.Size, SUM(COALESCE(od.Quantity, 0)) AS TotalQty, COUNT(*) AS LineCount
                FROM Orders o JOIN OrderDetails od ON od.OrderID = o.OrderID
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND od.Size IS NOT NULL AND od.Size <> '' AND o.OrderNo NOT LIKE 'T%'
                GROUP BY od.Size ORDER BY TotalQty DESC
                """,
                *base_params,
            )
            sizes = _rows_to_list(cur, cur.fetchall())
            cur.execute(
                """
                SELECT TOP (20) ed.DesignNo, ed.Description, ed.Location,
                       COUNT(DISTINCT o.OrderID) AS OrderCount,
                       MAX(o.OrderDate) AS LastUsedDate
                FROM Orders o JOIN EmbData ed ON ed.OrderID = o.OrderID
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE())
                  AND ed.DesignNo IS NOT NULL AND ed.DesignNo <> '' AND o.OrderNo NOT LIKE 'T%'
                GROUP BY ed.DesignNo, ed.Description, ed.Location
                ORDER BY OrderCount DESC, LastUsedDate DESC
                """,
                *base_params,
            )
            designs = _rows_to_list(cur, cur.fetchall())
            cur.execute(
                """
                SELECT FORMAT(o.OrderDate, 'MM') AS MonthNo, COUNT(DISTINCT o.OrderID) AS OrderCount,
                       SUM(COALESCE(od.Total, 0)) AS Revenue
                FROM Orders o LEFT JOIN OrderDetails od ON od.OrderID = o.OrderID
                WHERE o.CustomerID = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE()) AND o.OrderNo NOT LIKE 'T%'
                GROUP BY FORMAT(o.OrderDate, 'MM') ORDER BY MonthNo
                """,
                *base_params,
            )
            monthly = _rows_to_list(cur, cur.fetchall())
        return {"resolved": resolved, "period_days": days, "products": products, "categories": categories, "colors": colors, "sizes": sizes, "designs": designs, "monthly": monthly}

    def find_similar_past_orders(
        self,
        prod_no: str = "",
        category: str = "",
        customer_type: str = "",
        quantity: int | None = None,
        decoration_type: str = "",
        search: str = "",
        days: int = 1095,
        limit: int = 50,
    ) -> dict:
        """Find past orders similar to a described job."""
        days  = min(max(1, int(days or 1095)), 3650)
        limit = min(max(1, int(limit or 50)), 500)
        with self._conn() as conn:
            cur = conn.cursor()
            conditions = ["o.OrderDate >= DATEADD(day, ?, GETDATE())", "o.OrderNo NOT LIKE 'T%'"]
            params: list = [-days]
            if prod_no:
                conditions.append("od.ProdNo = ?")
                params.append(prod_no)
            if category:
                conditions.append("p.Category = ?")
                params.append(category)
            if quantity is not None:
                conditions.append("od.Quantity >= ?")
                params.append(int(quantity))
            if search:
                conditions.append("(c.Organization LIKE ? OR od.Description LIKE ?)")
                params += [f"%{search}%", f"%{search}%"]
            where = " AND ".join(conditions)
            cur.execute(
                f"""SELECT TOP ({limit})
                    o.OrderNo, o.OrderDate, o.OrderStatus,
                    c.CustomerID, c.Organization AS CustomerName,
                    od.ProdNo, od.Color, od.Size, od.Quantity, od.Price, od.Total, od.Description
                FROM Orders o
                JOIN OrderDetails od ON od.OrderID = o.OrderID
                JOIN Customers c ON c.CustomerID = o.CustomerID
                LEFT JOIN Products p ON p.ProdNo = od.ProdNo
                WHERE {where}
                ORDER BY o.OrderDate DESC""",
                *params,
            )
            orders = _rows_to_list(cur, cur.fetchall())
        return {"count": len(orders), "orders": orders}

    def get_orders_by_prodno(self, prod_no: str = "R112", status: str = "", days: int = 365, limit: int = 50) -> dict:
        """List orders for a given ProdNo sorted by quantity descending."""
        days  = min(max(1, int(days or 365)), 3650)
        limit = min(max(1, int(limit or 50)), 500)
        with self._conn() as conn:
            cur = conn.cursor()
            params: list = [prod_no, -days]
            where = "od.ProdNo = ? AND o.OrderDate >= DATEADD(day, ?, GETDATE()) AND o.OrderNo NOT LIKE 'T%'"
            if status:
                where += " AND o.OrderStatus = ?"
                params.append(status)
            cur.execute(
                f"""SELECT TOP ({limit})
                    o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus, o.Rush,
                    o.PlannedShipDate, o.InHandsDate, o.DateShipped,
                    c.CustomerID, c.Organization AS CustomerName,
                    od.OrderDetailID, od.ProdNo, od.Color, od.Size,
                    od.Quantity, od.Price, od.Total, od.Description
                FROM OrderDetails od
                JOIN Orders o ON o.OrderID = od.OrderID
                JOIN Customers c ON c.CustomerID = o.CustomerID
                WHERE {where}
                ORDER BY od.Quantity DESC, o.OrderDate DESC""",
                *params,
            )
            orders = _rows_to_list(cur, cur.fetchall())
        return {"prod_no": prod_no, "count": len(orders), "orders": orders}
