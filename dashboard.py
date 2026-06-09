"""
dashboard.py — Embroidery Shop Dashboard
Runs on port 8080 (set DASHBOARD_PORT env var to override).
Open http://localhost:8080 in your browser.
"""
from __future__ import annotations

import os
import json
import ssl
import asyncio
import urllib.request
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from database import Database
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route
import uvicorn

db = Database()


# ── helpers ──────────────────────────────────────────────────────────────────

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


# ── API endpoints ─────────────────────────────────────────────────────────────

def api_kpis(request: Request):
    try:
      return _api_kpis_inner(request)
    except Exception as e:
        import traceback
        print(f"[api_kpis] ERROR: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

def _api_kpis_inner(request: Request):
    now         = datetime.now()
    # Use full-day timestamps so orders placed any time today are included
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    today_end   = now.strftime("%Y-%m-%d 23:59:59")
    month_start = now.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    year_start  = now.replace(month=1, day=1).strftime("%Y-%m-%d 00:00:00")

    # Same periods last year (full-day timestamps)
    ly          = now.replace(year=now.year - 1)
    ly_today_s  = ly.strftime("%Y-%m-%d 00:00:00")
    ly_today_e  = ly.strftime("%Y-%m-%d 23:59:59")
    ly_month_s  = ly.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    ly_month_e  = ly.strftime("%Y-%m-%d 23:59:59")
    ly_year_s   = ly.replace(month=1, day=1).strftime("%Y-%m-%d 00:00:00")
    ly_year_e   = ly.strftime("%Y-%m-%d 23:59:59")

    # Blended revenue: orders < 31 days old → use OrderDetails.Total (not yet invoiced);
    # orders ≥ 31 days old → use Invoices.InvoiceTotal (should be fully billed by then).
    # Cancelled orders are excluded from both legs.
    def blended_revenue(date_from: str, date_to: str) -> dict:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT o.OrderID) AS OrderCount,
                    SUM(CASE
                        WHEN DATEDIFF(day, o.OrderDate, GETDATE()) < 31
                        THEN COALESCE(od_agg.TotalRevenue,  0)
                        ELSE COALESCE(i_agg.TotalInvoiced,  0)
                    END) AS BlendedRevenue
                FROM Orders o
                LEFT JOIN (
                    SELECT OrderID, SUM(Total) AS TotalRevenue
                    FROM   OrderDetails
                    GROUP  BY OrderID
                ) od_agg ON od_agg.OrderID = o.OrderID
                LEFT JOIN (
                    SELECT OrderID, SUM(InvoiceTotal) AS TotalInvoiced
                    FROM   Invoices
                    GROUP  BY OrderID
                ) i_agg ON i_agg.OrderID = o.OrderID
                WHERE o.OrderDate >= ? AND o.OrderDate <= ?
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus <> 'Tentative'
                  AND o.OrderNo NOT LIKE 'T%'
                """,
                date_from, date_to,
            )
            row = cur.fetchone()
            return {
                "OrderCount":   _i(row[0]),
                "TotalRevenue": round(_f(row[1]), 2),
            }

    t_day   = blended_revenue(today_start, today_end)
    t_month = blended_revenue(month_start, today_end)
    t_ytd   = blended_revenue(year_start,  today_end)

    ly_day   = blended_revenue(ly_today_s, ly_today_e)
    ly_month = blended_revenue(ly_month_s, ly_month_e)
    ly_ytd   = blended_revenue(ly_year_s,  ly_year_e)

    # Invoiced totals filtered by InvoiceDate (not OrderDate) — invoices are
    # raised weeks after orders are placed, so these must be queried separately.
    # We use EmbTotal (garment/embroidery cost) rather than InvoiceTotal so that
    # the figure is comparable to OrderDetails.Total (which also excludes shipping).
    def invoice_totals(date_from: str, date_to: str) -> dict:
        with db._conn() as conn:
            cur = conn.cursor()
            # Only count invoices where the order has been fully processed (status = Invoiced)
            cur.execute(
                "SELECT SUM(i.EmbTotal), SUM(i.SalesTax), SUM(i.Shipping), SUM(i.InvoiceTotal), COUNT(*) "
                "FROM Invoices i "
                "JOIN Orders o ON o.OrderID = i.OrderID "
                "WHERE i.InvoiceDate >= ? AND i.InvoiceDate <= ? "
                "AND o.OrderStatus = 'Invoiced' "
                "AND o.OrderNo NOT LIKE 'T%'",
                date_from, date_to,
            )
            row = cur.fetchone()
            return {
                "invoiced":       round(_f(row[0]), 2),  # EmbTotal only — matches OrderDetails.Total semantics
                "tax":            round(_f(row[1]), 2),
                "shipping":       round(_f(row[2]), 2),
                "invoice_total":  round(_f(row[3]), 2),  # full billed amount incl. shipping
                "count":          _i(row[4]),
            }

    inv_month    = invoice_totals(month_start,  today_end)
    inv_ytd      = invoice_totals(year_start,   today_end)
    inv_ly_month = invoice_totals(ly_month_s,   ly_month_e)
    inv_ly_ytd   = invoice_totals(ly_year_s,    ly_year_e)

    # Count orders placed this month that have no invoice yet (work in progress)
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM Orders o "
            "WHERE o.OrderDate >= ? AND o.OrderDate <= ? "
            "AND NOT EXISTS (SELECT 1 FROM Invoices i WHERE i.OrderID = o.OrderID) "
            "AND o.OrderNo NOT LIKE 'T%'",
            month_start, today_end,
        )
        wip_count = _i(cur.fetchone()[0])

    # Direct COUNT queries — no limit, exact status names from DB
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT OrderStatus, COUNT(*) AS cnt "
            "FROM Orders "
            "WHERE OrderStatus IS NOT NULL "
            "  AND OrderDate >= DATEADD(day, -30, GETDATE()) "
            "  AND OrderNo NOT LIKE 'T%' "
            "GROUP BY OrderStatus"
        )
        status_counts = {row[0]: row[1] for row in cur.fetchall()}

    # Confirmed active = in production (excludes Cancelled, Invoiced, and Tentative)
    excluded  = {"cancelled", "cancel", "invoiced", "tentative", "completed", "void"}
    active_ct = sum(v for k, v in status_counts.items() if k.lower() not in excluded)
    tentative_ct = sum(v for k, v in status_counts.items() if k.lower() == "tentative")
    ship_ct   = sum(v for k, v in status_counts.items() if "ship" in k.lower())

    return JSONResponse({
        "today":   {
            "orders":     t_day["OrderCount"],
            "revenue":    t_day["TotalRevenue"],
            "ly_orders":  ly_day["OrderCount"],
            "ly_revenue": ly_day["TotalRevenue"],
        },
        "month":   {
            "orders":        t_month["OrderCount"],
            "revenue":       t_month["TotalRevenue"],
            "invoiced":      inv_month["invoiced"],
            "invoice_count": inv_month["count"],
            "wip_orders":    wip_count,
            "ly_orders":     ly_month["OrderCount"],
            "ly_revenue":    ly_month["TotalRevenue"],
            "ly_invoiced":   inv_ly_month["invoiced"],
        },
        "ytd":     {
            "orders":        t_ytd["OrderCount"],
            "revenue":       t_ytd["TotalRevenue"],
            "invoiced":      inv_ytd["invoiced"],
            "invoice_count": inv_ytd["count"],
            "ly_orders":     ly_ytd["OrderCount"],
            "ly_revenue":    ly_ytd["TotalRevenue"],
            "ly_invoiced":   inv_ly_ytd["invoiced"],
        },
        "open_orders":    active_ct,
        "tentative":      tentative_ct,
        "shipped_orders": ship_ct,
        "all_statuses":   status_counts,
    })


def api_revenue_trend(request: Request):
    # Blended revenue per month (orders < 31 days → order total, older → invoiced total)
    # Plus: actual invoiced total (Invoiced-status orders only) by InvoiceDate per month
    with db._conn() as conn:
        cur = conn.cursor()

        # Revenue trend by OrderDate
        cur.execute("""
            SELECT TOP (13)
                FORMAT(o.OrderDate, 'yyyy-MM') AS Period,
                COUNT(DISTINCT o.OrderID)       AS OrderCount,
                SUM(CASE
                    WHEN DATEDIFF(day, o.OrderDate, GETDATE()) < 31
                    THEN COALESCE(od_agg.TotalRevenue, 0)
                    ELSE COALESCE(i_agg.TotalInvoiced, 0)
                END) AS BlendedRevenue
            FROM Orders o
            LEFT JOIN (
                SELECT OrderID, SUM(Total) AS TotalRevenue
                FROM   OrderDetails GROUP BY OrderID
            ) od_agg ON od_agg.OrderID = o.OrderID
            LEFT JOIN (
                SELECT OrderID, SUM(InvoiceTotal) AS TotalInvoiced
                FROM   Invoices GROUP BY OrderID
            ) i_agg ON i_agg.OrderID = o.OrderID
            WHERE o.OrderDate >= DATEADD(day, -365, GETDATE())
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus <> 'Tentative'
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period DESC
        """)
        rev_rows = {r[0]: {"OrderCount": r[1], "BlendedRevenue": round(_f(r[2]), 2)}
                    for r in cur.fetchall()}

        # Invoiced totals grouped by OrderDate (same axis as revenue)
        cur.execute("""
            SELECT TOP (13)
                FORMAT(o.OrderDate, 'yyyy-MM') AS Period,
                SUM(i.EmbTotal)                AS TotalInvoiced
            FROM Invoices i
            JOIN Orders o ON o.OrderID = i.OrderID
            WHERE o.OrderDate >= DATEADD(day, -365, GETDATE())
              AND o.OrderStatus = 'Invoiced'
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period DESC
        """)
        inv_map = {r[0]: round(_f(r[1]), 2) for r in cur.fetchall()}

    periods = sorted(rev_rows.keys())
    return JSONResponse({
        "labels":   periods,
        "revenue":  [rev_rows[p]["BlendedRevenue"]   for p in periods],
        "invoiced": [inv_map.get(p, 0)               for p in periods],
        "orders":   [rev_rows[p]["OrderCount"]        for p in periods],
    })


def api_best_sellers(request: Request):
    raw = db.get_best_sellers(days=90, limit=10).get("best_sellers", [])
    return JSONResponse({
        "best_sellers": [
            {
                "ProdNo":       s.get("ProdNo", ""),
                "Brand":        s.get("Brand", ""),
                "Title":        s.get("Title", ""),
                "TotalQty":     _i(s.get("TotalQty")),
                "TotalRevenue": round(_f(s.get("TotalRevenue")), 2),
                "OrderCount":   _i(s.get("OrderCount")),
            }
            for s in raw
        ],
    })


def api_sales_breakdown(request: Request):
    data = db.get_sales_breakdown(days=90, limit=8)

    def clean(items: list, key: str) -> list:
        return [
            {
                key:            item.get(key, ""),
                "TotalQty":     _i(item.get("TotalQty")),
                "TotalRevenue": round(_f(item.get("TotalRevenue")), 2),
                "OrderCount":   _i(item.get("OrderCount")),
            }
            for item in items
        ]

    return JSONResponse({
        "by_category": clean(data.get("by_category", []), "Category"),
        "by_brand":    clean(data.get("by_brand",    []), "Brand"),
        "by_color":    clean(data.get("by_color",    []), "Color"),
    })


def api_recent_orders(request: Request):
    raw = db.list_orders(limit=15).get("orders", [])
    return JSONResponse({
        "orders": [
            {
                "OrderNo":       o.get("OrderNo", ""),
                "CustomerName":  o.get("CustomerName", ""),
                "OrderDate":     str(o.get("OrderDate", ""))[:10],
                "OrderStatus":   o.get("OrderStatus", ""),
                "InvoiceNumber": o.get("InvoiceNumber") or "—",
                "Rush":          bool(o.get("Rush")),
            }
            for o in raw
        ],
    })


def api_invoice_lag(request: Request):
    """Average days from OrderDate to InvoiceDate, overall and by month."""
    with db._conn() as conn:
        cur = conn.cursor()

        # Overall summary — last 365 days (avg / min / max / count)
        cur.execute("""
            SELECT
                AVG(CAST(DATEDIFF(day, o.OrderDate, i.InvoiceDate) AS float)) AS AvgDays,
                MIN(DATEDIFF(day, o.OrderDate, i.InvoiceDate))                 AS MinDays,
                MAX(DATEDIFF(day, o.OrderDate, i.InvoiceDate))                 AS MaxDays,
                COUNT(*)                                                        AS InvoiceCount
            FROM Invoices i
            JOIN Orders o ON i.OrderID = o.OrderID
            WHERE o.OrderStatus = 'Invoiced'
              AND o.OrderDate >= DATEADD(day, -365, GETDATE())
              AND DATEDIFF(day, o.OrderDate, i.InvoiceDate) >= 0
              AND o.OrderNo NOT LIKE 'T%'
        """)
        row = cur.fetchone()
        summary = {
            "avg_days":      round(_f(row[0]), 1) if row and row[0] else None,
            "min_days":      _i(row[1])            if row else None,
            "max_days":      _i(row[2])            if row else None,
            "invoice_count": _i(row[3])            if row else 0,
        }

        # Median — use ROW_NUMBER() to avoid PERCENTILE_CONT compatibility issue
        cur.execute("""
            SELECT AVG(CAST(days AS float))
            FROM (
                SELECT DATEDIFF(day, o.OrderDate, i.InvoiceDate) AS days,
                       ROW_NUMBER() OVER (ORDER BY DATEDIFF(day, o.OrderDate, i.InvoiceDate)) AS rn,
                       COUNT(*) OVER () AS cnt
                FROM Invoices i
                JOIN Orders o ON i.OrderID = o.OrderID
                WHERE o.OrderStatus = 'Invoiced'
                  AND o.OrderDate >= DATEADD(day, -365, GETDATE())
                  AND DATEDIFF(day, o.OrderDate, i.InvoiceDate) >= 0
                  AND o.OrderNo NOT LIKE 'T%'
            ) t
            WHERE rn IN ((cnt + 1) / 2, (cnt + 2) / 2)
        """)
        med = cur.fetchone()
        summary["median_days"] = round(_f(med[0]), 1) if med and med[0] else None

        # Monthly trend — avg lag per order month
        cur.execute("""
            SELECT
                FORMAT(o.OrderDate, 'yyyy-MM')                                           AS Period,
                AVG(CAST(DATEDIFF(day, o.OrderDate, i.InvoiceDate) AS float))            AS AvgDays,
                COUNT(*)                                                                  AS InvoiceCount
            FROM Invoices i
            JOIN Orders o ON i.OrderID = o.OrderID
            WHERE o.OrderStatus = 'Invoiced'
              AND o.OrderDate >= DATEADD(day, -365, GETDATE())
              AND DATEDIFF(day, o.OrderDate, i.InvoiceDate) >= 0
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period
        """)
        monthly = [
            {"period": r[0], "avg_days": round(_f(r[1]), 1), "count": _i(r[2])}
            for r in cur.fetchall()
        ]

    return JSONResponse({"summary": summary, "monthly": monthly})


def api_order_health(request: Request):
    """
    Orders that stalled: cancelled or not progressed to Invoiced within 30 days.
    Summary covers last 90 days; monthly trend covers last 13 months.
    """
    with db._conn() as conn:
        cur = conn.cursor()

        # ── 90-day summary ─────────────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*)                                                            AS Total,
                SUM(CASE WHEN o.OrderStatus LIKE '%cancel%' THEN 1 ELSE 0 END)     AS Cancelled,
                SUM(CASE
                        WHEN DATEDIFF(day, o.OrderDate, GETDATE()) > 31
                         AND o.OrderStatus NOT LIKE '%cancel%'
                         AND o.OrderStatus <> 'Invoiced'
                    THEN 1 ELSE 0 END)                                             AS Stalled,
                SUM(CASE WHEN o.OrderStatus = 'Invoiced' THEN 1 ELSE 0 END)        AS Invoiced
            FROM Orders o
            WHERE o.OrderDate >= DATEADD(day, -90, GETDATE())
              AND o.OrderNo NOT LIKE 'T%'
        """)
        row = cur.fetchone()
        summary = {
            "total":     _i(row[0]),
            "cancelled": _i(row[1]),
            "stalled":   _i(row[2]),
            "invoiced":  _i(row[3]),
        }

        # ── Monthly trend (last 13 months by OrderDate) ────────────────────────
        # "Stalled" = order is now > 30 days old and still not Invoiced/Cancelled.
        # We evaluate stall-status as of today, so older months naturally have
        # higher stall counts than the current month (which has many orders < 30d).
        cur.execute("""
            SELECT
                FORMAT(o.OrderDate, 'yyyy-MM')                                     AS Period,
                COUNT(*)                                                            AS Total,
                SUM(CASE WHEN o.OrderStatus LIKE '%cancel%' THEN 1 ELSE 0 END)     AS Cancelled,
                SUM(CASE
                        WHEN DATEDIFF(day, o.OrderDate, GETDATE()) > 31
                         AND o.OrderStatus NOT LIKE '%cancel%'
                         AND o.OrderStatus <> 'Invoiced'
                    THEN 1 ELSE 0 END)                                             AS Stalled,
                SUM(CASE WHEN o.OrderStatus = 'Invoiced' THEN 1 ELSE 0 END)        AS Invoiced
            FROM Orders o
            WHERE o.OrderDate >= DATEADD(day, -395, GETDATE())
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period
        """)
        monthly = [
            {
                "period":    r[0],
                "total":     _i(r[1]),
                "cancelled": _i(r[2]),
                "stalled":   _i(r[3]),
                "invoiced":  _i(r[4]),
            }
            for r in cur.fetchall()
        ]

    return JSONResponse({"summary": summary, "monthly": monthly})


def api_prodno_trend(request: Request):
    """Top products by quantity: last 12 months vs prior 12 months, with delta."""
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP (25)
                od.ProdNo,
                SUM(CASE
                    WHEN o.OrderDate >= DATEADD(month, -12, GETDATE())
                    THEN COALESCE(od.Quantity, 0)
                    ELSE 0
                END) AS Qty_Current,
                SUM(CASE
                    WHEN o.OrderDate < DATEADD(month, -12, GETDATE())
                     AND o.OrderDate >= DATEADD(month, -24, GETDATE())
                    THEN COALESCE(od.Quantity, 0)
                    ELSE 0
                END) AS Qty_Prior
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            WHERE o.OrderDate >= DATEADD(month, -24, GETDATE())
              AND o.OrderStatus NOT IN ('Cancelled', 'Cancel', 'Void')
              AND od.ProdNo IS NOT NULL
              AND od.ProdNo <> ''
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY od.ProdNo
            HAVING SUM(CASE
                WHEN o.OrderDate >= DATEADD(month, -12, GETDATE())
                THEN COALESCE(od.Quantity, 0)
                ELSE 0
            END) > 0
            ORDER BY Qty_Current DESC
        """)
        rows = cur.fetchall()

    items = []
    for row in rows:
        cur_qty = _i(row[1])
        prior_qty = _i(row[2])
        delta = cur_qty - prior_qty
        pct = round((delta / prior_qty * 100), 1) if prior_qty else None

        items.append({
            "prodno": row[0],
            "current": cur_qty,
            "prior": prior_qty,
            "delta": delta,
            "delta_pct": pct,
        })

    return JSONResponse({"items": items})


def api_product_month_yoy(request: Request):
    """Top products this month vs same month last year and two years ago (by $ and qty)."""
    now = datetime.now()

    # Last full calendar month
    lm_end_dt   = now.replace(day=1) - timedelta(days=1)
    lm_start_dt = lm_end_dt.replace(day=1)
    cy_start = lm_start_dt.strftime("%Y-%m-%d 00:00:00")
    cy_end   = lm_end_dt.strftime("%Y-%m-%d 23:59:59")

    def month_end(year, month):
        """Return a datetime at the last second of the given year/month."""
        if month == 12:
            return datetime(year + 1, 1, 1) - timedelta(days=1)
        return datetime(year, month + 1, 1) - timedelta(days=1)

    # Same full month last year
    ly1_year  = lm_start_dt.year - 1
    ly1_month = lm_start_dt.month
    ly1_start = datetime(ly1_year, ly1_month, 1).strftime("%Y-%m-%d 00:00:00")
    ly1_end   = month_end(ly1_year, ly1_month).strftime("%Y-%m-%d 23:59:59")

    # Same full month two years ago
    ly2_year  = lm_start_dt.year - 2
    ly2_month = lm_start_dt.month
    ly2_start = datetime(ly2_year, ly2_month, 1).strftime("%Y-%m-%d 00:00:00")
    ly2_end   = month_end(ly2_year, ly2_month).strftime("%Y-%m-%d 23:59:59")

    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP (15)
                od.ProdNo,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Total,    0) ELSE 0 END) AS Rev_CY,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Quantity, 0) ELSE 0 END) AS Qty_CY,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Total,    0) ELSE 0 END) AS Rev_LY1,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Quantity, 0) ELSE 0 END) AS Qty_LY1,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Total,    0) ELSE 0 END) AS Rev_LY2,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                         THEN COALESCE(od.Quantity, 0) ELSE 0 END) AS Qty_LY2
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            WHERE o.OrderDate >= ?
              AND o.OrderDate <= ?
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus <> 'Tentative'
              AND od.ProdNo IS NOT NULL
              AND od.ProdNo <> ''
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY od.ProdNo
            HAVING SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ?
                            THEN COALESCE(od.Total, 0) ELSE 0 END) > 0
            ORDER BY Rev_CY DESC
        """,
            cy_start,  cy_end,   # Rev_CY
            cy_start,  cy_end,   # Qty_CY
            ly1_start, ly1_end,  # Rev_LY1
            ly1_start, ly1_end,  # Qty_LY1
            ly2_start, ly2_end,  # Rev_LY2
            ly2_start, ly2_end,  # Qty_LY2
            ly2_start, cy_end,   # WHERE window (covers all three periods)
            cy_start,  cy_end,   # HAVING (must have revenue this month)
        )
        rows = cur.fetchall()

    def pct_chg(cur_val, prior_val):
        if prior_val and prior_val > 0:
            return round((cur_val - prior_val) / prior_val * 100, 1)
        return None

    items = []
    for r in rows:
        rev_cy  = round(_f(r[1]), 2)
        qty_cy  = _i(r[2])
        rev_ly1 = round(_f(r[3]), 2)
        qty_ly1 = _i(r[4])
        rev_ly2 = round(_f(r[5]), 2)
        qty_ly2 = _i(r[6])
        items.append({
            "prodno":      r[0],
            "rev_cy":      rev_cy,
            "qty_cy":      qty_cy,
            "rev_ly1":     rev_ly1,
            "qty_ly1":     qty_ly1,
            "rev_ly2":     rev_ly2,
            "qty_ly2":     qty_ly2,
            "rev_pct_ly1": pct_chg(rev_cy, rev_ly1),
            "qty_pct_ly1": pct_chg(qty_cy, qty_ly1),
            "rev_pct_ly2": pct_chg(rev_cy, rev_ly2),
            "qty_pct_ly2": pct_chg(qty_cy, qty_ly2),
        })

    return JSONResponse({
        "items":      items,
        "month_name": lm_start_dt.strftime("%B"),
        "year_cy":    lm_start_dt.year,
        "year_ly1":   ly1_year,
        "year_ly2":   ly2_year,
        "cy_range":   f"{cy_start[:10]} → {cy_end[:10]}",
        "ly1_range":  f"{ly1_start[:10]} → {ly1_end[:10]}",
        "ly2_range":  f"{ly2_start[:10]} → {ly2_end[:10]}",
    })


def api_r112_trend(request: Request):
    """Monthly $ and qty for product R112 — last 12 full months vs same month prior year."""
    now = datetime.now()

    # Build the 12 most-recent full calendar months (oldest → newest)
    cy_periods = []
    d = now.replace(day=1) - timedelta(days=1)   # last day of previous month
    for _ in range(12):
        cy_periods.append(d.strftime("%Y-%m"))
        d = d.replace(day=1) - timedelta(days=1)
    cy_periods.reverse()

    ly_periods = [f"{int(p[:4]) - 1}{p[4:]}" for p in cy_periods]

    oldest = min(ly_periods)   # 2 years back at most
    newest = max(cy_periods)

    # Convert period strings to proper date bounds for sargable WHERE
    oldest_start = oldest + "-01 00:00:00"
    # newest is like "2026-04"; find last day of that month
    ny, nm = int(newest[:4]), int(newest[5:])
    if nm == 12:
        newest_end = f"{ny+1}-01-01 00:00:00"
    else:
        newest_end = f"{ny}-{nm+1:02d}-01 00:00:00"
    # Use < newest_end rather than <= last-day to avoid computing last day
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                FORMAT(o.OrderDate, 'yyyy-MM')  AS Period,
                SUM(COALESCE(od.Total,    0))   AS Revenue,
                SUM(COALESCE(od.Quantity, 0))   AS Qty
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            WHERE od.ProdNo = 'R112'
              AND o.OrderDate >= ?
              AND o.OrderDate <  ?
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Tentative', 'Void')
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
        """, oldest_start, newest_end)
        raw = {r[0]: {"revenue": round(_f(r[1]), 2), "qty": _i(r[2])} for r in cur.fetchall()}

    def pct(cur_v, prior_v):
        return round((cur_v - prior_v) / prior_v * 100, 1) if prior_v else None

    months = []
    for cy, ly in zip(cy_periods, ly_periods):
        cy_d = raw.get(cy, {"revenue": 0, "qty": 0})
        ly_d = raw.get(ly, {"revenue": 0, "qty": 0})
        months.append({
            "period":        cy,
            "revenue":       cy_d["revenue"],
            "qty":           cy_d["qty"],
            "ly_revenue":    ly_d["revenue"],
            "ly_qty":        ly_d["qty"],
            "rev_delta_pct": pct(cy_d["revenue"], ly_d["revenue"]),
            "qty_delta_pct": pct(cy_d["qty"],     ly_d["qty"]),
        })

    return JSONResponse({"months": months, "prodno": "R112"})


def api_r112_detail(request: Request):
    """Deep-dive for R112: KPI summary, color breakdown, top customers, related Richardson SKUs, specials history."""
    now = datetime.now()

    # ── Use exact same 12 full-calendar-month windows as api_r112_trend ──────
    # CY: first of month-11-ago → last day of last full month
    cy_end_dt   = now.replace(day=1) - timedelta(days=1)          # e.g. Apr 30 2026
    cy_sm       = cy_end_dt.month - 11
    cy_sy       = cy_end_dt.year + (cy_sm - 1) // 12
    cy_sm       = ((cy_sm - 1) % 12) + 1
    cy_start_dt = datetime(cy_sy, cy_sm, 1)                       # e.g. May 1 2025

    # PY: same 12-month block shifted back exactly 1 year
    py_end_dt   = cy_start_dt - timedelta(days=1)                 # e.g. Apr 30 2025
    py_start_dt = datetime(cy_sy - 1, cy_sm, 1)                  # e.g. May 1 2024

    cy_start = cy_start_dt.strftime("%Y-%m-%d 00:00:00")
    cy_end   = cy_end_dt.strftime("%Y-%m-%d 23:59:59")
    py_start = py_start_dt.strftime("%Y-%m-%d 00:00:00")
    py_end   = py_end_dt.strftime("%Y-%m-%d 23:59:59")

    def pct(a, b): return round((a - b) / b * 100, 1) if b else None

    with db._conn() as conn:
        cur = conn.cursor()

        # ── KPI summary: last 12 full months vs prior 12 full months ─────────
        cur.execute("""
            SELECT
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Total    ELSE 0 END),
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Quantity ELSE 0 END),
                COUNT(DISTINCT CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN o.OrderID END),
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Total    ELSE 0 END),
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Quantity ELSE 0 END),
                COUNT(DISTINCT CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN o.OrderID END)
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            WHERE od.ProdNo = 'R112'
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Tentative','Void')
              AND o.OrderNo NOT LIKE 'T%'
        """,
            cy_start, cy_end,   # Rev CY
            cy_start, cy_end,   # Qty CY
            cy_start, cy_end,   # Orders CY
            py_start, py_end,   # Rev PY
            py_start, py_end,   # Qty PY
            py_start, py_end,   # Orders PY
        )
        r = cur.fetchone()
        rev_cy = round(_f(r[0]), 2); qty_cy = _i(r[1]); orders_cy = _i(r[2])
        rev_py = round(_f(r[3]), 2); qty_py = _i(r[4]); orders_py = _i(r[5])
        aov_cy = round(rev_cy / orders_cy, 2) if orders_cy else 0
        aov_py = round(rev_py / orders_py, 2) if orders_py else 0
        summary = {
            "rev_cy": rev_cy, "qty_cy": qty_cy, "orders_cy": orders_cy,
            "rev_py": rev_py, "qty_py": qty_py, "orders_py": orders_py,
            "aov_cy": aov_cy,
            "aov_py": aov_py,
            "rev_pct":    pct(rev_cy,    rev_py),
            "qty_pct":    pct(qty_cy,    qty_py),
            "orders_pct": pct(orders_cy, orders_py),
            "aov_pct":    pct(aov_cy,    aov_py),
            "cy_range": f"{cy_start[:10]} → {cy_end[:10]}",
            "py_range": f"{py_start[:10]} → {py_end[:10]}",
        }

        # ── Color breakdown — last 12 full months ─────────────────────────────
        cur.execute("""
            SELECT TOP 15
                ISNULL(od.Color, 'Unknown') AS Color,
                SUM(od.Quantity)            AS Qty,
                SUM(od.Total)               AS Revenue,
                COUNT(DISTINCT o.OrderID)   AS Orders
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            WHERE od.ProdNo = 'R112'
              AND o.OrderDate >= ? AND o.OrderDate <= ?
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Tentative','Void')
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY ISNULL(od.Color, 'Unknown')
            ORDER BY Qty DESC
        """, cy_start, cy_end)
        colors = [{"color": r[0], "qty": _i(r[1]), "revenue": round(_f(r[2]),2), "orders": _i(r[3])} for r in cur.fetchall()]

        # ── Top customers for R112 — last 12 full months ──────────────────────
        cur.execute("""
            SELECT TOP 10
                c.Organization              AS CustomerName,
                SUM(od.Total)               AS Revenue,
                SUM(od.Quantity)            AS Qty,
                COUNT(DISTINCT o.OrderID)   AS Orders
            FROM OrderDetails od
            JOIN Orders o    ON o.OrderID    = od.OrderID
            JOIN Customers c ON c.CustomerID = o.CustomerID
            WHERE od.ProdNo = 'R112'
              AND o.OrderDate >= ? AND o.OrderDate <= ?
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Tentative','Void')
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY c.Organization
            ORDER BY Revenue DESC
        """, cy_start, cy_end)
        top_customers = [{"name": r[0], "revenue": round(_f(r[1]),2), "qty": _i(r[2]), "orders": _i(r[3])} for r in cur.fetchall()]

        # ── Related Richardson SKUs — last 12 full months vs prior 12 ─────────
        cur.execute("""
            SELECT TOP 8
                od.ProdNo,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Total    ELSE 0 END) AS Rev_CY,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Quantity ELSE 0 END) AS Qty_CY,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Total    ELSE 0 END) AS Rev_PY,
                SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Quantity ELSE 0 END) AS Qty_PY
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            JOIN Products p ON p.ProdNo = od.ProdNo
            WHERE p.Brand = 'Richardson'
              AND od.ProdNo <> 'R112'
              AND o.OrderDate >= ? AND o.OrderDate <= ?
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Tentative','Void')
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY od.ProdNo
            HAVING SUM(CASE WHEN o.OrderDate >= ? AND o.OrderDate <= ? THEN od.Total ELSE 0 END) > 0
            ORDER BY Rev_CY DESC
        """,
            cy_start, cy_end,   # Rev CY
            cy_start, cy_end,   # Qty CY
            py_start, py_end,   # Rev PY
            py_start, py_end,   # Qty PY
            py_start, cy_end,   # WHERE full span
            cy_start, cy_end,   # HAVING
        )
        related = []
        for r in cur.fetchall():
            rc = round(_f(r[1]),2); qc = _i(r[2]); rp = round(_f(r[3]),2); qp = _i(r[4])
            related.append({"prodno": r[0], "rev_cy": rc, "qty_cy": qc,
                             "rev_py": rp, "qty_py": qp,
                             "rev_pct": pct(rc, rp), "qty_pct": pct(qc, qp)})

        # ── Specials history for R112 / Richardson — last 24 months ──────────
        cur.execute("""
            SELECT SpecialType, SpecialItem, SpecialDiscount, SpecialStartDate, SpecialEndDate
            FROM   Specials
            WHERE  (SpecialItem = 'R112' OR SpecialItem = 'Richardson'
                    OR SpecialItem LIKE 'R112%')
              AND  SpecialEndDate >= DATEADD(month, -24, GETDATE())
            ORDER  BY SpecialStartDate DESC
        """)
        specials_history = [
            {"type": r[0], "item": r[1], "discount": int(_f(r[2]) * 100),
             "start_date": str(r[3])[:10] if r[3] else None,
             "end_date":   str(r[4])[:10] if r[4] else None}
            for r in cur.fetchall()
        ]

    return JSONResponse({
        "summary": summary,
        "colors": colors,
        "top_customers": top_customers,
        "related_skus": related,
        "specials_history": specials_history,
    })


def api_r112_cancellations(request: Request):
    """Cancelled orders per month by ProdNo for R112 and top Richardson SKUs — last 12 full months."""
    now = datetime.now()
    cy_end_dt   = now.replace(day=1) - timedelta(days=1)
    cy_sm       = cy_end_dt.month - 11
    cy_sy       = cy_end_dt.year + (cy_sm - 1) // 12
    cy_sm       = ((cy_sm - 1) % 12) + 1
    cy_start_dt = datetime(cy_sy, cy_sm, 1)
    cy_start    = cy_start_dt.strftime("%Y-%m-%d 00:00:00")
    ny, nm      = cy_end_dt.year, cy_end_dt.month
    cy_end_exc  = f"{ny+1}-01-01 00:00:00" if nm == 12 else f"{ny}-{nm+1:02d}-01 00:00:00"

    with db._conn() as conn:
        cur = conn.cursor()

        # All cancelled order lines for Richardson brand, last 12 full months
        cur.execute("""
            SELECT
                od.ProdNo,
                FORMAT(o.OrderDate, 'yyyy-MM')  AS Month,
                COUNT(DISTINCT o.OrderID)        AS CancelledOrders,
                SUM(od.Quantity)                 AS CancelledQty,
                SUM(od.Total)                    AS CancelledRevenue
            FROM OrderDetails od
            JOIN Orders o ON o.OrderID = od.OrderID
            JOIN Products p ON p.ProdNo = od.ProdNo
            WHERE o.OrderDate >= ? AND o.OrderDate < ?
              AND o.OrderStatus LIKE '%cancel%'
              AND p.Brand = 'Richardson'
              AND od.ProdNo IS NOT NULL AND od.ProdNo <> ''
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY od.ProdNo, FORMAT(o.OrderDate, 'yyyy-MM')
        """, cy_start, cy_end_exc)
        rows = cur.fetchall()

    # Build full month list for x-axis
    months = []
    d = cy_end_dt
    for _ in range(12):
        months.append(d.strftime("%Y-%m"))
        d = d.replace(day=1) - timedelta(days=1)
    months.reverse()

    # Aggregate totals per ProdNo to find top products
    totals = {}
    detail = {}  # {prodno: {month: {orders, qty, revenue}}}
    for prodno, month, orders, qty, rev in rows:
        totals[prodno] = totals.get(prodno, 0) + _i(orders)
        if prodno not in detail:
            detail[prodno] = {}
        detail[prodno][month] = {
            "orders":  _i(orders),
            "qty":     _i(qty),
            "revenue": round(_f(rev), 2),
        }

    # Top 8 by total cancelled orders; always include R112 if present
    top_prodnos = sorted(totals, key=lambda k: totals[k], reverse=True)[:8]
    if "R112" in totals and "R112" not in top_prodnos:
        top_prodnos[-1] = "R112"

    # Build series: one entry per ProdNo with monthly breakdown
    series = []
    for prodno in top_prodnos:
        monthly = []
        for m in months:
            d = detail.get(prodno, {}).get(m, {"orders": 0, "qty": 0, "revenue": 0.0})
            monthly.append(d)
        series.append({
            "prodno":        prodno,
            "total_orders":  totals.get(prodno, 0),
            "monthly":       monthly,
        })

    return JSONResponse({"months": months, "series": series})


def api_debug(request: Request):
    """Diagnostic endpoint — compares revenue vs invoiced from multiple angles."""
    now         = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    today_end   = now.strftime("%Y-%m-%d 23:59:59")

    with db._conn() as conn:
        cur = conn.cursor()

        # 1. Revenue from OrderDetails for orders placed THIS month
        cur.execute("""
            SELECT COUNT(DISTINCT o.OrderID), SUM(od.Total)
            FROM Orders o
            JOIN OrderDetails od ON od.OrderID = o.OrderID
            WHERE o.OrderDate >= ? AND o.OrderDate <= ?
        """, month_start, today_end)
        r = cur.fetchone()
        od_revenue = {"order_count": _i(r[0]), "total_revenue": round(_f(r[1]), 2)}

        # 2. Invoices for orders placed THIS month (by OrderDate) — NOT fan-out safe yet
        cur.execute("""
            SELECT COUNT(DISTINCT o.OrderID),
                   SUM(i.EmbTotal), SUM(i.SalesTax), SUM(i.Shipping), SUM(i.InvoiceTotal)
            FROM Orders o
            JOIN Invoices i ON i.OrderID = o.OrderID
            WHERE o.OrderDate >= ? AND o.OrderDate <= ?
        """, month_start, today_end)
        r = cur.fetchone()
        inv_by_orderdate = {
            "invoiced_order_count": _i(r[0]),
            "emb_total":     round(_f(r[1]), 2),
            "sales_tax":     round(_f(r[2]), 2),
            "shipping":      round(_f(r[3]), 2),
            "invoice_total": round(_f(r[4]), 2),
        }

        # 3. Invoices by InvoiceDate this month
        cur.execute("""
            SELECT COUNT(*), SUM(EmbTotal), SUM(SalesTax), SUM(Shipping), SUM(InvoiceTotal)
            FROM Invoices
            WHERE InvoiceDate >= ? AND InvoiceDate <= ?
        """, month_start, today_end)
        r = cur.fetchone()
        inv_by_invoicedate = {
            "invoice_count": _i(r[0]),
            "emb_total":     round(_f(r[1]), 2),
            "sales_tax":     round(_f(r[2]), 2),
            "shipping":      round(_f(r[3]), 2),
            "invoice_total": round(_f(r[4]), 2),
        }

        # 4. Orders this month with NO invoice
        cur.execute("""
            SELECT COUNT(*)
            FROM Orders o
            WHERE o.OrderDate >= ? AND o.OrderDate <= ?
              AND NOT EXISTS (SELECT 1 FROM Invoices i WHERE i.OrderID = o.OrderID)
        """, month_start, today_end)
        uninvoiced_count = _i(cur.fetchone()[0])

        # 5. Sample invoices for this month's orders
        cur.execute("""
            SELECT TOP 8
                i.InvoiceNo, CONVERT(varchar, i.InvoiceDate, 23),
                i.EmbTotal, i.SalesTax, i.Shipping, i.InvoiceTotal,
                CONVERT(varchar, o.OrderDate, 23), o.OrderStatus
            FROM Invoices i JOIN Orders o ON i.OrderID = o.OrderID
            WHERE o.OrderDate >= ? AND o.OrderDate <= ?
            ORDER BY i.InvoiceDate DESC
        """, month_start, today_end)
        samples = [
            {
                "invoice_no": r[0], "invoice_date": r[1],
                "emb_total": round(_f(r[2]), 2), "sales_tax": round(_f(r[3]), 2),
                "shipping": round(_f(r[4]), 2), "invoice_total": round(_f(r[5]), 2),
                "order_date": r[6], "order_status": r[7],
            }
            for r in cur.fetchall()
        ]

    return JSONResponse({
        "period": f"{month_start[:10]} → {today_end[:10]}",
        "1_order_revenue_by_orderdate": od_revenue,
        "2_invoices_for_this_month_orders_by_orderdate": inv_by_orderdate,
        "3_invoices_dated_this_month_by_invoicedate": inv_by_invoicedate,
        "4_uninvoiced_orders_this_month": uninvoiced_count,
        "5_sample_invoices": samples,
        "note": "Compare (1) vs (2.emb_total) to see if EmbTotal≈Revenue. Compare (2) vs (3) to understand OrderDate vs InvoiceDate timing gap.",
    })


def api_specials(request: Request):
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT SpecialType, SpecialItem, SpecialDiscount, SpecialEndDate
            FROM   Specials
            WHERE  SpecialStartDate <= GETDATE()
              AND  SpecialEndDate   >= GETDATE()
            ORDER  BY SpecialEndDate ASC
        """)
        rows = cur.fetchall()
    return JSONResponse({
        "specials": [
            {
                "SpecialType":    r[0],
                "SpecialItem":    r[1],
                "DiscountPct":    int(_f(r[2]) * 100),
                "SpecialEndDate": str(r[3])[:10] if r[3] else None,
            }
            for r in rows
        ],
    })


def api_proof_times(request: Request):
    """
    Time from OrderDate to ProofDate and ProofApprovalDate.
    Source: OrderPerformance table (OrderDate, ProofDate, ProofApprovalDate).
    Returns summary stats (avg, median, min, max) and a 13-month trend.
    Excludes T-prefix (quote) orders.
    """
    try:
        with db._conn() as conn:
            cur = conn.cursor()
            # Discover actual column names in OrderPerformance
            cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='OrderPerformance' ORDER BY ORDINAL_POSITION")
            cols = [r[0] for r in cur.fetchall()]
            print(f"[proof-times] OrderPerformance columns: {cols}")

            # Build join condition based on available key column
            if "OrderID" in cols:
                join_clause = "JOIN Orders o ON o.OrderID = op.OrderID"
                filter_clause = "AND o.OrderNo NOT LIKE 'T%' AND o.OrderStatus NOT LIKE '%cancel%'"
            elif "OrderNo" in cols:
                join_clause = "JOIN Orders o ON o.OrderNo = op.OrderNo"
                filter_clause = "AND op.OrderNo NOT LIKE 'T%' AND o.OrderStatus NOT LIKE '%cancel%'"
            else:
                join_clause = ""
                filter_clause = ""

            # Discover the correct column names (case-insensitive match)
            col_map = {c.lower(): c for c in cols}
            order_date_col    = col_map.get("orderdate",         "OrderDate")
            proof_col         = col_map.get("proofdate",         "ProofDate")
            approval_col      = col_map.get("proofapprovaldate", "ProofApprovalDate")

            cur.execute(f"""
                SELECT
                    FORMAT(op.{order_date_col}, 'yyyy-MM')                               AS Period,
                    DATEDIFF(day, op.{order_date_col}, op.{proof_col})                   AS DaysToProof,
                    DATEDIFF(day, op.{order_date_col}, op.{approval_col})                AS DaysToApproval,
                    DATEDIFF(day, op.{proof_col},      op.{approval_col})                AS DaysProofToApproval
                FROM OrderPerformance op
                {join_clause}
                WHERE op.{order_date_col} >= DATEADD(month, -13, GETDATE())
                  {filter_clause}
                  AND op.{proof_col} IS NOT NULL
            """)
            rows = cur.fetchall()
    except Exception as e:
        import traceback
        print(f"[proof-times] ERROR: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e), "summary": {
            "to_proof":    {"avg": None, "median": None, "min": None, "max": None, "count": 0},
            "to_approval": {"avg": None, "median": None, "min": None, "max": None, "count": 0},
            "proof_to_approval": {"avg": None, "median": None, "min": None, "max": None, "count": 0},
        }, "monthly": []})

    from collections import defaultdict

    records = []
    for period, d_proof, d_approval, d_lag in rows:
        records.append({
            "period":      period,
            "to_proof":    d_proof    if d_proof    is not None and d_proof    >= 0 else None,
            "to_approval": d_approval if d_approval is not None and d_approval >= 0 else None,
            "lag":         d_lag      if d_lag      is not None and d_lag      >= 0 else None,
        })

    def _stats(values):
        vals = sorted(v for v in values if v is not None)
        if not vals:
            return {"avg": None, "median": None, "min": None, "max": None, "count": 0}
        n = len(vals)
        avg = round(sum(vals) / n, 1)
        mid = n // 2
        median = round((vals[mid - 1] + vals[mid]) / 2, 1) if n % 2 == 0 else float(vals[mid])
        return {"avg": avg, "median": median, "min": vals[0], "max": vals[-1], "count": n}

    summary = {
        "to_proof":    _stats([r["to_proof"]    for r in records]),
        "to_approval": _stats([r["to_approval"] for r in records]),
        "proof_to_approval": _stats([r["lag"]   for r in records]),
    }

    by_period = defaultdict(lambda: {"proof": [], "approval": [], "lag": []})
    for r in records:
        p = r["period"]
        if r["to_proof"]    is not None: by_period[p]["proof"].append(r["to_proof"])
        if r["to_approval"] is not None: by_period[p]["approval"].append(r["to_approval"])
        if r["lag"]         is not None: by_period[p]["lag"].append(r["lag"])

    def _avg(lst): return round(sum(lst) / len(lst), 1) if lst else None

    periods = sorted(by_period.keys())
    monthly = [
        {
            "period":          p,
            "avg_to_proof":    _avg(by_period[p]["proof"]),
            "avg_to_approval": _avg(by_period[p]["approval"]),
            "avg_lag":         _avg(by_period[p]["lag"]),
            "count":           len(by_period[p]["proof"]),
        }
        for p in periods
    ]

    return JSONResponse({"summary": summary, "monthly": monthly})


def api_proof_cancellation(request: Request):
    """
    Studies whether proof turnaround length (OrderDate → ProofDate) correlates
    with cancellation rate. Buckets orders by proof duration and shows cancel
    rate per bucket, plus a scatter of monthly avg proof days vs cancel rate.
    Excludes T-prefix (quote) orders.
    """
    try:
        with db._conn() as conn:
            cur = conn.cursor()

            # Discover column names
            cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='OrderPerformance' ORDER BY ORDINAL_POSITION")
            cols       = [r[0] for r in cur.fetchall()]
            col_map    = {c.lower(): c for c in cols}
            order_date_col = col_map.get("orderdate",  "OrderDate")
            proof_col      = col_map.get("proofdate",  "ProofDate")

            join_clause   = "JOIN Orders o ON o.OrderID = op.OrderID" if "OrderID" in cols else \
                            "JOIN Orders o ON o.OrderNo = op.OrderNo" if "OrderNo" in cols else ""
            no_t_filter   = "AND o.OrderNo NOT LIKE 'T%'" if join_clause else ""

            # ── Bucket analysis ───────────────────────────────────────────────
            # For every order that has a ProofDate, record proof duration and
            # whether the order was eventually cancelled.
            cur.execute(f"""
                SELECT
                    DATEDIFF(day, op.{order_date_col}, op.{proof_col})          AS ProofDays,
                    CASE WHEN o.OrderStatus LIKE '%cancel%' THEN 1 ELSE 0 END   AS IsCancelled
                FROM OrderPerformance op
                {join_clause}
                WHERE op.{proof_col} IS NOT NULL
                  AND op.{order_date_col} >= DATEADD(month, -24, GETDATE())
                  {no_t_filter}
                  AND DATEDIFF(day, op.{order_date_col}, op.{proof_col}) >= 0
            """)
            rows = cur.fetchall()

            # ── Monthly trend: avg proof days + cancel rate ───────────────────
            cur.execute(f"""
                SELECT
                    FORMAT(op.{order_date_col}, 'yyyy-MM')                      AS Period,
                    AVG(CAST(DATEDIFF(day, op.{order_date_col}, op.{proof_col}) AS float)) AS AvgProofDays,
                    COUNT(*)                                                     AS Total,
                    SUM(CASE WHEN o.OrderStatus LIKE '%cancel%' THEN 1 ELSE 0 END) AS Cancelled
                FROM OrderPerformance op
                {join_clause}
                WHERE op.{proof_col} IS NOT NULL
                  AND op.{order_date_col} >= DATEADD(month, -13, GETDATE())
                  {no_t_filter}
                  AND DATEDIFF(day, op.{order_date_col}, op.{proof_col}) >= 0
                GROUP BY FORMAT(op.{order_date_col}, 'yyyy-MM')
                ORDER BY Period
            """)
            monthly_rows = cur.fetchall()

            # ── Weekly trend: avg proof days + cancelled count ────────────────
            cur.execute(f"""
                SELECT
                    CONCAT(YEAR(op.{order_date_col}), '-W',
                           RIGHT('0' + CAST(DATEPART(iso_week, op.{order_date_col}) AS varchar), 2)) AS Week,
                    MIN(op.{order_date_col})                                                          AS WeekStart,
                    AVG(CAST(DATEDIFF(day, op.{order_date_col}, op.{proof_col}) AS float))           AS AvgProofDays,
                    COUNT(*)                                                                          AS Total,
                    SUM(CASE WHEN o.OrderStatus LIKE '%cancel%' THEN 1 ELSE 0 END)                   AS Cancelled
                FROM OrderPerformance op
                {join_clause}
                WHERE op.{proof_col} IS NOT NULL
                  AND op.{order_date_col} >= DATEADD(week, -26, GETDATE())
                  {no_t_filter}
                  AND DATEDIFF(day, op.{order_date_col}, op.{proof_col}) >= 0
                GROUP BY CONCAT(YEAR(op.{order_date_col}), '-W',
                                RIGHT('0' + CAST(DATEPART(iso_week, op.{order_date_col}) AS varchar), 2))
                ORDER BY MIN(op.{order_date_col})
            """)
            weekly_rows = cur.fetchall()

    except Exception as e:
        import traceback
        print(f"[proof-cancellation] ERROR: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e), "buckets": [], "monthly": [], "weekly": []})

    # ── Build buckets ─────────────────────────────────────────────────────────
    bucket_defs = [
        ("Same day",  0,  0),
        ("1 day",     1,  1),
        ("2–3 days",  2,  3),
        ("4–7 days",  4,  7),
        ("8–14 days", 8, 14),
        ("15+ days",  15, 9999),
    ]
    buckets = {label: {"total": 0, "cancelled": 0} for label, *_ in bucket_defs}

    for proof_days, is_cancelled in rows:
        if proof_days is None:
            continue
        for label, lo, hi in bucket_defs:
            if lo <= proof_days <= hi:
                buckets[label]["total"]     += 1
                buckets[label]["cancelled"] += is_cancelled
                break

    result_buckets = []
    for label, lo, hi in bucket_defs:
        b = buckets[label]
        total = b["total"]
        cancelled = b["cancelled"]
        result_buckets.append({
            "label":       label,
            "total":       total,
            "cancelled":   cancelled,
            "cancel_rate": round(cancelled / total * 100, 1) if total else 0,
        })

    monthly = []
    for period, avg_days, total, cancelled in monthly_rows:
        monthly.append({
            "period":      period,
            "avg_days":    round(float(avg_days), 1) if avg_days is not None else None,
            "total":       _i(total),
            "cancelled":   _i(cancelled),
            "cancel_rate": round(_i(cancelled) / _i(total) * 100, 1) if _i(total) else 0,
        })

    weekly = []
    for week, week_start, avg_days, total, cancelled in weekly_rows:
        weekly.append({
            "week":        week,
            "avg_days":    round(float(avg_days), 1) if avg_days is not None else None,
            "total":       _i(total),
            "cancelled":   _i(cancelled),
        })

    return JSONResponse({"buckets": result_buckets, "monthly": monthly, "weekly": weekly})


def api_rush_orders(request: Request):
    """Open orders with PlannedShipDate within 31 days of today."""
    try:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT TOP 200
                    o.OrderNo,
                    c.Organization                                               AS CustomerName,
                    CONVERT(varchar, o.OrderDate,                           23)  AS OrderDate,
                    CONVERT(varchar, TRY_CAST(o.PlannedShipDate AS DATE),   23)  AS PlannedShipDate,
                    CONVERT(varchar, TRY_CAST(o.InHandsDate     AS DATE),   23)  AS InHandsDate,
                    o.OrderStatus,
                    ISNULL(o.ItemStatus, '')                                      AS ItemStatus,
                    DATEDIFF(day, o.OrderDate,
                             TRY_CAST(o.PlannedShipDate AS DATE))                AS LeadDays,
                    DATEDIFF(day, CAST(GETDATE() AS DATE),
                             TRY_CAST(o.PlannedShipDate AS DATE))                AS DaysUntilShip
                FROM Orders o
                JOIN Customers c ON o.CustomerID = c.CustomerID
                WHERE TRY_CAST(o.PlannedShipDate AS DATE) IS NOT NULL
                  AND TRY_CAST(o.PlannedShipDate AS DATE) >= DATEADD(day, -90, CAST(GETDATE() AS DATE))
                  AND TRY_CAST(o.PlannedShipDate AS DATE) <= DATEADD(day, 31, CAST(GETDATE() AS DATE))
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus NOT IN ('Invoiced', 'Tentative')
                  AND o.OrderNo NOT LIKE 'T%'
                ORDER BY TRY_CAST(o.PlannedShipDate AS DATE) ASC, o.OrderNo ASC
            """)
            rows = cur.fetchall()

        orders = []
        summary = {"overdue": 0, "critical": 0, "soon": 0, "upcoming": 0, "no_date": 0}
        for r in rows:
            days_until = int(r[8]) if r[8] is not None else None
            if days_until is None:
                summary["no_date"] += 1
            elif days_until < 0:
                summary["overdue"] += 1
            elif days_until <= 2:
                summary["critical"] += 1
            elif days_until <= 6:
                summary["soon"] += 1
            else:
                summary["upcoming"] += 1
            orders.append({
                "OrderNo":        str(r[0]) if r[0] is not None else None,
                "CustomerName":   str(r[1]) if r[1] is not None else "",
                "OrderDate":      r[2],
                "PlannedShipDate": r[3],
                "InHandsDate":    r[4],
                "OrderStatus":    r[5],
                "ItemStatus":     r[6],
                "LeadDays":       int(r[7]) if r[7] is not None else None,
                "DaysUntilShip":  days_until,
            })
        summary["total"] = len(orders)
        return JSONResponse({"orders": orders, "summary": summary})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)


def api_top_customers(request: Request):
    """Top 10 customers by revenue — last 90 days."""
    now          = datetime.now()
    days90_start = (now - timedelta(days=90)).strftime("%Y-%m-%d 00:00:00")
    today_end    = now.strftime("%Y-%m-%d 23:59:59")

    def _top(date_from: str, date_to: str, limit: int = 10) -> list:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT TOP (?)
                    c.Organization            AS CustomerName,
                    COUNT(DISTINCT o.OrderID) AS OrderCount,
                    SUM(od_agg.TotalRevenue)  AS Revenue
                FROM Orders o
                JOIN Customers c ON o.CustomerID = c.CustomerID
                LEFT JOIN (
                    SELECT OrderID, SUM(Total) AS TotalRevenue
                    FROM   OrderDetails GROUP BY OrderID
                ) od_agg ON od_agg.OrderID = o.OrderID
                WHERE o.OrderDate >= ? AND o.OrderDate <= ?
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus <> 'Tentative'
                  AND o.OrderNo NOT LIKE 'T%'
                GROUP BY c.Organization
                ORDER BY Revenue DESC
                """,
                limit, date_from, date_to,
            )
            return [
                {
                    "CustomerName": r[0],
                    "OrderCount":   _i(r[1]),
                    "Revenue":      round(_f(r[2]), 2),
                }
                for r in cur.fetchall()
            ]

    return JSONResponse({
        "last90": _top(days90_start, today_end),
    })


def api_on_time_delivery(request: Request):
    """On-time delivery rate vs PlannedShipDate — 90-day summary + 12-month trend."""
    with db._conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT
                SUM(CASE WHEN DATEDIFF(day, o.PlannedShipDate, o.DateShipped) <= 0
                         THEN 1 ELSE 0 END)                                  AS OnTime,
                SUM(CASE WHEN DATEDIFF(day, o.PlannedShipDate, o.DateShipped)
                             BETWEEN 1 AND 3 THEN 1 ELSE 0 END)              AS Late1to3,
                SUM(CASE WHEN DATEDIFF(day, o.PlannedShipDate, o.DateShipped) > 3
                         THEN 1 ELSE 0 END)                                  AS Late4Plus,
                COUNT(*)                                                      AS Total
            FROM Orders o
            WHERE o.OrderStatus      = 'Invoiced'
              AND o.DateShipped      IS NOT NULL
              AND o.PlannedShipDate  IS NOT NULL
              AND o.OrderDate >= DATEADD(day, -90, GETDATE())
              AND o.OrderNo NOT LIKE 'T%'
        """)
        r = cur.fetchone()
        on_time = _i(r[0]); total = _i(r[3])
        summary = {
            "on_time":     on_time,
            "late_1_3":    _i(r[1]),
            "late_4_plus": _i(r[2]),
            "total":       total,
            "rate":        round(on_time / total * 100, 1) if total else 0,
        }

        cur.execute("""
            SELECT TOP 12
                FORMAT(o.OrderDate, 'yyyy-MM')                                   AS Period,
                COUNT(*)                                                          AS Total,
                SUM(CASE WHEN DATEDIFF(day, o.PlannedShipDate, o.DateShipped) <= 0
                         THEN 1 ELSE 0 END)                                      AS OnTime
            FROM Orders o
            WHERE o.OrderStatus     = 'Invoiced'
              AND o.DateShipped     IS NOT NULL
              AND o.PlannedShipDate IS NOT NULL
              AND o.OrderDate >= DATEADD(day, -365, GETDATE())
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period ASC
        """)
        monthly = []
        for row in cur.fetchall():
            t = _i(row[1]); ot = _i(row[2])
            monthly.append({
                "period":  row[0],
                "total":   t,
                "on_time": ot,
                "rate":    round(ot / t * 100, 1) if t else 0,
            })

    return JSONResponse({"summary": summary, "monthly": monthly})


def api_in_hands(request: Request):
    """Upcoming in-hands dates — bucketed counts + order list for next 30 days."""
    try:
        with db._conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT
                    SUM(CASE WHEN DATEDIFF(day, CAST(GETDATE() AS DATE),
                                               TRY_CAST(o.InHandsDate AS DATE)) < 0
                             THEN 1 ELSE 0 END)                             AS Overdue,
                    SUM(CASE WHEN DATEDIFF(day, CAST(GETDATE() AS DATE),
                                               TRY_CAST(o.InHandsDate AS DATE))
                                 BETWEEN 0 AND 7 THEN 1 ELSE 0 END)         AS Within7,
                    SUM(CASE WHEN DATEDIFF(day, CAST(GETDATE() AS DATE),
                                               TRY_CAST(o.InHandsDate AS DATE))
                                 BETWEEN 8 AND 14 THEN 1 ELSE 0 END)        AS Within14,
                    SUM(CASE WHEN DATEDIFF(day, CAST(GETDATE() AS DATE),
                                               TRY_CAST(o.InHandsDate AS DATE))
                                 BETWEEN 15 AND 30 THEN 1 ELSE 0 END)       AS Within30
                FROM Orders o
                WHERE TRY_CAST(o.InHandsDate AS DATE) IS NOT NULL
                  AND TRY_CAST(o.InHandsDate AS DATE) <= DATEADD(day, 30, GETDATE())
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus NOT IN ('Invoiced', 'Tentative')
                  AND o.OrderNo NOT LIKE 'T%'
            """)
            r = cur.fetchone()
            summary = {
                "overdue":   _i(r[0]),
                "within_7":  _i(r[1]),
                "within_14": _i(r[2]),
                "within_30": _i(r[3]),
            }

            cur.execute("""
                SELECT TOP 50
                    o.OrderNo,
                    c.Organization                                             AS CustomerName,
                    CONVERT(varchar, TRY_CAST(o.InHandsDate AS DATE), 23)     AS InHandsDate,
                    o.OrderStatus,
                    CASE WHEN ISNULL(o.Rush,'') NOT IN ('','None','False','false','0','No','no','N','n','NO') THEN 1 ELSE 0 END AS Rush,
                    DATEDIFF(day, CAST(GETDATE() AS DATE),
                                  TRY_CAST(o.InHandsDate AS DATE))            AS DaysOut
                FROM Orders o
                JOIN Customers c ON o.CustomerID = c.CustomerID
                WHERE TRY_CAST(o.InHandsDate AS DATE) IS NOT NULL
                  AND TRY_CAST(o.InHandsDate AS DATE) <= DATEADD(day, 30, GETDATE())
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus NOT IN ('Invoiced', 'Tentative')
                  AND o.OrderNo NOT LIKE 'T%'
                ORDER BY TRY_CAST(o.InHandsDate AS DATE) ASC
            """)
            orders = [
                {
                    "OrderNo":      r[0],
                    "CustomerName": r[1],
                    "InHandsDate":  r[2],
                    "OrderStatus":  r[3],
                    "Rush":         bool(r[4]),
                    "DaysOut":      _i(r[5]),
                }
                for r in cur.fetchall()
            ]

        return JSONResponse({"summary": summary, "orders": orders})
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)


MARKETING_ADVICE_URL = "https://customcustomersupport.com/webhook/get-marketing-advice"


def api_aov_trend(request: Request):
    """Monthly AOV — last 13 months."""
    try:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT TOP 13
                    FORMAT(o.OrderDate, 'yyyy-MM') AS Period,
                    SUM(COALESCE(od_agg.TotalRevenue, 0)) AS Revenue,
                    COUNT(DISTINCT o.OrderID) AS OrderCount
                FROM Orders o
                LEFT JOIN (
                    SELECT OrderID, SUM(Total) AS TotalRevenue
                    FROM OrderDetails GROUP BY OrderID
                ) od_agg ON od_agg.OrderID = o.OrderID
                WHERE o.OrderDate >= DATEADD(month, -13, GETDATE())
                  AND o.OrderStatus NOT LIKE '%cancel%'
                  AND o.OrderStatus <> 'Tentative'
                  AND o.OrderNo NOT LIKE 'T%'
                GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
                ORDER BY Period DESC
            """)
            rows = cur.fetchall()
        periods = sorted(r[0] for r in rows)
        rev_map = {r[0]: (_f(r[1]), _i(r[2])) for r in rows}
        trend = []
        for p in periods:
            rev, cnt = rev_map[p]
            trend.append({"period": p, "aov": round(rev / cnt, 2) if cnt else 0,
                          "revenue": round(rev, 2), "orders": cnt})
        return JSONResponse({"trend": trend})
    except Exception as e:
        import traceback
        print(f"[aov-trend] ERROR: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e), "trend": []})


def api_backlog(request: Request):
    """Total value of open (in-production) orders."""
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(DISTINCT o.OrderID),
                SUM(COALESCE(od_agg.TotalRevenue, 0))
            FROM Orders o
            LEFT JOIN (
                SELECT OrderID, SUM(Total) AS TotalRevenue
                FROM OrderDetails GROUP BY OrderID
            ) od_agg ON od_agg.OrderID = o.OrderID
            WHERE o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Invoiced', 'Tentative', 'Void')
              AND o.OrderNo NOT LIKE 'T%'
        """)
        row = cur.fetchone()
    return JSONResponse({"order_count": _i(row[0]), "backlog_value": round(_f(row[1]), 2)})


def api_ar_aging(request: Request):
    """Invoice age buckets — days since InvoiceDate for Invoiced-status orders."""
    try:
        with db._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    SUM(CASE WHEN DATEDIFF(day, i.InvoiceDate, GETDATE()) <= 30
                             THEN i.InvoiceTotal ELSE 0 END),
                    SUM(CASE WHEN DATEDIFF(day, i.InvoiceDate, GETDATE()) BETWEEN 31 AND 60
                             THEN i.InvoiceTotal ELSE 0 END),
                    SUM(CASE WHEN DATEDIFF(day, i.InvoiceDate, GETDATE()) BETWEEN 61 AND 90
                             THEN i.InvoiceTotal ELSE 0 END),
                    SUM(CASE WHEN DATEDIFF(day, i.InvoiceDate, GETDATE()) > 90
                             THEN i.InvoiceTotal ELSE 0 END),
                    COUNT(*), SUM(i.InvoiceTotal)
                FROM Invoices i
                JOIN Orders o ON o.OrderID = i.OrderID
                WHERE o.OrderStatus = 'Invoiced'
                  AND i.InvoiceDate >= DATEADD(day, -365, GETDATE())
            """)
            row = cur.fetchone()
            cur.execute("""
                SELECT TOP 10
                    i.InvoiceNo, c.Organization,
                    CONVERT(varchar, i.InvoiceDate, 23),
                    i.InvoiceTotal,
                    DATEDIFF(day, i.InvoiceDate, GETDATE())
                FROM Invoices i
                JOIN Orders o ON o.OrderID = i.OrderID
                JOIN Customers c ON c.CustomerID = o.CustomerID
                WHERE o.OrderStatus = 'Invoiced'
                  AND i.InvoiceDate >= DATEADD(day, -365, GETDATE())
                ORDER BY i.InvoiceDate ASC
            """)
            oldest = [{"invoice_no": r[0], "customer": r[1], "invoice_date": r[2],
                       "total": round(_f(r[3]), 2), "age_days": _i(r[4])} for r in cur.fetchall()]
        return JSONResponse({
            "buckets": {"0_30": round(_f(row[0]), 2), "31_60": round(_f(row[1]), 2),
                        "61_90": round(_f(row[2]), 2), "90_plus": round(_f(row[3]), 2)},
            "total_invoices": _i(row[4]), "total_value": round(_f(row[5]), 2),
            "oldest": oldest,
        })
    except Exception as e:
        import traceback
        print(f"[ar-aging] ERROR: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e), "buckets": {"0_30":0,"31_60":0,"61_90":0,"90_plus":0},
                             "total_invoices": 0, "total_value": 0, "oldest": []})


def api_customer_mix(request: Request):
    """New vs repeat customers by month — last 12 months."""
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                FORMAT(o.OrderDate, 'yyyy-MM') AS Period,
                COUNT(DISTINCT CASE
                    WHEN FORMAT(fo.FirstOrderDate, 'yyyy-MM') = FORMAT(o.OrderDate, 'yyyy-MM')
                    THEN o.CustomerID END) AS NewCustomers,
                COUNT(DISTINCT CASE
                    WHEN FORMAT(fo.FirstOrderDate, 'yyyy-MM') != FORMAT(o.OrderDate, 'yyyy-MM')
                    THEN o.CustomerID END) AS RepeatCustomers
            FROM Orders o
            JOIN (
                SELECT CustomerID, MIN(OrderDate) AS FirstOrderDate
                FROM Orders
                WHERE OrderStatus NOT LIKE '%cancel%'
                  AND OrderNo NOT LIKE 'T%'
                GROUP BY CustomerID
            ) fo ON fo.CustomerID = o.CustomerID
            WHERE o.OrderDate >= DATEADD(month, -12, GETDATE())
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus <> 'Tentative'
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY FORMAT(o.OrderDate, 'yyyy-MM')
            ORDER BY Period
        """)
        rows = cur.fetchall()
    periods = [r[0] for r in rows]
    return JSONResponse({
        "labels":  periods,
        "new":     [_i(r[1]) for r in rows],
        "repeat":  [_i(r[2]) for r in rows],
    })


def api_quotes(request: Request):
    """Quotes (OrderNo starting with T) — open count (last 90 days), MTD count and value."""
    now         = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%d 00:00:00")
    today_end   = now.strftime("%Y-%m-%d 23:59:59")
    days90_start = (now - timedelta(days=90)).strftime("%Y-%m-%d 00:00:00")

    with db._conn() as conn:
        cur = conn.cursor()

        # Open quotes (not cancelled/invoiced/void) — last 90 days
        cur.execute("""
            SELECT COUNT(DISTINCT o.OrderID)
            FROM Orders o
            WHERE o.OrderNo LIKE 'T%'
              AND o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderStatus NOT IN ('Invoiced', 'Void')
              AND o.OrderDate >= ?
        """, days90_start)
        open_count = _i(cur.fetchone()[0])

        # MTD quotes — count and total value
        cur.execute("""
            SELECT COUNT(DISTINCT o.OrderID),
                   SUM(COALESCE(od_agg.TotalRevenue, 0))
            FROM Orders o
            LEFT JOIN (
                SELECT OrderID, SUM(Total) AS TotalRevenue
                FROM   OrderDetails GROUP BY OrderID
            ) od_agg ON od_agg.OrderID = o.OrderID
            WHERE o.OrderNo LIKE 'T%'
              AND o.OrderDate >= ? AND o.OrderDate <= ?
              AND o.OrderStatus NOT LIKE '%cancel%'
        """, month_start, today_end)
        row = cur.fetchone()

    return JSONResponse({
        "open_count": open_count,
        "mtd_count":  _i(row[0]),
        "mtd_value":  round(_f(row[1]), 2),
    })


def api_churn_risk(request: Request):
    """Customers with 2+ orders in past 24 months who haven't ordered in 60+ days."""
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT TOP 25
                c.Organization,
                COUNT(DISTINCT o.OrderID),
                CONVERT(varchar, MAX(o.OrderDate), 23),
                DATEDIFF(day, MAX(o.OrderDate), GETDATE()),
                SUM(COALESCE(od_agg.TotalRevenue, 0))
            FROM Orders o
            JOIN Customers c ON c.CustomerID = o.CustomerID
            LEFT JOIN (
                SELECT OrderID, SUM(Total) AS TotalRevenue
                FROM OrderDetails GROUP BY OrderID
            ) od_agg ON od_agg.OrderID = o.OrderID
            WHERE o.OrderStatus NOT LIKE '%cancel%'
              AND o.OrderDate >= DATEADD(month, -24, GETDATE())
              AND o.OrderNo NOT LIKE 'T%'
            GROUP BY c.Organization, o.CustomerID
            HAVING MAX(o.OrderDate) < DATEADD(day, -60, GETDATE())
               AND COUNT(DISTINCT o.OrderID) >= 2
            ORDER BY SUM(COALESCE(od_agg.TotalRevenue, 0)) DESC
        """)
        rows = cur.fetchall()
    return JSONResponse({"customers": [
        {"name": r[0], "orders": _i(r[1]), "last_order": r[2],
         "days_since": _i(r[3]), "revenue": round(_f(r[4]), 2)}
        for r in rows
    ]})


def api_order_frequency(request: Request):
    """Distribution of orders-per-customer — last 12 months."""
    with db._conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                CASE
                    WHEN cnt = 1 THEN '1'
                    WHEN cnt = 2 THEN '2'
                    WHEN cnt = 3 THEN '3'
                    WHEN cnt = 4 THEN '4'
                    WHEN cnt = 5 THEN '5'
                    ELSE '6+'
                END AS Bucket,
                COUNT(*) AS CustomerCount,
                SUM(cnt) AS TotalOrders
            FROM (
                SELECT CustomerID, COUNT(DISTINCT OrderID) AS cnt
                FROM Orders
                WHERE OrderDate >= DATEADD(month, -12, GETDATE())
                  AND OrderStatus NOT LIKE '%cancel%'
                  AND OrderStatus <> 'Tentative'
                  AND OrderNo NOT LIKE 'T%'
                GROUP BY CustomerID
            ) sub
            GROUP BY
                CASE
                    WHEN cnt = 1 THEN '1'
                    WHEN cnt = 2 THEN '2'
                    WHEN cnt = 3 THEN '3'
                    WHEN cnt = 4 THEN '4'
                    WHEN cnt = 5 THEN '5'
                    ELSE '6+'
                END
        """)
        rows = cur.fetchall()
    order_map = {'1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6+': 6}
    items = sorted(
        [{"bucket": r[0], "customers": _i(r[1]), "orders": _i(r[2])} for r in rows],
        key=lambda x: order_map.get(x["bucket"], 9)
    )
    return JSONResponse({"distribution": items})

def _fetch_marketing_advice():
    """Blocking fetch — called in a thread executor to avoid blocking the event loop."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(MARKETING_ADVICE_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return json.loads(resp.read().decode())

def api_marketing_advice(request: Request):
    """Proxy to the marketing advice webhook and return the latest entry."""
    try:
        data = _fetch_marketing_advice()
        entry = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        return JSONResponse(entry)
    except Exception as e:
        import traceback
        print(f"[marketing-advice] error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=502)





# ── HTML (React via CDN) ───────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Embroidery Shop Dashboard</title>
<script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #edf0f6;
  --card:      #ffffff;
  --navy:      #0c1a2e;
  --blue:      #2563eb;
  --purple:    #7c3aed;
  --green:     #059669;
  --amber:     #d97706;
  --red:       #dc2626;
  --teal:      #0891b2;
  --text:      #0f172a;
  --muted:     #64748b;
  --border:    #e4e8ef;
  --shadow:    0 2px 12px rgba(15,23,42,.07), 0 1px 3px rgba(15,23,42,.04);
  --shadow-lg: 0 8px 28px rgba(15,23,42,.11), 0 2px 8px rgba(15,23,42,.05);
  --radius:    14px;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ── */
.header {
  background: linear-gradient(135deg, #0c1a2e 0%, #1a3458 100%);
  color: #fff;
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 14px;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 16px rgba(0,0,0,.28);
  border-bottom: 1px solid rgba(255,255,255,.07);
}
.header-logo { flex-shrink: 0; display: flex; align-items: center; }
.header-logo svg { filter: drop-shadow(0 2px 6px rgba(0,0,0,.4)); }
.header-divider { width: 1px; height: 28px; background: rgba(255,255,255,.14); margin: 0 2px; }
.header-title { font-size: 15px; font-weight: 700; letter-spacing: -.3px; }
.header-sub { font-size: 10px; color: #7ea4c8; margin-top: 1px; }
.header-spacer { flex: 1; }
.header-meta { text-align: right; font-size: 10px; color: #6d8fad; line-height: 1.6; }
.refresh-btn {
  background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.16);
  color: rgba(255,255,255,.92);
  padding: 6px 14px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  transition: background .15s, border-color .15s;
  margin-left: 10px;
}
.refresh-btn:hover { background: rgba(255,255,255,.16); border-color: rgba(255,255,255,.28); }
.refresh-btn:disabled { opacity: .38; cursor: default; }

/* ── Layout ── */
.main { padding: 18px 24px; max-width: 1600px; margin: 0 auto; }
.section-label {
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 1.4px;
  text-transform: uppercase;
  color: var(--muted);
  margin: 22px 0 10px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-label::after { content:''; flex:1; height:1px; background:var(--border); }

/* ── KPI Grid ── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
}
.kpi-card {
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 16px 18px 13px;
  position: relative;
  overflow: hidden;
  transition: transform .15s, box-shadow .15s;
}
.kpi-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lg); }
.kpi-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  border-radius: var(--radius) var(--radius) 0 0;
}
.kpi-card.blue::before   { background: linear-gradient(90deg, #2563eb, #60a5fa); }
.kpi-card.green::before  { background: linear-gradient(90deg, #059669, #34d399); }
.kpi-card.purple::before { background: linear-gradient(90deg, #7c3aed, #a78bfa); }
.kpi-card.amber::before  { background: linear-gradient(90deg, #d97706, #fcd34d); }
.kpi-card.teal::before   { background: linear-gradient(90deg, #0891b2, #38bdf8); }
.kpi-label { font-size: 9.5px; font-weight: 700; text-transform: uppercase;
             letter-spacing: .9px; color: var(--muted); margin-bottom: 8px; }
.kpi-value { font-size: 22px; font-weight: 800; color: var(--text); line-height: 1; letter-spacing: -.5px; }
.kpi-sub   { font-size: 11px; color: var(--muted); margin-top: 6px; line-height: 1.4; }
.kpi-yoy   { font-size: 10.5px; font-weight: 600; margin-top: 6px; }
.kpi-yoy-up   { color: var(--green); }
.kpi-yoy-down { color: var(--red); }
.kpi-yoy-flat { color: var(--muted); }
.kpi-yoy-label { font-weight: 400; color: var(--muted); }

/* ── Two-col ── */
.grid-2 { display: grid; gap: 12px; }
.grid-2.left-heavy  { grid-template-columns: 3fr 2fr; }
.grid-2.equal       { grid-template-columns: 1fr 1fr; }
.grid-3 { display: grid; gap: 12px; grid-template-columns: 1fr 1fr 1fr; }

/* ── Card ── */
.card {
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 18px 20px;
}
.card-title { font-size: 13px; font-weight: 700; margin-bottom: 2px; letter-spacing: -.2px; }
.card-sub   { font-size: 11px; color: var(--muted); margin-bottom: 14px; }
.chart-wrap { position: relative; }

/* ── Table ── */
.data-table { width: 100%; border-collapse: collapse; }
.data-table thead th {
  font-size: 9.5px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .7px; color: var(--muted);
  padding: 6px 10px; text-align: left;
  border-bottom: 1px solid var(--border);
}
.data-table thead th.r { text-align: right; }
.data-table tbody td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
  font-size: 12.5px;
}
.data-table tbody td.r { text-align: right; font-variant-numeric: tabular-nums; }
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table tbody tr:hover { background: #f9fafb; }
.prod-no { font-weight: 700; font-size: 12.5px; }
.prod-meta { font-size: 10.5px; color: var(--muted); margin-top: 1px; }

/* ── Stat chip (used in lag section) ── */
.stat-chip {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
}
.stat-chip-label { font-size: 9.5px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: .8px; color: var(--muted); margin-bottom: 4px; }
.stat-chip-value { font-size: 20px; font-weight: 800; color: var(--text); letter-spacing: -.4px; }

/* ── Badges ── */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 99px;
  font-size: 10.5px; font-weight: 600;
  white-space: nowrap; letter-spacing: .1px;
}
.badge-open     { background: #dbeafe; color: #1d4ed8; }
.badge-invoiced { background: #d1fae5; color: #065f46; }
.badge-shipped  { background: #ede9fe; color: #5b21b6; }
.badge-other    { background: #f1f5f9; color: #475569; }
.badge-rush     { background: #fee2e2; color: #b91c1c; margin-left: 5px; }

/* ── Promotions ── */
.promo-list { list-style: none; }
.promo-row {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 0; border-bottom: 1px solid var(--border);
}
.promo-row:last-child { border-bottom: none; }
.promo-pill {
  background: linear-gradient(135deg, #f59e0b, #fbbf24);
  color: #78350f;
  font-size: 10.5px; font-weight: 700;
  padding: 3px 9px; border-radius: 99px; white-space: nowrap;
  box-shadow: 0 1px 4px rgba(245,158,11,.3);
}
.promo-info { flex: 1; }
.promo-name { font-weight: 600; font-size: 12.5px; }
.promo-type { font-size: 10.5px; color: var(--muted); margin-top: 1px; }
.promo-end  { font-size: 10.5px; color: var(--muted); white-space: nowrap; }
.empty-msg  { color: var(--muted); font-size: 12px; padding: 10px 0; }

/* ── Spinner ── */
.spinner-wrap { display: flex; align-items: center; gap: 8px; color: var(--muted); padding: 14px 0; }
.spin {
  width: 16px; height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--blue);
  border-radius: 50%;
  animation: rotate .7s linear infinite;
}
@keyframes rotate { to { transform: rotate(360deg); } }

/* ── Error ── */
.error-msg { color: var(--red); font-size: 12px; padding: 10px 0; }

/* ── Source citation ── */
.card-source {
  font-size: 9px; color: #94a3b8; margin-top: 12px;
  padding-top: 8px; border-top: 1px solid var(--border);
  letter-spacing: .2px;
}

/* ── Churn risk heat ── */
.churn-high   { color: #dc2626; font-weight: 700; }
.churn-medium { color: #f97316; font-weight: 700; }
.churn-low    { color: #d97706; font-weight: 700; }

/* ── Tab Navigation ── */
.tab-nav {
  display: flex;
  gap: 4px;
  padding: 10px 24px 0;
  background: linear-gradient(135deg, #0c1a2e 0%, #1a3458 100%);
  border-bottom: 1px solid rgba(255,255,255,.08);
}
.tab-btn {
  padding: 8px 18px;
  border: none;
  background: rgba(255,255,255,.07);
  color: rgba(255,255,255,.65);
  font-size: 12px;
  font-weight: 600;
  border-radius: 8px 8px 0 0;
  cursor: pointer;
  transition: background .15s, color .15s;
  letter-spacing: .2px;
  border-bottom: 3px solid transparent;
}
.tab-btn:hover { background: rgba(255,255,255,.13); color: rgba(255,255,255,.9); }
.tab-btn.active {
  background: var(--bg);
  color: var(--text);
  border-bottom: 3px solid var(--blue);
}

/* ── R112 Tab specific ── */
.r112-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
.pct-up   { color: #059669; font-weight: 700; }
.pct-down { color: #dc2626; font-weight: 700; }
.pct-flat { color: #64748b; font-weight: 700; }
</style>
</head>
<body>
<div id="root"></div>

<script type="text/babel">
const { useState, useEffect, useRef, useCallback } = React;

// ── utils ─────────────────────────────────────────────────────────────────────
const fmt   = n => n == null ? '—' : '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtN  = n => n == null ? '—' : Number(n).toLocaleString('en-US');
const round1 = n => Math.round(n * 10) / 10;
const PALETTE = ['#2563eb','#7c3aed','#16a34a','#d97706','#dc2626','#0891b2','#be185d','#ea580c'];

function Spinner() {
  return <div className="spinner-wrap"><div className="spin" /><span>Loading…</span></div>;
}

function StatusBadge({ status }) {
  if (!status) return null;
  const cls = status === 'Open' ? 'badge-open'
            : status === 'Invoiced' ? 'badge-invoiced'
            : status === 'Shipped' ? 'badge-shipped'
            : 'badge-other';
  return <span className={`badge ${cls}`}>{status}</span>;
}

// ── KPI Card ──────────────────────────────────────────────────────────────────
function KPICard({ label, value, sub, color }) {
  return (
    <div className={`kpi-card ${color}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value ?? '—'}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

// ── YoY badge ─────────────────────────────────────────────────────────────────
function YoY({ current, prior }) {
  if (prior == null) return null;
  if (prior === 0 && current === 0) return null;
  if (prior === 0) return <div className="kpi-yoy kpi-yoy-up">▲ new <span className="kpi-yoy-label">vs last year</span></div>;
  const pct = ((current - prior) / prior) * 100;
  const dir = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
  const arrow = pct > 0 ? '▲' : pct < 0 ? '▼' : '–';
  const lyFmt = (typeof current === 'number' && current % 1 !== 0) ? fmt(prior) : fmtN(prior);
  return (
    <div className={`kpi-yoy kpi-yoy-${dir}`}>
      {arrow} {Math.abs(pct).toFixed(1)}%
      <span className="kpi-yoy-label"> vs {lyFmt} last yr</span>
    </div>
  );
}

// ── KPI Section ───────────────────────────────────────────────────────────────
function KPISection({ data, quotes }) {
  if (!data) return <div className="kpi-grid">{[...Array(6)].map((_, i) => <div key={i} className="kpi-card"><Spinner /></div>)}</div>;
  const m = data.month, y = data.ytd, t = data.today;
  return (
    <div className="kpi-grid">
      {/* Today */}
      <div className="kpi-card blue">
        <div className="kpi-label">Today's Orders</div>
        <div className="kpi-value">{fmtN(t.orders)}</div>
        <div className="kpi-sub">{fmt(t.revenue)} revenue</div>
        <YoY current={t.orders} prior={t.ly_orders} />
      </div>

      {/* Monthly Booked */}
      <div className="kpi-card green">
        <div className="kpi-label">Monthly Booked</div>
        <div className="kpi-value">{fmt(m.revenue)}</div>
        <div className="kpi-sub">{fmtN(m.orders)} orders placed</div>
        <YoY current={m.revenue} prior={m.ly_revenue} />
      </div>

      {/* Month Invoiced */}
      <div className="kpi-card purple">
        <div className="kpi-label">Month Invoiced</div>
        <div className="kpi-value">{fmt(m.invoiced)}</div>
        <div className="kpi-sub">
          {fmtN(m.invoice_count)} invoices sent · {fmtN(m.wip_orders)} orders still in progress
        </div>
        <YoY current={m.invoiced} prior={m.ly_invoiced} />
      </div>

      {/* YTD Revenue */}
      <div className="kpi-card amber">
        <div className="kpi-label">YTD Revenue</div>
        <div className="kpi-value">{fmt(y.revenue)}</div>
        <div className="kpi-sub">{fmt(y.invoiced)} invoiced · {fmtN(y.invoice_count)} invoices</div>
        <YoY current={y.revenue} prior={y.ly_revenue} />
      </div>

      {/* Active Orders */}
      <div className="kpi-card teal">
        <div className="kpi-label">Active Orders</div>
        <div className="kpi-value">{fmtN(data.open_orders)}</div>
        <div className="kpi-sub">{fmtN(data.tentative)} tentative · {fmtN(data.shipped_orders)} shipped</div>
      </div>

      {/* Open Quotes */}
      <div className="kpi-card" style={{borderTop:'3px solid #be185d'}}>
        <div className="kpi-label" style={{color:'#be185d'}}>Open Quotes</div>
        <div className="kpi-value">{quotes ? fmtN(quotes.open_count) : '—'}</div>
        <div className="kpi-sub">
          {quotes ? `${fmtN(quotes.mtd_count)} MTD · ${fmt(quotes.mtd_value)} value` : 'Loading…'}
        </div>
      </div>
    </div>
  );
}

// ── Invoice Lag ───────────────────────────────────────────────────────────────
function InvoiceLag({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current || !data.monthly.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const avg = data.summary.avg_days;
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: data.monthly.map(r => r.period),
        datasets: [
          { label: 'Avg Days', data: data.monthly.map(r => r.avg_days),
            borderColor: '#7c3aed', backgroundColor: 'rgba(124,58,237,.08)',
            tension: 0.35, fill: true, pointRadius: 4, pointBackgroundColor: '#7c3aed' },
          { label: '12-mo Avg', data: data.monthly.map(() => avg),
            borderColor: '#94a3b8', borderDash: [4,3], borderWidth: 1.5,
            pointRadius: 0, fill: false },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => v + 'd' } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  const s = data.summary;
  const stats = [
    { label: 'Avg',    value: s.avg_days    != null ? s.avg_days    + 'd' : '—' },
    { label: 'Median', value: s.median_days != null ? s.median_days + 'd' : '—' },
    { label: 'Min',    value: s.min_days    != null ? s.min_days    + 'd' : '—' },
    { label: 'Max',    value: s.max_days    != null ? s.max_days    + 'd' : '—' },
  ];
  return (
    <div className="card">
      <div className="card-title">⏱ Order → Invoice Lag</div>
      <div className="card-sub">Days from OrderDate to InvoiceDate · last 12 months · Invoiced-status orders only</div>
      <div style={{display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:8, marginBottom:14}}>
        {stats.map(st => (
          <div key={st.label} className="stat-chip">
            <div className="stat-chip-label">{st.label}</div>
            <div className="stat-chip-value">{st.value}</div>
          </div>
        ))}
      </div>
      <div className="chart-wrap" style={{height:160}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · Invoices JOIN Orders WHERE OrderStatus = 'Invoiced'</div>
    </div>
  );
}

// ── Order Pipeline ────────────────────────────────────────────────────────────
function OrderPipeline({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    const entries = Object.entries(data.all_statuses)
      .sort((a,b) => b[1]-a[1]);
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: entries.map(e => e[0]),
        datasets: [{
          data: entries.map(e => e[1]),
          backgroundColor: PALETTE,
          borderRadius: 5,
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { color: '#f1f5f9' } },
          y: { grid: { display: false } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  return (
    <div className="card">
      <div className="card-title">📋 Order Pipeline by Status</div>
      <div className="card-sub">Last 30 days · all statuses</div>
      <div className="chart-wrap" style={{height:200}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · Orders table · OrderDate ≥ today−30 days · COUNT(*) GROUP BY OrderStatus</div>
    </div>
  );
}

// ── Order Health ─────────────────────────────────────────────────────────────
function OrderHealth({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current || !data.monthly.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const rows = data.monthly;
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: rows.map(r => r.period),
        datasets: [
          { label: 'Invoiced',  data: rows.map(r => r.invoiced),  backgroundColor: 'rgba(5,150,105,.75)',   borderRadius: 3 },
          { label: 'Stalled',   data: rows.map(r => r.stalled),   backgroundColor: 'rgba(217,119,6,.75)',   borderRadius: 3 },
          { label: 'Cancelled', data: rows.map(r => r.cancelled), backgroundColor: 'rgba(220,38,38,.72)',   borderRadius: 3 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } },
          tooltip: { callbacks: {
            afterTitle: ctx => {
              const i = ctx[0].dataIndex;
              const r = rows[i];
              const pct = r.total > 0 ? (((r.stalled + r.cancelled) / r.total) * 100).toFixed(1) : '0.0';
              return `${pct}% did not progress to Invoiced`;
            }
          }},
        },
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, ticks: { stepSize: 20 } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  const s = data.summary;
  const atRisk = s.cancelled + s.stalled;
  const atRiskPct = s.total > 0 ? ((atRisk / s.total) * 100).toFixed(1) : '0.0';
  const chips = [
    { label: 'Cancelled',       value: fmtN(s.cancelled), color: '#dc2626' },
    { label: 'Stalled >30 days', value: fmtN(s.stalled),   color: '#d97706' },
    { label: 'Did Not Invoice',  value: fmtN(atRisk),       color: '#7c3aed' },
    { label: '% of Orders',      value: atRiskPct + '%',    color: '#0f172a' },
  ];
  return (
    <div className="card">
      <div className="card-title">⚠️ Order Health — Cancellations &amp; Stalled Orders</div>
      <div className="card-sub">
        Last 90 days · stalled = placed &gt;30 days ago and still not Invoiced or Cancelled
      </div>
      <div style={{display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:8, marginBottom:14}}>
        {chips.map(c => (
          <div key={c.label} className="stat-chip">
            <div className="stat-chip-label">{c.label}</div>
            <div className="stat-chip-value" style={{color: c.color}}>{c.value}</div>
          </div>
        ))}
      </div>
      <div className="chart-wrap" style={{height:180}}><canvas ref={canvasRef}/></div>
      <div className="card-source">
        Source: your_database · Orders table · last 13 months by OrderDate ·
        stalled = DATEDIFF(today, OrderDate) &gt; 30 AND status ∉ &#123;Invoiced, Cancelled&#125;
      </div>
    </div>
  );
}

// ── MTD vs Same Period Last Year ──────────────────────────────────────────────
function MTDComparison({ data }) {
  const canvasRef  = useRef(null);
  const chartRef   = useRef(null);
  const [chartErr, setChartErr] = React.useState(null);
  useEffect(() => {
    if (!data || !data.month || !canvasRef.current) return;
    if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; }
    const m = data.month;
    try {
      chartRef.current = new Chart(canvasRef.current, {
        type: 'bar',
        data: {
          labels: ['Orders', 'Revenue ($k)', 'Invoiced ($k)'],
          datasets: [
            { label: 'This Year', data: [m.orders, (m.revenue||0)/1000, (m.invoiced||0)/1000],
              backgroundColor: 'rgba(37,99,235,.82)', borderRadius: 5 },
            { label: 'Last Year', data: [m.ly_orders, (m.ly_revenue||0)/1000, (m.ly_invoiced||0)/1000],
              backgroundColor: 'rgba(37,99,235,.22)', borderRadius: 5 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } },
            tooltip: { callbacks: {
              label: ctx => {
                const v = ctx.raw;
                const lbl = ctx.dataset.label;
                const cat = ctx.label;
                if (cat === 'Orders') return ` ${lbl}: ${fmtN(Math.round(v))}`;
                return ` ${lbl}: $${(v||0).toFixed(1)}k`;
              }
            }},
          },
          scales: {
            x: { grid: { display: false } },
            y: { ticks: { callback: v => v } },
          },
        },
      });
      setChartErr(null);
    } catch(e) {
      console.error('[MTDComparison] Chart error:', e, 'data.month=', JSON.stringify(data.month));
      setChartErr(e.message);
    }
    return () => { if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; } };
  }, [data]);
  if (!data || !data.month) return <div className="card"><Spinner /></div>;
  const now = new Date();
  const m = data.month;
  return (
    <div className="card">
      <div className="card-title">📅 Month-to-Date vs Prior Year</div>
      <div className="card-sub">
        {now.toLocaleString('default',{month:'long'})} 1–{now.getDate()} {now.getFullYear()} vs same period {now.getFullYear()-1}
      </div>
      {chartErr ? (
        <table style={{width:'100%',borderCollapse:'collapse',fontSize:13,marginTop:8}}>
          <thead><tr>{['','Orders','Revenue','Invoiced'].map(h=><th key={h} style={{textAlign:'right',padding:'4px 8px',borderBottom:'1px solid #334155',color:'#94a3b8'}}>{h}</th>)}</tr></thead>
          <tbody>
            <tr><td style={{padding:'4px 8px',color:'#60a5fa'}}>This Year</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmtN(m.orders)}</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmt(m.revenue)}</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmt(m.invoiced)}</td></tr>
            <tr><td style={{padding:'4px 8px',color:'#94a3b8'}}>Last Year</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmtN(m.ly_orders)}</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmt(m.ly_revenue)}</td><td style={{textAlign:'right',padding:'4px 8px'}}>{fmt(m.ly_invoiced)}</td></tr>
          </tbody>
        </table>
      ) : (
        <div className="chart-wrap" style={{height:190}}><canvas ref={canvasRef}/></div>
      )}
      <div className="card-source">Source: your_database · Orders · blended revenue (OrderDetails.Total &lt;31d, InvoiceTotal ≥31d)</div>
    </div>
  );
}

// ── Revenue Trend Chart ───────────────────────────────────────────────────────
function RevenueTrendChart({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);

  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [
          { label: 'Revenue',  data: data.revenue,  backgroundColor: 'rgba(37,99,235,.8)',  borderRadius: 4 },
          { label: 'Invoiced', data: data.invoiced, backgroundColor: 'rgba(124,58,237,.65)', borderRadius: 4 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 12 }, boxWidth: 12 } } },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v) } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  return (
    <div className="card">
      <div className="card-title">📈 Revenue Trend</div>
      <div className="card-sub">Last 12 months by order date · revenue vs invoiced amount for same orders</div>
      {!data ? <Spinner /> : <div className="chart-wrap" style={{ height: 210 }}><canvas ref={canvasRef} /></div>}
      <div className="card-source">Source: your_database · Orders JOIN OrderDetails/Invoices · blended revenue by OrderDate · last 12 months · excludes cancelled</div>
    </div>
  );
}

// ── Best Sellers ──────────────────────────────────────────────────────────────
function BestSellers({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    const items = data.best_sellers.slice(0, 8);
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: items.map(s => s.ProdNo + (s.Brand ? ' · ' + s.Brand : '')),
        datasets: [{
          data: items.map(s => s.TotalRevenue),
          backgroundColor: PALETTE,
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: ctx => ' ' + fmt(ctx.raw) + '  (' + fmtN(items[ctx.dataIndex].TotalQty) + ' units)' } } },
        scales: {
          x: { ticks: { callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v) }, grid: { color: '#f1f5f9' } },
          y: { grid: { display: false } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  return (
    <div className="card">
      <div className="card-title">🏆 Best-Selling Products</div>
      <div className="card-sub">Revenue by product · last 90 days</div>
      {!data ? <Spinner /> : <div className="chart-wrap" style={{height:260}}><canvas ref={canvasRef}/></div>}
      <div className="card-source">Source: your_database · OrderDetails JOIN Orders · last 90 days by OrderDate</div>
    </div>
  );
}

// ── Category Doughnut ─────────────────────────────────────────────────────────
function CategoryChart({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);

  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    const cats = data.by_category.slice(0, 7);
    chartRef.current = new Chart(canvasRef.current, {
      type: 'doughnut',
      data: {
        labels: cats.map(c => c.Category || 'Other'),
        datasets: [{ data: cats.map(c => c.TotalRevenue), backgroundColor: PALETTE, borderWidth: 2 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: 'right', labels: { font: { size: 11 }, boxWidth: 12 } },
          tooltip: { callbacks: { label: ctx => ` ${fmt(ctx.raw)}` } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  return (
    <div className="card">
      <div className="card-title">📦 Sales by Category</div>
      <div className="card-sub">Last 90 days</div>
      {!data ? <Spinner /> : <div className="chart-wrap" style={{ height: 210 }}><canvas ref={canvasRef} /></div>}
      <div className="card-source">Source: your_database · OrderDetails JOIN Orders · SUM(Total) GROUP BY Category · last 90 days by OrderDate</div>
    </div>
  );
}

// ── Brand Table ───────────────────────────────────────────────────────────────
function BrandTable({ data }) {
  return (
    <div className="card">
      <div className="card-title">🏷️ Sales by Brand</div>
      <div className="card-sub">Top brands · last 90 days</div>
      {!data ? <Spinner /> : (
        <table className="data-table">
          <thead>
            <tr><th>Brand</th><th className="r">Revenue</th><th className="r">Qty</th></tr>
          </thead>
          <tbody>
            {data.by_brand.map(b => (
              <tr key={b.Brand}>
                <td><strong>{b.Brand || '—'}</strong></td>
                <td className="r">{fmt(b.TotalRevenue)}</td>
                <td className="r">{fmtN(b.TotalQty)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="card-source">Source: your_database · OrderDetails JOIN Orders · SUM(Total) GROUP BY Brand · last 90 days by OrderDate</div>
    </div>
  );
}

// ── Promotions ────────────────────────────────────────────────────────────────
function Promotions({ data }) {
  return (
    <div className="card">
      <div className="card-title">🎯 Active Promotions</div>
      <div className="card-sub">Current specials &amp; discounts</div>
      {!data ? <Spinner /> : (
        data.specials.length === 0
          ? <p className="empty-msg">No active promotions right now.</p>
          : <ul className="promo-list">
              {data.specials.map((s, i) => (
                <li key={i} className="promo-row">
                  <span className="promo-pill">{s.DiscountPct}% OFF</span>
                  <div className="promo-info">
                    <div className="promo-name">{s.SpecialItem}</div>
                    <div className="promo-type">{s.SpecialType}-level discount</div>
                  </div>
                  <div className="promo-end">Ends {s.SpecialEndDate || '—'}</div>
                </li>
              ))}
            </ul>
      )}
      <div className="card-source">Source: your_database · Specials table · WHERE SpecialStartDate ≤ today ≤ SpecialEndDate</div>
    </div>
  );
}

// ── Recent Orders ─────────────────────────────────────────────────────────────
function RecentOrders({ data }) {
  return (
    <div className="card">
      <div className="card-title">🕒 Recent Orders</div>
      <div className="card-sub">Latest 15 orders</div>
      {!data ? <Spinner /> : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Order #</th>
              <th>Customer</th>
              <th>Date</th>
              <th>Status</th>
              <th className="r">Invoice</th>
            </tr>
          </thead>
          <tbody>
            {data.orders.map(o => (
              <tr key={o.OrderNo}>
                <td>
                  <strong>{o.OrderNo}</strong>
                  {o.Rush && <span className="badge badge-rush">RUSH</span>}
                </td>
                <td>{o.CustomerName || '—'}</td>
                <td>{o.OrderDate || '—'}</td>
                <td><StatusBadge status={o.OrderStatus} /></td>
                <td className="r">{o.InvoiceNumber || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="card-source">Source: your_database · Orders JOIN Customers/Invoices · ORDER BY OrderDate DESC · TOP 15</div>
    </div>
  );
}

// ── ProdNo Trend ─────────────────────────────────────────────────────────────
function ProdnoTrend({ data }) {
  if (!data) return <div className="card"><Spinner /></div>;
  const items = data.items || [];
  return (
    <div className="card">
      <div className="card-title">📦 Product Volume — Last 12 Months vs Prior 12 Months</div>
      <div className="card-sub">Top 25 products by quantity · ranked by current period · excludes Cancelled &amp; Void</div>
      <div style={{overflowX:'auto'}}>
        <table style={{width:'100%', borderCollapse:'collapse', fontSize:13}}>
          <thead>
            <tr style={{background:'#f8fafc', borderBottom:'2px solid #e2e8f0'}}>
              <th style={{textAlign:'left',  padding:'7px 10px', fontWeight:600}}>ProdNo</th>
              <th style={{textAlign:'right', padding:'7px 10px', fontWeight:600}}>Last 12 Mo</th>
              <th style={{textAlign:'right', padding:'7px 10px', fontWeight:600}}>Prior 12 Mo</th>
              <th style={{textAlign:'right', padding:'7px 10px', fontWeight:600}}>Δ Qty</th>
              <th style={{textAlign:'right', padding:'7px 10px', fontWeight:600}}>Δ %</th>
              <th style={{textAlign:'left',  padding:'7px 10px', fontWeight:600}}>Trend</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r, i) => {
              const up   = r.delta > 0;
              const flat = r.delta === 0;
              const clr  = flat ? '#64748b' : up ? '#059669' : '#dc2626';
              const bar  = r.prior > 0
                ? Math.min(100, Math.abs(r.delta_pct ?? 0))
                : 100;
              return (
                <tr key={r.prodno} style={{borderBottom:'1px solid #f1f5f9', background: i%2===0?'#fff':'#fafafa'}}>
                  <td style={{padding:'6px 10px', fontWeight:600, fontFamily:'monospace'}}>{r.prodno}</td>
                  <td style={{padding:'6px 10px', textAlign:'right'}}>{fmtN(r.current)}</td>
                  <td style={{padding:'6px 10px', textAlign:'right', color:'#64748b'}}>{fmtN(r.prior)}</td>
                  <td style={{padding:'6px 10px', textAlign:'right', color:clr, fontWeight:600}}>
                    {r.delta > 0 ? '+' : ''}{fmtN(r.delta)}
                  </td>
                  <td style={{padding:'6px 10px', textAlign:'right', color:clr}}>
                    {r.delta_pct != null ? (r.delta_pct > 0 ? '+' : '') + r.delta_pct + '%' : '—'}
                  </td>
                  <td style={{padding:'6px 10px'}}>
                    <div style={{display:'flex', alignItems:'center', gap:4}}>
                      <div style={{
                        height:8, borderRadius:4,
                        width: bar + '%', maxWidth:80,
                        background: clr, opacity:0.75,
                        minWidth: flat ? 2 : 4,
                      }}/>
                      <span style={{fontSize:11, color:clr}}>
                        {flat ? '=' : up ? '▲' : '▼'}
                      </span>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="card-source">
        Source: your_database · OrderDetails JOIN Orders · SUM(Qty) GROUP BY ProdNo ·
        current = last 12 months · prior = months 13–24 ago
      </div>
    </div>
  );
}

// ── Proof Times ──────────────────────────────────────────────────────────────
function ProofTimes({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);

  useEffect(() => {
    if (!data || !canvasRef.current || !data.monthly.length) return;
    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: data.monthly.map(r => r.period),
        datasets: [
          {
            label: 'Days to Proof',
            data: data.monthly.map(r => r.avg_to_proof),
            borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.08)',
            tension: 0.35, fill: true, pointRadius: 3, pointBackgroundColor: '#2563eb',
          },
          {
            label: 'Days to Approval',
            data: data.monthly.map(r => r.avg_to_approval),
            borderColor: '#059669', backgroundColor: 'rgba(5,150,105,.06)',
            tension: 0.35, fill: true, pointRadius: 3, pointBackgroundColor: '#059669',
          },
          {
            label: 'Sent → Approved Lag',
            data: data.monthly.map(r => r.avg_lag),
            borderColor: '#d97706', borderDash: [4,3], borderWidth: 1.5,
            tension: 0.35, fill: false, pointRadius: 3, pointBackgroundColor: '#d97706',
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => v + 'd' }, beginAtZero: true },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return <div className="card"><div className="card-title">🖨️ Proof Turnaround Times</div><div className="error-msg">{data.error}</div></div>;

  const s = data.summary;
  const chips = [
    { label: 'Avg to Proof',          value: s.to_proof.avg           != null ? s.to_proof.avg           + 'd' : '—', color: '#2563eb' },
    { label: 'Median to Proof',       value: s.to_proof.median        != null ? s.to_proof.median        + 'd' : '—', color: '#2563eb' },
    { label: 'Avg to Approval',       value: s.to_approval.avg        != null ? s.to_approval.avg        + 'd' : '—', color: '#059669' },
    { label: 'Median to Approval',    value: s.to_approval.median     != null ? s.to_approval.median     + 'd' : '—', color: '#059669' },
    { label: 'Avg Proof→Approval',    value: s.proof_to_approval.avg  != null ? s.proof_to_approval.avg  + 'd' : '—', color: '#d97706' },
    { label: 'Median Proof→Approval', value: s.proof_to_approval.median != null ? s.proof_to_approval.median + 'd' : '—', color: '#d97706' },
  ];

  return (
    <div className="card">
      <div className="card-title">🖨️ Proof Turnaround Times</div>
      <div className="card-sub">Days from OrderDate to Proof / Proof Approval · last 13 months</div>
      <div style={{display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:8, marginBottom:14}}>
        {chips.map(c => (
          <div key={c.label} className="stat-chip">
            <div className="stat-chip-val" style={{color: c.color}}>{c.value}</div>
            <div className="stat-chip-label">{c.label}</div>
          </div>
        ))}
      </div>
      <div style={{height:180}}>
        <canvas ref={canvasRef} />
      </div>
      <div style={{display:'flex', gap:16, marginTop:10, fontSize:11, color:'var(--muted)'}}>
        <span>Proof: {s.to_proof.count} orders · min {s.to_proof.min ?? '—'}d / max {s.to_proof.max ?? '—'}d</span>
        <span>Approval: {s.to_approval.count} orders · min {s.to_approval.min ?? '—'}d / max {s.to_approval.max ?? '—'}d</span>
      </div>
    </div>
  );
}

// ── Proof → Cancellation ─────────────────────────────────────────────────────
function ProofCancellation({ data }) {
  const barRef  = useRef(null);
  const barChart = useRef(null);
  const lineRef  = useRef(null);
  const lineChart = useRef(null);

  useEffect(() => {
    if (!data || !barRef.current || !data.buckets.length) return;
    if (barChart.current) barChart.current.destroy();
    barChart.current = new Chart(barRef.current, {
      type: 'bar',
      data: {
        labels: data.buckets.map(b => b.label),
        datasets: [
          {
            label: 'Cancel Rate %',
            data: data.buckets.map(b => b.cancel_rate),
            backgroundColor: data.buckets.map(b =>
              b.cancel_rate > 20 ? 'rgba(220,38,38,.75)' :
              b.cancel_rate > 10 ? 'rgba(217,119,6,.75)' :
                                   'rgba(37,99,235,.65)'
            ),
            borderRadius: 5,
            yAxisID: 'y',
          },
          {
            label: 'Total Orders',
            data: data.buckets.map(b => b.total),
            type: 'line',
            borderColor: '#94a3b8', borderDash: [4,3], borderWidth: 1.5,
            pointRadius: 3, fill: false, yAxisID: 'y2',
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { grid: { display: false } },
          y:  { title: { display: true, text: 'Cancel %', font: { size: 10 } },
                ticks: { callback: v => v + '%' }, beginAtZero: true },
          y2: { position: 'right', title: { display: true, text: 'Orders', font: { size: 10 } },
                grid: { display: false }, beginAtZero: true },
        },
      },
    });
    return () => { if (barChart.current) barChart.current.destroy(); };
  }, [data]);

  useEffect(() => {
    if (!data || !lineRef.current || !data.monthly.length) return;
    if (lineChart.current) lineChart.current.destroy();
    lineChart.current = new Chart(lineRef.current, {
      type: 'line',
      data: {
        labels: data.monthly.map(r => r.period),
        datasets: [
          {
            label: 'Avg Proof Days',
            data: data.monthly.map(r => r.avg_days),
            borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.07)',
            tension: 0.35, fill: true, pointRadius: 3, yAxisID: 'y',
          },
          {
            label: 'Cancel Rate %',
            data: data.monthly.map(r => r.cancel_rate),
            borderColor: '#dc2626', backgroundColor: 'rgba(220,38,38,.06)',
            tension: 0.35, fill: true, pointRadius: 3, yAxisID: 'y2',
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { grid: { display: false } },
          y:  { title: { display: true, text: 'Proof Days', font: { size: 10 } }, beginAtZero: true },
          y2: { position: 'right', title: { display: true, text: 'Cancel %', font: { size: 10 } },
                ticks: { callback: v => v + '%' }, grid: { display: false }, beginAtZero: true },
        },
      },
    });
    return () => { if (lineChart.current) lineChart.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return <div className="card"><div className="card-title">📉 Proof Days vs Cancellations</div><div className="error-msg">{data.error}</div></div>;

  const total   = data.buckets.reduce((s, b) => s + b.total, 0);
  const overall = total ? round1(data.buckets.reduce((s, b) => s + b.cancelled, 0) / total * 100) : 0;

  return (
    <div className="card">
      <div className="card-title">📉 Proof Days vs Cancellation Rate</div>
      <div className="card-sub">Does a longer proof turnaround lead to more cancellations? · last 24 months · {fmtN(total)} orders</div>
      <div style={{display:'flex', gap:6, marginBottom:12, flexWrap:'wrap'}}>
        {data.buckets.map(b => (
          <div key={b.label} style={{
            background: b.cancel_rate > 20 ? '#fee2e2' : b.cancel_rate > 10 ? '#fef3c7' : '#eff6ff',
            border: `1px solid ${b.cancel_rate > 20 ? '#fca5a5' : b.cancel_rate > 10 ? '#fde68a' : '#bfdbfe'}`,
            borderRadius: 8, padding: '6px 12px', fontSize: 12, textAlign: 'center', minWidth: 80,
          }}>
            <div style={{fontWeight:700, fontSize:15, color: b.cancel_rate > 20 ? '#b91c1c' : b.cancel_rate > 10 ? '#92400e' : '#1d4ed8'}}>
              {b.cancel_rate}%
            </div>
            <div style={{color:'#475569', fontSize:11}}>{b.label}</div>
            <div style={{color:'#94a3b8', fontSize:10}}>{fmtN(b.total)} orders</div>
          </div>
        ))}
        <div style={{background:'#f1f5f9', border:'1px solid #e2e8f0', borderRadius:8, padding:'6px 12px', fontSize:12, textAlign:'center', minWidth:80}}>
          <div style={{fontWeight:700, fontSize:15, color:'#334155'}}>{overall}%</div>
          <div style={{color:'#475569', fontSize:11}}>Overall</div>
          <div style={{color:'#94a3b8', fontSize:10}}>{fmtN(total)} orders</div>
        </div>
      </div>
      <div className="grid-2 equal" style={{gap:12}}>
        <div>
          <div style={{fontSize:11, fontWeight:600, color:'var(--muted)', marginBottom:6, textTransform:'uppercase', letterSpacing:'.5px'}}>Cancel Rate by Proof Duration</div>
          <div style={{height:160}}><canvas ref={barRef} /></div>
        </div>
        <div>
          <div style={{fontSize:11, fontWeight:600, color:'var(--muted)', marginBottom:6, textTransform:'uppercase', letterSpacing:'.5px'}}>Monthly: Avg Proof Days vs Cancel Rate</div>
          <div style={{height:160}}><canvas ref={lineRef} /></div>
        </div>
      </div>
    </div>
  );
}

// ── Proof Weekly ─────────────────────────────────────────────────────────────
function ProofWeekly({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);

  useEffect(() => {
    if (!data || !canvasRef.current || !data.weekly || !data.weekly.length) return;
    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: data.weekly.map(r => r.week),
        datasets: [
          {
            label: 'Cancelled Orders',
            data: data.weekly.map(r => r.cancelled),
            backgroundColor: 'rgba(220,38,38,.65)',
            borderRadius: 3,
            yAxisID: 'y2',
            order: 2,
          },
          {
            label: 'Avg Proof Days',
            data: data.weekly.map(r => r.avg_days),
            type: 'line',
            borderColor: '#2563eb',
            backgroundColor: 'rgba(37,99,235,.08)',
            tension: 0.35,
            fill: true,
            pointRadius: 3,
            pointBackgroundColor: '#2563eb',
            borderWidth: 2,
            yAxisID: 'y',
            order: 1,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } },
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const idx = items[0]?.dataIndex;
                const row = data.weekly[idx];
                return row ? [`Total orders: ${row.total}`] : [];
              }
            }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 13, maxRotation: 45 } },
          y:  { title: { display: true, text: 'Avg Proof Days', font: { size: 10 } },
                beginAtZero: true, position: 'left' },
          y2: { title: { display: true, text: 'Cancelled', font: { size: 10 } },
                beginAtZero: true, position: 'right', grid: { display: false } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return <div className="card"><div className="card-title">📅 Weekly: Proof Days vs Cancellations</div><div className="error-msg">{data.error}</div></div>;
  if (!data.weekly || !data.weekly.length) return <div className="card"><div className="card-title">📅 Weekly: Proof Days vs Cancellations</div><div className="empty-msg">No data</div></div>;

  const totalCancelled = data.weekly.reduce((s, r) => s + r.cancelled, 0);
  const totalOrders    = data.weekly.reduce((s, r) => s + r.total,     0);
  const avgProofDays   = data.weekly.length
    ? round1(data.weekly.reduce((s, r) => s + (r.avg_days || 0), 0) / data.weekly.length)
    : null;

  return (
    <div className="card">
      <div className="card-title">📅 Weekly: Avg Proof Days vs Cancelled Orders</div>
      <div className="card-sub">Last 26 weeks · bars = cancellations · line = avg proof days</div>
      <div style={{display:'flex', gap:20, marginBottom:12, fontSize:12}}>
        <div className="stat-chip">
          <div className="stat-chip-val" style={{color:'#2563eb'}}>{avgProofDays != null ? avgProofDays + 'd' : '—'}</div>
          <div className="stat-chip-label">Avg Proof Days (period)</div>
        </div>
        <div className="stat-chip">
          <div className="stat-chip-val" style={{color:'#dc2626'}}>{fmtN(totalCancelled)}</div>
          <div className="stat-chip-label">Total Cancelled</div>
        </div>
        <div className="stat-chip">
          <div className="stat-chip-val" style={{color:'#475569'}}>{totalOrders ? round1(totalCancelled / totalOrders * 100) + '%' : '—'}</div>
          <div className="stat-chip-label">Overall Cancel Rate</div>
        </div>
      </div>
      <div style={{height:220}}>
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}

// ── Rush Orders ──────────────────────────────────────────────────────────────
function RushOrders({ data }) {
  const barRef  = useRef(null);
  const barChart = useRef(null);
  useEffect(() => {
    if (!data || !barRef.current) return;
    if (barChart.current) barChart.current.destroy();
    const s = data.summary;
    barChart.current = new Chart(barRef.current, {
      type: 'bar',
      data: {
        labels: ['Overdue', 'Critical (0–2d)', 'Soon (3–6d)', 'Upcoming (7+d)', 'No Date'],
        datasets: [{ data: [s.overdue, s.critical, s.soon, s.upcoming, s.no_date],
          backgroundColor: ['#dc2626','#f97316','#eab308','#059669','#94a3b8'],
          borderRadius: 5 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false } }, y: { ticks: { stepSize: 1 } } },
      },
    });
    return () => { if (barChart.current) barChart.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  const s = data.summary;
  const urgColor = d => d == null ? '#94a3b8' : d < 0 ? '#dc2626' : d <= 2 ? '#f97316' : d <= 6 ? '#eab308' : '#059669';
  const urgLabel = d => d == null ? '—' : d < 0 ? `${Math.abs(d)}d late` : d === 0 ? 'Ships today' : `${d}d`;
  return (
    <div className="card">
      <div className="card-title">🚨 Rush Orders</div>
      <div className="card-sub">Open orders with planned ship date within 31 days · sorted by ship date</div>
      <div style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:8,marginBottom:12}}>
        {[['Overdue',s.overdue,'#dc2626'],['Critical',s.critical,'#f97316'],['Soon',s.soon,'#eab308'],['Upcoming',s.upcoming,'#059669']].map(([l,v,c])=>(
          <div key={l} className="stat-chip">
            <div className="stat-chip-label">{l}</div>
            <div className="stat-chip-value" style={{color:c}}>{v}</div>
          </div>
        ))}
      </div>
      <div className="chart-wrap" style={{height:130,marginBottom:12}}><canvas ref={barRef}/></div>
      <div style={{maxHeight:220,overflowY:'auto'}}>
        <table className="data-table">
          <thead><tr><th>Order #</th><th>Customer</th><th>Status</th><th className="r">Ship Date</th><th className="r">Lead</th><th className="r">Ships In</th></tr></thead>
          <tbody>
            {data.orders.length === 0
              ? <tr><td colSpan={6} style={{color:'var(--muted)',padding:10}}>No rush orders found</td></tr>
              : data.orders.map(o => (
                <tr key={o.OrderNo}>
                  <td><strong>{o.OrderNo}</strong></td>
                  <td style={{fontSize:11}}>{o.CustomerName}</td>
                  <td><span className="badge badge-other" style={{fontSize:10}}>{o.OrderStatus}</span></td>
                  <td className="r" style={{fontSize:11}}>{o.PlannedShipDate||'—'}</td>
                  <td className="r" style={{fontSize:11,color:'#7c3aed',fontWeight:600}}>{o.LeadDays != null ? `${o.LeadDays}d` : '—'}</td>
                  <td className="r"><span style={{fontWeight:700,fontSize:12,color:urgColor(o.DaysUntilShip)}}>{urgLabel(o.DaysUntilShip)}</span></td>
                </tr>
              ))
            }
          </tbody>
        </table>
      </div>
      <div className="card-source">Source: your_database · Orders WHERE PlannedShipDate ≤ today+31 · excludes cancelled/invoiced/tentative</div>
    </div>
  );
}

// ── Top Customers ─────────────────────────────────────────────────────────────
function TopCustomers({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  const customers = data ? (data.last90 || []) : [];

  useEffect(() => {
    if (!canvasRef.current || !customers.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const items = customers.slice(0, 8);
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: items.map(c => c.CustomerName.length > 22 ? c.CustomerName.slice(0,20)+'…' : c.CustomerName),
        datasets: [{
          label: 'Revenue', data: items.map(c => c.Revenue),
          backgroundColor: PALETTE, borderRadius: 5,
        }],
      },
      options: {
        indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => ' ' + fmt(ctx.raw) + '  (' + items[ctx.dataIndex].OrderCount + ' orders)' } },
        },
        scales: {
          x: { ticks: { callback: v => '$'+(v>=1000?(v/1000).toFixed(0)+'k':v) }, grid: { color:'#f1f5f9' } },
          y: { grid: { display:false } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [customers]);

  if (!data) return <div className="card"><Spinner /></div>;
  return (
    <div className="card">
      <div className="card-title">🏅 Top Customers</div>
      <div className="card-sub">By revenue · last 90 days · excludes cancelled &amp; tentative</div>
      {customers.length === 0
        ? <p className="empty-msg">No data for this period.</p>
        : <div className="chart-wrap" style={{height:240}}><canvas ref={canvasRef}/></div>
      }
      <div className="card-source">Source: your_database · Orders JOIN Customers · SUM(OrderDetails.Total)</div>
    </div>
  );
}

// ── On-Time Delivery ──────────────────────────────────────────────────────────
function OnTimeDelivery({ data }) {
  const donutRef  = useRef(null); const donutChart = useRef(null);
  const lineRef   = useRef(null); const lineChart  = useRef(null);

  useEffect(() => {
    if (!data || !donutRef.current) return;
    if (donutChart.current) donutChart.current.destroy();
    const s = data.summary;
    donutChart.current = new Chart(donutRef.current, {
      type: 'doughnut',
      data: {
        labels: ['On Time','Late 1–3d','Late 4+d'],
        datasets: [{ data:[s.on_time,s.late_1_3,s.late_4_plus],
          backgroundColor:['#059669','#f97316','#dc2626'], borderWidth:2 }],
      },
      options: {
        responsive:true, maintainAspectRatio:false, cutout:'72%',
        plugins: {
          legend: { position:'bottom', labels:{ font:{size:11}, boxWidth:10 } },
          tooltip: { callbacks:{ label: ctx => ` ${ctx.label}: ${ctx.raw} orders` } },
        },
      },
    });
    return () => { if (donutChart.current) donutChart.current.destroy(); };
  }, [data]);

  useEffect(() => {
    if (!data || !lineRef.current || !data.monthly.length) return;
    if (lineChart.current) lineChart.current.destroy();
    lineChart.current = new Chart(lineRef.current, {
      type: 'line',
      data: {
        labels: data.monthly.map(r => r.period),
        datasets: [{
          label:'On-Time %', data: data.monthly.map(r => r.rate),
          borderColor:'#059669', backgroundColor:'rgba(5,150,105,.1)',
          tension:0.35, fill:true, pointRadius:4, pointBackgroundColor:'#059669',
        }],
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins:{ legend:{display:false} },
        scales:{
          x:{ grid:{display:false} },
          y:{ min:0, max:100, ticks:{ callback: v => v+'%' } },
        },
      },
    });
    return () => { if (lineChart.current) lineChart.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  const s = data.summary;
  const rateColor = s.rate >= 85 ? '#059669' : s.rate >= 65 ? '#f97316' : '#dc2626';
  return (
    <div className="card">
      <div className="card-title">✅ On-Time Delivery</div>
      <div className="card-sub">PlannedShipDate vs DateShipped · Invoiced orders · last 90 days</div>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:12,marginBottom:12}}>
        <div style={{position:'relative'}}>
          <div className="chart-wrap" style={{height:175}}><canvas ref={donutRef}/></div>
          <div style={{position:'absolute',top:'40%',left:'50%',transform:'translate(-50%,-50%)',textAlign:'center',pointerEvents:'none'}}>
            <div style={{fontSize:26,fontWeight:800,color:rateColor}}>{s.rate}%</div>
            <div style={{fontSize:10,color:'var(--muted)'}}>on time</div>
          </div>
        </div>
        <div style={{display:'flex',flexDirection:'column',justifyContent:'center',gap:7}}>
          {[['On Time',s.on_time,'#059669'],['Late 1–3d',s.late_1_3,'#f97316'],['Late 4+d',s.late_4_plus,'#dc2626'],['Total',s.total,'#0f172a']].map(([l,v,c])=>(
            <div key={l} style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'6px 10px',background:'#f8fafc',borderRadius:8}}>
              <span style={{fontSize:11,color:'var(--muted)'}}>{l}</span>
              <span style={{fontSize:15,fontWeight:700,color:c}}>{fmtN(v)}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="chart-wrap" style={{height:110}}><canvas ref={lineRef}/></div>
      <div className="card-source">Source: your_database · Orders WHERE OrderStatus='Invoiced' AND DateShipped IS NOT NULL AND PlannedShipDate IS NOT NULL</div>
    </div>
  );
}

// ── In-Hands Dates ────────────────────────────────────────────────────────────
function InHandsDates({ data }) {
  const barRef  = useRef(null);
  const barChart = useRef(null);
  useEffect(() => {
    if (!data || !barRef.current) return;
    if (barChart.current) barChart.current.destroy();
    const s = data.summary;
    barChart.current = new Chart(barRef.current, {
      type: 'bar',
      data: {
        labels: ['Overdue','≤ 7 days','8–14 days','15–30 days'],
        datasets: [{ data:[s.overdue,s.within_7,s.within_14,s.within_30],
          backgroundColor:['#dc2626','#f97316','#eab308','#2563eb'], borderRadius:5 }],
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins:{ legend:{display:false} },
        scales:{ x:{grid:{display:false}}, y:{ticks:{stepSize:1}} },
      },
    });
    return () => { if (barChart.current) barChart.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  const s = data.summary;
  const dayColor = d => d < 0 ? '#dc2626' : d <= 7 ? '#f97316' : d <= 14 ? '#eab308' : '#2563eb';
  const dayLabel = d => d < 0 ? `${Math.abs(d)}d late` : d === 0 ? 'Today' : `${d}d`;
  return (
    <div className="card">
      <div className="card-title">📅 Upcoming In-Hands Dates</div>
      <div className="card-sub">Open orders with in-hands dates in the next 30 days</div>
      <div style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:8,marginBottom:12}}>
        {[['Overdue',s.overdue,'#dc2626'],['≤ 7 days',s.within_7,'#f97316'],['8–14 days',s.within_14,'#eab308'],['15–30 days',s.within_30,'#2563eb']].map(([l,v,c])=>(
          <div key={l} className="stat-chip">
            <div className="stat-chip-label">{l}</div>
            <div className="stat-chip-value" style={{color:c}}>{v}</div>
          </div>
        ))}
      </div>
      <div className="chart-wrap" style={{height:120,marginBottom:12}}><canvas ref={barRef}/></div>
      <div style={{maxHeight:200,overflowY:'auto'}}>
        <table className="data-table">
          <thead><tr><th>Order #</th><th>Customer</th><th>Status</th><th className="r">In-Hands</th><th className="r">Days Out</th></tr></thead>
          <tbody>
            {data.orders.length === 0
              ? <tr><td colSpan={5} style={{color:'var(--muted)',padding:10}}>No upcoming in-hands dates</td></tr>
              : data.orders.map(o=>(
                <tr key={o.OrderNo}>
                  <td>
                    <strong>{o.OrderNo}</strong>
                    {o.Rush && <span className="badge badge-rush" style={{fontSize:9}}>RUSH</span>}
                  </td>
                  <td style={{fontSize:11}}>{o.CustomerName}</td>
                  <td><span className="badge badge-other" style={{fontSize:10}}>{o.OrderStatus}</span></td>
                  <td className="r" style={{fontSize:11}}>{o.InHandsDate||'—'}</td>
                  <td className="r"><span style={{fontWeight:700,fontSize:12,color:dayColor(o.DaysOut)}}>{dayLabel(o.DaysOut)}</span></td>
                </tr>
              ))
            }
          </tbody>
        </table>
      </div>
      <div className="card-source">Source: your_database · Orders WHERE InHandsDate BETWEEN today AND today+30 · excludes cancelled/invoiced/tentative</div>
    </div>
  );
}

// ── AOV Trend ─────────────────────────────────────────────────────────────────
function AOVTrend({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current || !data.trend.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const avg = data.trend.reduce((s, r) => s + r.aov, 0) / data.trend.length;
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: data.trend.map(r => r.period),
        datasets: [
          { label: 'AOV', data: data.trend.map(r => r.aov),
            borderColor: '#7c3aed', backgroundColor: 'rgba(124,58,237,.1)',
            tension: 0.35, fill: true, pointRadius: 4, pointBackgroundColor: '#7c3aed' },
          { label: '13-mo Avg', data: data.trend.map(() => Math.round(avg * 100) / 100),
            borderColor: '#94a3b8', borderDash: [4, 3], borderWidth: 1.5, pointRadius: 0, fill: false },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => '$' + v } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return <div className="card"><div className="card-title">💵 Average Order Value Trend</div><div className="error-msg">{data.error}</div></div>;
  const items = data.trend || [];
  const latest = items[items.length - 1];
  const prev   = items[items.length - 2];
  const delta  = latest && prev && prev.aov ? ((latest.aov - prev.aov) / prev.aov * 100).toFixed(1) : null;
  return (
    <div className="card">
      <div className="card-title">💵 Average Order Value Trend</div>
      <div className="card-sub">
        Latest: <strong>{latest ? fmt(latest.aov) : '—'}</strong>
        {delta != null && (
          <span style={{color: parseFloat(delta) >= 0 ? '#059669' : '#dc2626', marginLeft: 8, fontWeight: 600}}>
            {parseFloat(delta) >= 0 ? '▲' : '▼'} {Math.abs(delta)}% vs prior month
          </span>
        )}
      </div>
      <div className="chart-wrap" style={{height: 210}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · SUM(OrderDetails.Total) / COUNT(Orders) by month · excludes cancelled &amp; tentative</div>
    </div>
  );
}

// ── Backlog Card ───────────────────────────────────────────────────────────────
function BacklogCard({ data }) {
  if (!data) return <div className="card"><Spinner /></div>;
  return (
    <div className="card">
      <div className="card-title">📦 Revenue Backlog</div>
      <div className="card-sub">Open orders in production — excludes invoiced, cancelled, tentative, void</div>
      <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 14}}>
        <div className="stat-chip">
          <div className="stat-chip-label">Backlog Value</div>
          <div className="stat-chip-value" style={{color: 'var(--blue)', fontSize: 22}}>{fmt(data.backlog_value)}</div>
        </div>
        <div className="stat-chip">
          <div className="stat-chip-label">Open Orders</div>
          <div className="stat-chip-value">{fmtN(data.order_count)}</div>
        </div>
      </div>
      <div className="card-source">Source: your_database · Orders WHERE status ∉ &#123;Invoiced, Cancelled, Tentative, Void&#125;</div>
    </div>
  );
}

// ── AR Aging ───────────────────────────────────────────────────────────────────
function ARAgingCard({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    const b = data.buckets;
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: ['0–30 days', '31–60 days', '61–90 days', '90+ days'],
        datasets: [{ data: [b['0_30'], b['31_60'], b['61_90'], b['90_plus']],
          backgroundColor: ['#059669', '#d97706', '#f97316', '#dc2626'], borderRadius: 5 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: ctx => ' ' + fmt(ctx.raw) } } },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0)+'k' : v) } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return <div className="card"><div className="card-title">🗓 Invoice Age Distribution</div><div className="error-msg">{data.error}</div></div>;
  const b = data.buckets;
  const aged = b['61_90'] + b['90_plus'];
  return (
    <div className="card">
      <div className="card-title">🗓 Invoice Age Distribution</div>
      <div className="card-sub">
        {fmtN(data.total_invoices)} invoices · {fmt(data.total_value)} total ·{' '}
        <span style={{color: aged > 0 ? '#dc2626' : '#059669', fontWeight: 600}}>
          {fmt(aged)} aged 60+ days
        </span>
      </div>
      <div className="chart-wrap" style={{height: 150}}><canvas ref={canvasRef}/></div>
      {data.oldest && data.oldest.length > 0 && (
        <>
          <div style={{fontSize:10,fontWeight:700,textTransform:'uppercase',letterSpacing:'1px',color:'var(--muted)',margin:'12px 0 6px'}}>Oldest Invoices</div>
          <div style={{maxHeight: 150, overflowY: 'auto'}}>
            <table className="data-table" style={{fontSize: 11.5}}>
              <thead><tr><th>Invoice</th><th>Customer</th><th className="r">Amount</th><th className="r">Age</th></tr></thead>
              <tbody>
                {data.oldest.map(r => (
                  <tr key={r.invoice_no}>
                    <td><strong>{r.invoice_no}</strong></td>
                    <td style={{maxWidth:140,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{r.customer}</td>
                    <td className="r">{fmt(r.total)}</td>
                    <td className="r">
                      <span className={r.age_days > 90 ? 'churn-high' : r.age_days > 60 ? 'churn-medium' : 'churn-low'}>
                        {r.age_days}d
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="card-source">Source: your_database · Invoices WHERE OrderStatus='Invoiced' · age = days since InvoiceDate</div>
    </div>
  );
}

// ── Customer Mix ───────────────────────────────────────────────────────────────
function CustomerMix({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current) return;
    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: data.labels,
        datasets: [
          { label: 'New Customers',    data: data.new,    backgroundColor: 'rgba(37,99,235,.82)',  borderRadius: 3 },
          { label: 'Repeat Customers', data: data.repeat, backgroundColor: 'rgba(5,150,105,.75)',  borderRadius: 3 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } } },
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, ticks: { stepSize: 5 } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  const totalNew    = (data.new    || []).reduce((a, b) => a + b, 0);
  const totalRepeat = (data.repeat || []).reduce((a, b) => a + b, 0);
  const total = totalNew + totalRepeat;
  return (
    <div className="card">
      <div className="card-title">👥 New vs Repeat Customers</div>
      <div className="card-sub">
        Last 12 months ·{' '}
        <span style={{color:'var(--blue)',fontWeight:600}}>{totalNew} new</span>{' · '}
        <span style={{color:'#059669',fontWeight:600}}>{totalRepeat} repeat</span>
        {total > 0 && <span style={{color:'var(--muted)'}}> · {Math.round(totalRepeat/total*100)}% returning</span>}
      </div>
      <div className="chart-wrap" style={{height: 210}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · "New" = first-ever order in that month · "Repeat" = customer has prior orders</div>
    </div>
  );
}

// ── Order Frequency ────────────────────────────────────────────────────────────
function OrderFrequency({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!data || !canvasRef.current || !data.distribution.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const items = data.distribution;
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: items.map(d => d.bucket),
        datasets: [{ label: 'Customers', data: items.map(d => d.customers),
          backgroundColor: PALETTE, borderRadius: 5 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { afterLabel: ctx => `  ${data.distribution[ctx.dataIndex].orders} total orders` } },
        },
        scales: { x: { grid: { display: false } }, y: { ticks: { stepSize: 10 } } },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);
  if (!data) return <div className="card"><Spinner /></div>;
  const total = (data.distribution || []).reduce((s, d) => s + d.customers, 0);
  return (
    <div className="card">
      <div className="card-title">📊 Customer Order Frequency</div>
      <div className="card-sub">Orders per customer — last 12 months · {fmtN(total)} active customers · 6+ = 6 or more orders</div>
      <div className="chart-wrap" style={{height: 210}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · COUNT(Orders) GROUP BY CustomerID · last 12 months · excludes cancelled &amp; tentative</div>
    </div>
  );
}

// ── Churn Risk ─────────────────────────────────────────────────────────────────
function ChurnRisk({ data }) {
  if (!data) return <div className="card"><Spinner /></div>;
  const riskCls = d => d > 180 ? 'churn-high' : d > 90 ? 'churn-medium' : 'churn-low';
  return (
    <div className="card">
      <div className="card-title">⚡ Churn Risk — At-Risk Customers</div>
      <div className="card-sub">2+ orders in last 24 months · no order in 60+ days · sorted by revenue at risk</div>
      {data.customers.length === 0
        ? <p className="empty-msg">No at-risk customers — great retention!</p>
        : (
          <div style={{overflowX: 'auto'}}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th className="r">Orders</th>
                  <th className="r">Revenue (24mo)</th>
                  <th className="r">Last Order</th>
                  <th className="r">Days Since</th>
                </tr>
              </thead>
              <tbody>
                {data.customers.map((c, i) => (
                  <tr key={i}>
                    <td><strong>{c.name}</strong></td>
                    <td className="r">{fmtN(c.orders)}</td>
                    <td className="r">{fmt(c.revenue)}</td>
                    <td className="r" style={{fontSize: 11}}>{c.last_order}</td>
                    <td className="r"><span className={riskCls(c.days_since)}>{c.days_since}d</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
      <div className="card-source">Source: your_database · Orders JOIN Customers · HAVING last_order &lt; today−60 AND order_count ≥ 2</div>
    </div>
  );
}

// ── Marketing KPIs ─────────────────────────────────────────────────────────────
function MarketingKPIs({ data }) {
  if (!data) return <div className="card"><Spinner /></div>;

  // Extract ROAS from summary text
  let roas = null;
  const txt = data.summary || '';
  const roasM = txt.match(/(\d+\.\d+)\s*ROAS|ROAS[^0-9]*(\d+\.\d+)/i);
  if (roasM) roas = parseFloat(roasM[1] || roasM[2]);

  // Attribution gap from tracking_issues evidence
  let metaRev = null, ga4Rev = null;
  (data.tracking_issues || []).forEach(ti => {
    (ti.evidence || []).forEach(e => {
      const m1 = e.match(/\$([\d,]+\.?\d*)\s*Meta/i);
      const m2 = e.match(/\$([\d,]+\.?\d*)\s*(matching paid |paid )?GA4/i);
      if (m1) metaRev = parseFloat(m1[1].replace(/,/g, ''));
      if (m2) ga4Rev  = parseFloat(m2[1].replace(/,/g, ''));
    });
  });
  const attrGap = (metaRev != null && ga4Rev != null) ? metaRev - ga4Rev : null;

  // CRM unresolved count
  let crmCount = null;
  const crmAction = (data.top_actions || []).find(a => a.action_type === 'crm');
  if (crmAction) {
    const m = (crmAction.evidence || []).join(' ').match(/(\d+)\s+unresolved/i);
    if (m) crmCount = parseInt(m[1]);
  }

  const start = data.analysis_start ? data.analysis_start.slice(0,10) : null;
  const end   = data.analysis_end   ? data.analysis_end.slice(0,10)   : null;
  const roasColor = roas == null ? 'var(--muted)' : roas >= 3.5 ? '#059669' : roas >= 2.5 ? '#d97706' : '#dc2626';

  return (
    <div className="card">
      <div className="card-title">📡 Paid Media Snapshot</div>
      <div className="card-sub">{start && end ? `${start} → ${end}` : 'From marketing advice webhook'}</div>
      <div style={{display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:10, marginTop:14}}>
        <div className="stat-chip">
          <div className="stat-chip-label">Overall ROAS</div>
          <div className="stat-chip-value" style={{color: roasColor}}>{roas != null ? roas.toFixed(2)+'x' : '—'}</div>
        </div>
        <div className="stat-chip">
          <div className="stat-chip-label">Attribution Gap</div>
          <div className="stat-chip-value" style={{color: attrGap != null ? '#dc2626' : 'var(--muted)', fontSize: 18}}>
            {attrGap != null ? fmt(attrGap) : '—'}
          </div>
          {attrGap != null && <div style={{fontSize:10,color:'var(--muted)',marginTop:2}}>Meta vs GA4</div>}
        </div>
        <div className="stat-chip">
          <div className="stat-chip-label">CRM Unresolved</div>
          <div className="stat-chip-value" style={{color: crmCount ? '#dc2626' : '#059669'}}>
            {crmCount != null ? crmCount : '—'}
          </div>
          {crmCount != null && <div style={{fontSize:10,color:'var(--muted)',marginTop:2}}>lost contacts</div>}
        </div>
      </div>
      <div className="card-source">Source: customcustomersupport.com · webhook/get-marketing-advice</div>
    </div>
  );
}

// ── Marketing Advice ─────────────────────────────────────────────────────────
function MarketingAdvice({ data }) {
  if (!data) return <div className="card"><Spinner /></div>;
  if (data.error) return (
    <div className="card">
      <div className="card-title">📣 Marketing Advice</div>
      <div className="error-msg">Could not load marketing advice: {data.error}</div>
    </div>
  );

  const riskClass = r => r === 'high' ? 'ma-risk-high' : r === 'medium' ? 'ma-risk-medium' : 'ma-risk-low';
  const moveClass = m => m === 'decrease' ? 'ma-move-decrease' : m === 'hold' ? 'ma-move-hold' : 'ma-move-increase';
  const moveLabel = m => m === 'decrease' ? '▼ Reduce' : m === 'hold' ? '— Hold' : '▲ Increase';

  const start = data.analysis_start ? data.analysis_start.slice(0,10) : null;
  const end   = data.analysis_end   ? data.analysis_end.slice(0,10)   : null;

  return (
    <div className="card">
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:2}}>
        <div className="card-title">📣 Marketing Advice</div>
        {start && end && <div className="ma-date-range">{start} → {end}</div>}
      </div>
      <div className="card-sub">AI-generated recommendations · {data.action_count || 0} actions</div>

      {data.summary && <div className="ma-summary">{data.summary}</div>}
      {data.overall_recommendation && (
        <div className="ma-recommendation">
          <strong>Overall recommendation: </strong>{data.overall_recommendation}
        </div>
      )}
      {data.budget_guidance && (
        <div className="ma-budget-guidance">
          <strong>Budget guidance: </strong>{data.budget_guidance}
        </div>
      )}

      {/* Top Actions */}
      {data.top_actions && data.top_actions.length > 0 && (
        <>
          <div className="ma-section-title">Top Actions</div>
          {data.top_actions.map(a => (
            <div key={a.rank} className="ma-action">
              <div className="ma-action-header">
                <div className="ma-rank">{a.rank}</div>
                <div className="ma-action-title">{a.campaign_or_area}</div>
                <span className={`ma-risk-badge ${riskClass(a.priority || a.risk)}`}>
                  {(a.priority || a.risk || '').toUpperCase()}
                </span>
                {a.platform && (
                  <span style={{fontSize:10.5, color:'var(--muted)', fontWeight:500}}>{a.platform}</span>
                )}
              </div>
              <div className="ma-action-body">{a.reason}</div>
              {a.specific_change && (
                <div className="ma-action-change">→ {a.specific_change}</div>
              )}
              {a.evidence && a.evidence.length > 0 && (
                <div className="ma-evidence">
                  {a.evidence.map((e,i) => <span key={i} className="ma-evidence-chip">{e}</span>)}
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {/* Budget Moves */}
      {data.budget_moves && data.budget_moves.length > 0 && (
        <>
          <div className="ma-section-title">Budget Moves</div>
          {data.budget_moves.map((bm, i) => (
            <div key={i} className="ma-budget-row">
              <span className={moveClass(bm.move)}>{moveLabel(bm.move)}</span>
              <div className="ma-campaign-name">{bm.campaign}</div>
              {bm.budget_change_pct != null && bm.budget_change_pct !== 0 && (
                <span className="ma-budget-delta" style={{color: bm.move==='decrease'?'#dc2626':bm.move==='hold'?'#d97706':'#059669'}}>
                  {bm.move==='decrease'?'-':bm.move==='increase'?'+':''}{Math.abs(bm.budget_change_pct)}%
                  {bm.estimated_daily_dollar_change != null ? ` / ${bm.estimated_daily_dollar_change > 0 ? '+' : ''}$${bm.estimated_daily_dollar_change}/day` : ''}
                </span>
              )}
              {(bm.move === 'hold' || bm.budget_change_pct == null) && bm.guardrail && (
                <span style={{fontSize:10.5,color:'var(--muted)',flex:1,textAlign:'right'}}>{bm.guardrail}</span>
              )}
            </div>
          ))}
        </>
      )}

      {/* Tracking Issues */}
      {data.tracking_issues && data.tracking_issues.length > 0 && (
        <>
          <div className="ma-section-title">⚠ Tracking Issues</div>
          {data.tracking_issues.map((ti, i) => (
            <div key={i} className="ma-tracking-row">
              <div style={{fontWeight:600,marginBottom:3}}>{ti.issue}</div>
              {ti.evidence && ti.evidence.map((e,j) => (
                <div key={j} style={{fontSize:11,color:'#991b1b'}}>{e}</div>
              ))}
            </div>
          ))}
        </>
      )}

      {/* Do Not Change */}
      {data.do_not_change && data.do_not_change.length > 0 && (
        <>
          <div className="ma-section-title">🔒 Do Not Change</div>
          {data.do_not_change.map((d, i) => (
            <div key={i} className="ma-donotchange-row">
              <strong>{d.area}</strong>
              {(d.reason || d.recommendation) && (
                <div style={{marginTop:2,fontWeight:400,color:'#5b21b6'}}>{d.reason || d.recommendation}</div>
              )}
            </div>
          ))}
        </>
      )}

      {/* Creative Tests */}
      {data.full_result && data.full_result.creative_tests && data.full_result.creative_tests.length > 0 && (
        <>
          <div className="ma-section-title">🎨 Creative Tests</div>
          {data.full_result.creative_tests.map((ct, i) => (
            <div key={i} className="ma-creative-row">
              <strong>{ct.test}</strong>
              {ct.reason && <div style={{marginTop:2,fontWeight:400}}>{ct.reason}</div>}
            </div>
          ))}
        </>
      )}

      {/* Landing Page Actions */}
      {data.full_result && data.full_result.landing_page_actions && data.full_result.landing_page_actions.length > 0 && (
        <>
          <div className="ma-section-title">🖥 Landing Page Actions</div>
          {data.full_result.landing_page_actions.map((lp, i) => (
            <div key={i} className="ma-lp-row">
              <strong>{lp.action}</strong>
              {lp.evidence && lp.evidence.length > 0 && (
                <div className="ma-evidence" style={{marginTop:4}}>
                  {lp.evidence.map((e,j) => <span key={j} className="ma-evidence-chip">{e}</span>)}
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {/* Execution Order */}
      {(() => {
        // prefer full_result array, fall back to flat execution_order1–5 fields
        const steps = (data.full_result && data.full_result.execution_order)
          ? data.full_result.execution_order
          : [1,2,3,4,5].map(n => data[`execution_order${n}`]).filter(Boolean);
        if (!steps || steps.length === 0) return null;
        return (
          <>
            <div className="ma-section-title">📋 Execution Order</div>
            <ul className="ma-exec-list">
              {steps.map((s, i) => (
                <li key={i} className="ma-exec-item">
                  <div className="ma-exec-num">{i+1}</div>
                  <span>{String(s).replace(/^\d+\.\s*/, '')}</span>
                </li>
              ))}
            </ul>
          </>
        );
      })()}

      <div className="card-source">Source: customcustomersupport.com · webhook/get-marketing/advice</div>
    </div>
  );
}

// ── Product Month YoY ────────────────────────────────────────────────────────
function ProductMonthYoY({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  const [sortBy, setSortBy] = useState('rev_cy');
  const [view,   setView]   = useState('chart');

  const items     = data ? (data.items || []) : [];
  const month_name = data ? data.month_name : '';
  const year_cy   = data ? data.year_cy  : '';
  const year_ly1  = data ? data.year_ly1 : '';
  const year_ly2  = data ? data.year_ly2 : '';
  const sorted    = [...items].sort((a, b) => b[sortBy] - a[sortBy]).slice(0, 12);
  const pctColor  = v => v == null ? '#64748b' : v > 0 ? '#059669' : v < 0 ? '#dc2626' : '#64748b';
  const pctFmt    = v => v == null ? '—' : (v > 0 ? '+' : '') + v + '%';

  useEffect(() => {
    if (view !== 'chart' || !canvasRef.current || !sorted.length) return;
    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: sorted.map(r => r.prodno),
        datasets: [
          {
            label: String(year_cy),
            data: sorted.map(r => sortBy === 'qty_cy' ? r.qty_cy : r.rev_cy),
            backgroundColor: 'rgba(37,99,235,0.85)',
            borderRadius: 5,
          },
          {
            label: String(year_ly1),
            data: sorted.map(r => sortBy === 'qty_cy' ? r.qty_ly1 : r.rev_ly1),
            backgroundColor: 'rgba(124,58,237,0.45)',
            borderRadius: 5,
          },
          {
            label: String(year_ly2),
            data: sorted.map(r => sortBy === 'qty_cy' ? r.qty_ly2 : r.rev_ly2),
            backgroundColor: 'rgba(148,163,184,0.4)',
            borderRadius: 5,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.raw;
                return ` ${ctx.dataset.label}: ${sortBy === 'qty_cy' ? fmtN(v) : fmt(v)}`;
              }
            }
          }
        },
        scales: {
          x: { grid: { display: false }, ticks: { font: { size: 11 }, maxRotation: 35 } },
          y: {
            grid: { color: '#f1f5f9' },
            ticks: { callback: v => sortBy === 'qty_cy' ? fmtN(v) : ('$'+(v>=1000?(v/1000).toFixed(0)+'k':v)) }
          },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data, sortBy, view]);

  const btnStyle = active => ({
    background: active ? 'var(--blue)' : '#f1f5f9',
    color: active ? '#fff' : 'var(--muted)',
    border: 'none', borderRadius: 6, padding: '4px 10px',
    fontSize: 11, fontWeight: 600, cursor: 'pointer',
  });

  if (!data) return <div className="card"><Spinner /></div>;

  return (
    <div className="card">
      <div style={{display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:4}}>
        <div className="card-title">📊 Top Products — {month_name} vs Prior Years</div>
        <div style={{display:'flex', gap:4}}>
          <button onClick={() => setView('chart')} style={btnStyle(view==='chart')}>Chart</button>
          <button onClick={() => setView('table')} style={btnStyle(view==='table')}>Table</button>
          <button onClick={() => setSortBy('rev_cy')} style={btnStyle(sortBy==='rev_cy')}>$ Rev</button>
          <button onClick={() => setSortBy('qty_cy')} style={btnStyle(sortBy==='qty_cy')}>Qty</button>
        </div>
      </div>
      <div className="card-sub">
        {year_cy} vs {year_ly1} vs {year_ly2} · top 12 products · excludes cancelled &amp; tentative
      </div>

      {view === 'chart' ? (
        <div className="chart-wrap" style={{height:300}}>
          <canvas ref={canvasRef} />
        </div>
      ) : (
        <div style={{overflowX:'auto'}}>
          <table style={{width:'100%', borderCollapse:'collapse', fontSize:12}}>
            <thead>
              <tr style={{background:'#f8fafc'}}>
                <th style={{textAlign:'left', padding:'6px 8px', color:'var(--muted)', fontWeight:700, fontSize:11, borderBottom:'2px solid var(--border)'}}>Product</th>
                {[[year_cy,'var(--blue)'],[year_ly1,'#7c3aed'],[year_ly2,'#94a3b8']].map(([yr,col]) => (
                  <th key={yr} colSpan={2} style={{textAlign:'center', padding:'6px 8px', color:col, fontWeight:700, fontSize:11, borderBottom:'2px solid var(--border)'}}>{yr}</th>
                ))}
                <th style={{textAlign:'right', padding:'6px 8px', color:'var(--muted)', fontWeight:700, fontSize:11, borderBottom:'2px solid var(--border)'}}>vs LY</th>
                <th style={{textAlign:'right', padding:'6px 8px', color:'var(--muted)', fontWeight:700, fontSize:11, borderBottom:'2px solid var(--border)'}}>vs 2Y</th>
              </tr>
              <tr style={{background:'#f8fafc'}}>
                <th style={{padding:'3px 8px', borderBottom:'1px solid var(--border)'}}></th>
                {[year_cy,year_ly1,year_ly2].map(yr => [
                  <th key={yr+'r'} style={{textAlign:'right', padding:'3px 8px', color:'var(--muted)', fontSize:10, fontWeight:600, borderBottom:'1px solid var(--border)'}}>$</th>,
                  <th key={yr+'q'} style={{textAlign:'right', padding:'3px 8px', color:'var(--muted)', fontSize:10, fontWeight:600, borderBottom:'1px solid var(--border)'}}>Qty</th>,
                ])}
                <th style={{textAlign:'right', padding:'3px 8px', color:'var(--muted)', fontSize:10, fontWeight:600, borderBottom:'1px solid var(--border)'}}>Δ%</th>
                <th style={{textAlign:'right', padding:'3px 8px', color:'var(--muted)', fontSize:10, fontWeight:600, borderBottom:'1px solid var(--border)'}}>Δ%</th>
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0
                ? <tr><td colSpan={9} style={{color:'var(--muted)', padding:12, textAlign:'center'}}>No data</td></tr>
                : sorted.map((r, i) => (
                  <tr key={r.prodno} style={{background: i%2===0 ? '#fff' : '#fafafa'}}>
                    <td style={{padding:'6px 8px', fontWeight:700, fontFamily:'monospace', borderBottom:'1px solid #f1f5f9'}}>{r.prodno}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, borderBottom:'1px solid #f1f5f9'}}>{fmt(r.rev_cy)}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, borderBottom:'1px solid #f1f5f9'}}>{fmtN(r.qty_cy)}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', color:'#7c3aed', borderBottom:'1px solid #f1f5f9'}}>{r.rev_ly1 > 0 ? fmt(r.rev_ly1) : '—'}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', color:'#7c3aed', borderBottom:'1px solid #f1f5f9'}}>{r.qty_ly1 > 0 ? fmtN(r.qty_ly1) : '—'}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', color:'#94a3b8', borderBottom:'1px solid #f1f5f9'}}>{r.rev_ly2 > 0 ? fmt(r.rev_ly2) : '—'}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', color:'#94a3b8', borderBottom:'1px solid #f1f5f9'}}>{r.qty_ly2 > 0 ? fmtN(r.qty_ly2) : '—'}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, color:pctColor(r.rev_pct_ly1), borderBottom:'1px solid #f1f5f9'}}>{pctFmt(r.rev_pct_ly1)}</td>
                    <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, color:pctColor(r.rev_pct_ly2), borderBottom:'1px solid #f1f5f9'}}>{pctFmt(r.rev_pct_ly2)}</td>
                  </tr>
                ))
              }
            </tbody>
          </table>
        </div>
      )}
      <div className="card-source">
        {year_cy}: {data.cy_range} · {year_ly1}: {data.ly1_range} · {year_ly2}: {data.ly2_range}
      </div>
    </div>
  );
}

// ── R112 Monthly Trend ───────────────────────────────────────────────────────
function R112Trend({ data }) {
  const barRef  = useRef(null);
  const barChart = useRef(null);

  useEffect(() => {
    if (!data || !barRef.current || !data.months.length) return;
    if (barChart.current) barChart.current.destroy();
    const months = data.months;
    barChart.current = new Chart(barRef.current, {
      type: 'bar',
      data: {
        labels: months.map(m => m.period),
        datasets: [
          { label: 'This Year $',  data: months.map(m => m.revenue),    backgroundColor: 'rgba(37,99,235,.85)', borderRadius: 4 },
          { label: 'Prior Year $', data: months.map(m => m.ly_revenue), backgroundColor: 'rgba(37,99,235,.22)', borderRadius: 4 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } },
          tooltip: { callbacks: { label: ctx => ' ' + fmt(ctx.raw) } },
        },
        scales: {
          x: { grid: { display: false } },
          y: { ticks: { callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v) }, grid: { color: '#f1f5f9' } },
        },
      },
    });
    return () => { if (barChart.current) barChart.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  const months = data.months || [];
  const pctColor = v => v == null ? '#94a3b8' : v > 0 ? '#059669' : v < 0 ? '#dc2626' : '#64748b';
  const pctFmt   = v => v == null ? '—' : (v > 0 ? '+' : '') + v + '%';
  const th = (align='right', color='var(--muted)') => ({
    textAlign: align, padding: '5px 8px', fontWeight: 700, fontSize: 10.5,
    color, borderBottom: '2px solid var(--border)', background: '#f8fafc', whiteSpace: 'nowrap',
  });

  return (
    <div className="card">
      <div className="card-title">📦 R112 — Monthly Sales vs Prior Year</div>
      <div className="card-sub">Revenue and quantity by month · last 12 full months</div>
      <div className="chart-wrap" style={{height: 190, marginBottom: 16}}>
        <canvas ref={barRef}/>
      </div>
      <div style={{overflowX:'auto'}}>
        <table style={{width:'100%', borderCollapse:'collapse', fontSize:12}}>
          <thead>
            <tr>
              <th style={th('left')}>Month</th>
              <th style={th('right','var(--blue)')}>$ TY</th>
              <th style={th('right','var(--blue)')}>Qty TY</th>
              <th style={th()}>$ LY</th>
              <th style={th()}>Qty LY</th>
              <th style={th()}>$ Δ%</th>
              <th style={th()}>Qty Δ%</th>
            </tr>
          </thead>
          <tbody>
            {months.length === 0
              ? <tr><td colSpan={7} style={{color:'var(--muted)',padding:12,textAlign:'center'}}>No R112 data found</td></tr>
              : months.map((m, i) => (
                <tr key={m.period} style={{borderBottom:'1px solid #f1f5f9', background: i%2===0?'#fff':'#fafafa'}}>
                  <td style={{padding:'6px 10px', fontWeight:600}}>{m.period}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, color:'var(--text)'}}>{m.revenue > 0 ? fmt(m.revenue) : '—'}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', fontWeight:700, color:'var(--text)'}}>{m.qty > 0 ? fmtN(m.qty) : '—'}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', color:'#94a3b8'}}>{m.ly_revenue > 0 ? fmt(m.ly_revenue) : '—'}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', color:'#94a3b8'}}>{m.ly_qty > 0 ? fmtN(m.ly_qty) : '—'}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', fontWeight:600, color:pctColor(m.rev_delta_pct)}}>{pctFmt(m.rev_delta_pct)}</td>
                  <td style={{padding:'6px 8px', textAlign:'right', fontWeight:600, color:pctColor(m.qty_delta_pct)}}>{pctFmt(m.qty_delta_pct)}</td>
                </tr>
              ))
            }
          </tbody>
        </table>
      </div>
      <div className="card-source">
        Source: your_database · OrderDetails JOIN Orders WHERE ProdNo = 'R112' · last 12 full months · excludes Cancelled, Tentative, Void
      </div>
    </div>
  );
}

// ── R112 Tab ──────────────────────────────────────────────────────────────────
function R112KPIs({ summary }) {
  if (!summary) return <div className="r112-kpi-grid">{[...Array(4)].map((_,i)=><div key={i} className="kpi-card"><Spinner/></div>)}</div>;
  const pctFmt = v => v == null ? null : (v > 0 ? '+' : '') + v + '%';
  const pctCls = v => v == null ? '' : v > 0 ? 'pct-up' : v < 0 ? 'pct-down' : 'pct-flat';
  const cards = [
    { label: 'Revenue (12 mo)',    value: fmt(summary.rev_cy),     sub: fmt(summary.rev_py) + ' prior yr',     pct: summary.rev_pct,    color: 'blue' },
    { label: 'Units Sold (12 mo)', value: fmtN(summary.qty_cy),    sub: fmtN(summary.qty_py) + ' prior yr',    pct: summary.qty_pct,    color: 'green' },
    { label: 'Orders (12 mo)',     value: fmtN(summary.orders_cy), sub: fmtN(summary.orders_py) + ' prior yr', pct: summary.orders_pct, color: 'purple' },
    { label: 'Avg R112 Rev/Order', value: fmt(summary.aov_cy),     sub: fmt(summary.aov_py) + ' prior yr',     pct: summary.aov_pct,    color: 'amber' },
  ];
  return (
    <div className="r112-kpi-grid">
      {cards.map(c => (
        <div key={c.label} className={`kpi-card ${c.color}`}>
          <div className="kpi-label">{c.label}</div>
          <div className="kpi-value">{c.value}</div>
          <div className="kpi-sub">{c.sub}</div>
          {c.pct != null && <div className={`kpi-yoy ${pctCls(c.pct)}`}>{c.pct > 0 ? '▲' : '▼'} {pctFmt(c.pct)} <span className="kpi-yoy-label">vs prior yr</span></div>}
        </div>
      ))}
    </div>
  );
}

function R112ColorChart({ colors }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);
  useEffect(() => {
    if (!colors || !canvasRef.current || !colors.length) return;
    if (chartRef.current) chartRef.current.destroy();
    const top = colors.slice(0, 12);
    chartRef.current = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: top.map(c => c.color),
        datasets: [
          { label: 'Units',   data: top.map(c => c.qty),     backgroundColor: 'rgba(37,99,235,.82)', borderRadius: 5, yAxisID: 'y'  },
          { label: 'Revenue', data: top.map(c => c.revenue), backgroundColor: 'rgba(124,58,237,.65)', borderRadius: 5, yAxisID: 'y2' },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 10 } },
          tooltip: { callbacks: { label: ctx => ctx.dataset.label === 'Revenue' ? ' ' + fmt(ctx.raw) : ' ' + fmtN(ctx.raw) + ' units' } },
        },
        scales: {
          x:  { grid: { display: false }, ticks: { maxRotation: 35, font: { size: 10 } } },
          y:  { position: 'left',  ticks: { callback: v => fmtN(v) },                                           grid: { color: '#f1f5f9' } },
          y2: { position: 'right', ticks: { callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v) }, grid: { display: false } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [colors]);
  if (!colors) return <div className="card"><Spinner /></div>;
  return (
    <div className="card">
      <div className="card-title">🎨 R112 Sales by Color — Last 12 Months</div>
      <div className="card-sub">Units and revenue per color variant · top 12 colors</div>
      <div className="chart-wrap" style={{height: 220}}><canvas ref={canvasRef}/></div>
      <div className="card-source">Source: your_database · OrderDetails WHERE ProdNo = 'R112' · GROUP BY Color</div>
    </div>
  );
}

function R112TopCustomers({ customers }) {
  if (!customers) return <div className="card"><Spinner /></div>;
  return (
    <div className="card">
      <div className="card-title">👥 Top R112 Customers — Last 12 Months</div>
      <div className="card-sub">Ranked by R112 revenue</div>
      <table className="data-table">
        <thead><tr>
          <th>Customer</th><th className="r">Revenue</th><th className="r">Units</th><th className="r">Orders</th>
        </tr></thead>
        <tbody>
          {customers.length === 0
            ? <tr><td colSpan={4} style={{color:'var(--muted)',textAlign:'center',padding:12}}>No data</td></tr>
            : customers.map((c,i) => (
              <tr key={i}>
                <td style={{fontWeight:600}}>{c.name}</td>
                <td className="r">{fmt(c.revenue)}</td>
                <td className="r">{fmtN(c.qty)}</td>
                <td className="r">{fmtN(c.orders)}</td>
              </tr>
            ))
          }
        </tbody>
      </table>
      <div className="card-source">Source: your_database · OrderDetails JOIN Orders JOIN Customers · ProdNo = 'R112'</div>
    </div>
  );
}

function R112RelatedSKUs({ skus }) {
  if (!skus) return <div className="card"><Spinner /></div>;
  const pctCls = v => v == null ? 'pct-flat' : v > 0 ? 'pct-up' : v < 0 ? 'pct-down' : 'pct-flat';
  const pctFmt = v => v == null ? '—' : (v > 0 ? '+' : '') + v + '%';
  return (
    <div className="card">
      <div className="card-title">🏷️ Other Richardson SKUs — 12 Mo vs Prior 12 Mo</div>
      <div className="card-sub">How R112 compares to the rest of the Richardson line</div>
      <table className="data-table">
        <thead><tr>
          <th>SKU</th><th className="r">Rev (CY)</th><th className="r">Qty (CY)</th>
          <th className="r">Rev (PY)</th><th className="r">Rev Δ</th><th className="r">Qty Δ</th>
        </tr></thead>
        <tbody>
          {skus.length === 0
            ? <tr><td colSpan={6} style={{color:'var(--muted)',textAlign:'center',padding:12}}>No related SKUs found</td></tr>
            : skus.map((s,i) => (
              <tr key={i}>
                <td className="prod-no">{s.prodno}</td>
                <td className="r">{fmt(s.rev_cy)}</td>
                <td className="r">{fmtN(s.qty_cy)}</td>
                <td className="r" style={{color:'#94a3b8'}}>{fmt(s.rev_py)}</td>
                <td className={`r ${pctCls(s.rev_pct)}`}>{pctFmt(s.rev_pct)}</td>
                <td className={`r ${pctCls(s.qty_pct)}`}>{pctFmt(s.qty_pct)}</td>
              </tr>
            ))
          }
        </tbody>
      </table>
      <div className="card-source">Source: your_database · OrderDetails JOIN Products WHERE Brand = 'Richardson' AND ProdNo ≠ 'R112'</div>
    </div>
  );
}

function R112Specials({ specials }) {
  if (!specials) return <div className="card"><Spinner /></div>;
  if (!specials.length) return (
    <div className="card">
      <div className="card-title">🎁 R112 / Richardson Specials History</div>
      <div className="card-sub">No specials found in the last 24 months</div>
    </div>
  );
  return (
    <div className="card">
      <div className="card-title">🎁 R112 / Richardson Specials History</div>
      <div className="card-sub">Promotional pricing history · last 24 months</div>
      <table className="data-table">
        <thead><tr>
          <th>Type</th><th>Item</th><th className="r">Discount</th><th>Start</th><th>End</th>
        </tr></thead>
        <tbody>
          {specials.map((s, i) => (
            <tr key={i}>
              <td>{s.type || '—'}</td>
              <td style={{fontWeight:600}}>{s.item || '—'}</td>
              <td className="r">{s.discount != null ? s.discount + '%' : '—'}</td>
              <td style={{color:'var(--muted)', fontSize:11}}>{s.start_date || '—'}</td>
              <td style={{color:'var(--muted)', fontSize:11}}>{s.end_date   || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="card-source">Source: your_database · Specials WHERE SpecialItem LIKE 'R112%' OR = 'Richardson'</div>
    </div>
  );
}

// ── R112 Cancellations Chart ──────────────────────────────────────────────────
function R112CancelsChart({ data }) {
  const canvasRef = useRef(null);
  const chartRef  = useRef(null);

  useEffect(() => {
    if (!data || !canvasRef.current || !data.series || !data.series.length) return;
    if (chartRef.current) chartRef.current.destroy();

    const colors = ['#2563eb','#dc2626','#059669','#d97706','#7c3aed','#0891b2','#db2777','#65a30d'];
    chartRef.current = new Chart(canvasRef.current, {
      type: 'line',
      data: {
        labels: data.months,
        datasets: data.series.map((s, i) => ({
          label: s.prodno,
          data: s.monthly.map(m => m.orders),
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length].replace(')', ',.08)').replace('rgb', 'rgba'),
          tension: 0.3, fill: false, pointRadius: 3, borderWidth: s.prodno === 'R112' ? 2.5 : 1.5,
        })),
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'top', labels: { font: { size: 11 }, boxWidth: 10, padding: 8 } },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 35, font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { stepSize: 1 }, title: { display: true, text: 'Cancelled Orders', font: { size: 10 } } },
        },
      },
    });
    return () => { if (chartRef.current) chartRef.current.destroy(); };
  }, [data]);

  if (!data) return <div className="card"><Spinner /></div>;
  if (!data.series || !data.series.length) return (
    <div className="card">
      <div className="card-title">❌ R112 Cancellations</div>
      <div className="empty-msg">No cancellation data</div>
    </div>
  );

  const r112 = data.series.find(s => s.prodno === 'R112');
  const total = r112 ? r112.total_orders : 0;

  return (
    <div className="card">
      <div className="card-title">❌ R112 — Cancellations by Month</div>
      <div className="card-sub">
        Cancelled orders containing R112 vs other Richardson SKUs · last 12 full months
        {total > 0 && <span style={{marginLeft:12,fontWeight:700,color:'#dc2626'}}>{total} R112 cancellations</span>}
      </div>
      <div style={{height: 220}}>
        <canvas ref={canvasRef} />
      </div>
      <div className="card-source">Source: your_database · Orders WHERE OrderStatus LIKE '%cancel%' AND Brand = 'Richardson'</div>
    </div>
  );
}

// ── R112 Tab (composite) ──────────────────────────────────────────────────────
function R112Tab({ trend, detail, cancels }) {
  const summary   = detail ? detail.summary         : null;
  const colors    = detail ? detail.colors          : null;
  const customers = detail ? detail.top_customers   : null;
  const skus      = detail ? detail.related_skus    : null;
  const specials  = detail ? detail.specials_history : null;

  return (
    <div className="main">
      <div className="section-label">
        R112 — Performance Summary
        {summary && <span style={{fontWeight:400,textTransform:'none',letterSpacing:0}}>&nbsp;· {summary.cy_range}</span>}
      </div>
      <R112KPIs summary={summary} />

      <div className="section-label">R112 — Monthly Trend vs Prior Year</div>
      <R112Trend data={trend} />

      <div className="section-label">R112 — Color Breakdown &amp; Promo History</div>
      <div className="grid-2 equal">
        <R112ColorChart colors={colors} />
        <R112Specials specials={specials} />
      </div>

      <div className="section-label">R112 — Top Customers &amp; Richardson Lineup</div>
      <div className="grid-2 equal">
        <R112TopCustomers customers={customers} />
        <R112RelatedSKUs skus={skus} />
      </div>

      <div className="section-label">R112 — Cancellations</div>
      <R112CancelsChart data={cancels} />
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────

function App() {
  const [kpis,        setKpis]        = useState(null);
  const [trend,       setTrend]       = useState(null);
  const [sellers,     setSellers]     = useState(null);
  const [breakdown,   setBreakdown]   = useState(null);
  const [orders,      setOrders]      = useState(null);
  const [specials,    setSpecials]    = useState(null);
  const [lag,         setLag]         = useState(null);
  const [health,      setHealth]      = useState(null);
  const [prodno,      setProdno]      = useState(null);
  const [rush,        setRush]        = useState(null);
  const [topCust,     setTopCust]     = useState(null);
  const [onTime,      setOnTime]      = useState(null);
  const [inHands,     setInHands]     = useState(null);
  const [productYoY,  setProductYoY]  = useState(null);
  const [r112,        setR112]        = useState(null);
  const [r112Detail,  setR112Detail]  = useState(null);
  const [r112Cancels, setR112Cancels] = useState(null);
  const [aovTrend,    setAovTrend]    = useState(null);
  const [backlog,     setBacklog]     = useState(null);
  const [arAging,     setArAging]     = useState(null);
  const [custMix,     setCustMix]     = useState(null);
  const [churnRisk,   setChurnRisk]   = useState(null);
  const [orderFreq,   setOrderFreq]   = useState(null);
  const [quotes,      setQuotes]      = useState(null);
  const [activeTab,   setActiveTab]   = useState('overview');
  const [loading,     setLoading]     = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  const safeFetch = url => fetch(url).then(r => r.ok ? r.json() : null).catch(() => null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // Fire every fetch simultaneously. Each .then() updates state the moment
      // its response arrives — fast endpoints render immediately without waiting
      // for slow ones to finish.
      await Promise.allSettled([
        safeFetch('/api/kpis').then(setKpis),
        safeFetch('/api/revenue-trend').then(setTrend),
        safeFetch('/api/best-sellers').then(setSellers),
        safeFetch('/api/sales-breakdown').then(setBreakdown),
        safeFetch('/api/recent-orders').then(setOrders),
        safeFetch('/api/specials').then(setSpecials),
        safeFetch('/api/backlog').then(setBacklog),
        safeFetch('/api/invoice-lag').then(setLag),
        safeFetch('/api/order-health').then(setHealth),
        safeFetch('/api/prodno-trend').then(setProdno),
        safeFetch('/api/rush-orders').then(setRush),
        safeFetch('/api/top-customers').then(setTopCust),
        safeFetch('/api/on-time-delivery').then(setOnTime),
        safeFetch('/api/in-hands').then(setInHands),
        safeFetch('/api/product-month-yoy').then(setProductYoY),
        safeFetch('/api/r112-trend').then(setR112),
        safeFetch('/api/r112-detail').then(setR112Detail),
        safeFetch('/api/r112-cancellations').then(setR112Cancels),
        safeFetch('/api/aov-trend').then(setAovTrend),
        safeFetch('/api/ar-aging').then(setArAging),
        safeFetch('/api/customer-mix').then(setCustMix),
        safeFetch('/api/churn-risk').then(setChurnRisk),
        safeFetch('/api/order-frequency').then(setOrderFreq),
        safeFetch('/api/quotes').then(setQuotes),
      ]);

      setLastRefresh(new Date().toLocaleTimeString());
    } catch (err) {
      console.error('Dashboard load error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5 * 60 * 1000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <>
      {/* Header */}
      <header className="header">
        <div className="header-logo">
          <svg viewBox="0 0 36 36" width="36" height="36" xmlns="http://www.w3.org/2000/svg">
            <circle cx="18" cy="18" r="18" fill="#2563eb"/>
            <text x="18" y="24" textAnchor="middle" fontSize="18" fill="white" fontWeight="bold">S</text>
          </svg>
        </div>
        <div className="header-title-group">
          <div className="header-title">Embroidery Shop</div>
          <div className="header-sub">Operations Dashboard</div>
        </div>
        <div className="header-right">
          {lastRefresh && <span className="refresh-time">Updated {lastRefresh}</span>}
          <button className="refresh-btn" onClick={load} disabled={loading}>
            {loading ? '⟳ Loading…' : '⟳ Refresh'}
          </button>
        </div>
      </header>

      {/* Tab Nav */}
      <nav className="tab-nav">
        <button className={`tab-btn${activeTab==='overview'?' active':''}`} onClick={()=>setActiveTab('overview')}>📊 Overview</button>
        <button className={`tab-btn${activeTab==='customers'?' active':''}`} onClick={()=>setActiveTab('customers')}>👥 Customers</button>
        <button className={`tab-btn${activeTab==='products'?' active':''}`} onClick={()=>setActiveTab('products')}>🏷️ Products</button>
        <button className={`tab-btn${activeTab==='r112'?' active':''}`} onClick={()=>setActiveTab('r112')}>📦 R112 Deep Dive</button>
      </nav>

      {/* ── Overview (merged with Revenue & Finance, Operations) ── */}
      {activeTab === 'overview' && <div className="main">
        <div className="section-label">Key Metrics</div>
        <KPISection data={kpis} quotes={quotes} />
        <div className="section-label">Revenue Backlog</div>
        <BacklogCard data={backlog} />

        <div className="section-label">Revenue Trends</div>
        <RevenueTrendChart data={trend} />
        <div className="grid-2 equal" style={{marginTop:12}}>
          <MTDComparison data={kpis} />
          <InvoiceLag data={lag} />
        </div>

        <div className="section-label">Revenue Quality</div>
        <div className="grid-2 equal">
          <AOVTrend data={aovTrend} />
          <ARAgingCard data={arAging} />
        </div>

        <div className="section-label">Order Health</div>
        <div className="grid-2 equal" style={{marginTop:12}}>
          <OrderPipeline data={kpis} />
          <OrderHealth data={health} />
        </div>

        <div className="section-label">Rush &amp; Deadlines</div>
        <div className="grid-2 equal">
          <RushOrders data={rush} />
          <InHandsDates data={inHands} />
        </div>

        <div className="section-label">Recent Orders</div>
        <RecentOrders data={orders} />

      </div>}

      {/* ── Customers ── */}
      {activeTab === 'customers' && <div className="main">
        <div style={{display:'grid', gridTemplateColumns:'1fr 1fr', gap:12, alignItems:'start'}}>
          {/* Left column — Churn Risk full height */}
          <ChurnRisk data={churnRisk} />

          {/* Right column — stacked visual cards */}
          <div style={{display:'flex', flexDirection:'column', gap:12}}>
            <TopCustomers data={topCust} />
            <OnTimeDelivery data={onTime} />
            <CustomerMix data={custMix} />
            <OrderFrequency data={orderFreq} />
          </div>
        </div>
      </div>}

      {/* ── Products ── */}
      {activeTab === 'products' && <div className="main">
        <div className="section-label">Sales Performance — Last 90 Days</div>
        <div className="grid-2 left-heavy">
          <BestSellers data={sellers} />
          <CategoryChart data={breakdown} />
        </div>
        <div className="grid-2 equal" style={{marginTop:12}}>
          <BrandTable data={breakdown} />
          <Promotions data={specials} />
        </div>

        <div className="section-label">Product Sales — Month vs Prior Years</div>
        <ProductMonthYoY data={productYoY} />

        <div className="section-label">Product Volume &amp; Recent Orders</div>
        <div className="grid-2 equal">
          <ProdnoTrend data={prodno} />
          <RecentOrders data={orders} />
        </div>
      </div>}

      {/* ── R112 Deep Dive ── */}
      {activeTab === 'r112' && <R112Tab trend={r112} detail={r112Detail} cancels={r112Cancels} />}
    </>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
</body>
</html>
"""


async def dashboard(request: Request):
    return HTMLResponse(DASHBOARD_HTML)


# ── App ───────────────────────────────────────────────────────────────────────

app = Starlette(routes=[
    Route("/",                       dashboard),
    Route("/api/kpis",               api_kpis),
    Route("/api/revenue-trend",      api_revenue_trend),
    Route("/api/best-sellers",       api_best_sellers),
    Route("/api/sales-breakdown",    api_sales_breakdown),
    Route("/api/recent-orders",      api_recent_orders),
    Route("/api/specials",           api_specials),
    Route("/api/invoice-lag",        api_invoice_lag),
    Route("/api/order-health",       api_order_health),
    Route("/api/prodno-trend",       api_prodno_trend),
    Route("/api/product-month-yoy",  api_product_month_yoy),
    Route("/api/r112-trend",         api_r112_trend),
    Route("/api/r112-detail",        api_r112_detail),
    Route("/api/r112-cancellations", api_r112_cancellations),
    Route("/api/proof-times",        api_proof_times),
    Route("/api/proof-cancellation", api_proof_cancellation),
    Route("/api/rush-orders",        api_rush_orders),
    Route("/api/top-customers",      api_top_customers),
    Route("/api/on-time-delivery",   api_on_time_delivery),
    Route("/api/in-hands",           api_in_hands),
    Route("/api/debug",              api_debug),
    Route("/api/aov-trend",          api_aov_trend),
    Route("/api/backlog",            api_backlog),
    Route("/api/ar-aging",           api_ar_aging),
    Route("/api/customer-mix",       api_customer_mix),
    Route("/api/churn-risk",         api_churn_risk),
    Route("/api/order-frequency",    api_order_frequency),
    Route("/api/quotes",             api_quotes),
    Route("/api/marketing-advice",   api_marketing_advice),
])

if __name__ == "__main__":
    import signal, sys, threading, time
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    config = uvicorn.Config(app, host=host, port=port, loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.should_exit and thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        print("\nDashboard stopped.")
