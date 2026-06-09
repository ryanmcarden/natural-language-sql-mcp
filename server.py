from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP
from database import Database

mcp = FastMCP(
    name="embroidery-business",
    instructions=(
        "You are an assistant for a custom embroidery shop. "
        "This server is READ-ONLY. "
        "Use these tools to look up customers, browse orders, check embroidery designs, "
        "find products, and view pricing. "
        "Orders are referenced by their OrderNo (e.g. 23456), not by internal OrderID. "
        "Customers are organisations - use Organization as the customer name."
    ),
)

db = Database()


@mcp.tool()
def search_customers(search: str = "", limit: int = 20) -> dict:
    """
    Search for customers by organisation name or email address.

    Args:
        search: Partial name or email to filter by. Leave blank to list all.
        limit:  Maximum number of results (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.search_customers(search=search, limit=limit)


@mcp.tool()
def get_customer(customer_id: int) -> dict:
    """
    Get the full profile for a customer, including all contacts and 10 most recent orders.

    Args:
        customer_id: The numeric CustomerID (returned by search_customers).
    """
    return db.get_customer(customer_id=customer_id)


@mcp.tool()
def list_orders(
    status: str = "",
    customer_id: int | None = None,
    search: str = "",
    limit: int = 20,
) -> dict:
    """
    List orders, optionally filtered by status, customer, or keyword.

    Args:
        status:      Filter by OrderStatus (e.g. Open, Shipped, Invoiced).
                     Use list_order_statuses to see all valid values.
        customer_id: Return only orders for this CustomerID (optional).
        search:      Free-text search against OrderNo or customer Organisation name.
        limit:       Maximum results (default 20).
    """
    limit = min(max(1, limit), 100)
    return db.list_orders(status=status, customer_id=customer_id, search=search, limit=limit)


@mcp.tool()
def get_order(order_no: str) -> dict:
    """
    Get complete details for a single order: header, customer, contact, line items,
    embroidery specs, decoration details, invoice, machine assignments, and recent history.

    Args:
        order_no: The human-readable order number (e.g. 23456), NOT the internal OrderID.
    """
    return db.get_order(order_no=order_no)


@mcp.tool()
def get_order_history(order_no: str, limit: int = 50) -> dict:
    """
    Get the full audit trail for an order from OrderHistory.
    Shows who did what and when.

    Args:
        order_no: The order number to look up.
        limit:    Maximum history entries to return (default 50).
    """
    limit = min(max(1, limit), 200)
    return db.get_order_history(order_no=order_no, limit=limit)


@mcp.tool()
def get_contact_history(
    order_no: str = "",
    customer_id: int | None = None,
    contact_id: int | None = None,
    search: str = "",
    limit: int = 50,
    describe: bool = False,
) -> dict:
    """
    Find ContactHistory records associated with an order, customer/client,
    contact, or search term.

    Use this when staff asks things like:
      - "show contact history for order 23456"
      - "show notes/calls/emails for customer 123"
      - "find contact history mentioning invoice issue"

    Args:
        order_no:    Human-readable OrderNo, e.g. 23456. Resolves to OrderID,
                     CustomerID, and ContactID when needed.
        customer_id: CustomerID/client ID to filter history by.
        contact_id:  ContactID/person to filter history by.
        search:      Keyword search across text columns and related customer/contact/order fields.
        limit:       Maximum history entries to return, default 50, max 500.
        describe:    If true, returns ContactHistory columns and sample rows.
    """
    limit = min(max(1, limit), 500)
    return db.get_contact_history(
        order_no=order_no,
        customer_id=customer_id,
        contact_id=contact_id,
        search=search,
        limit=limit,
        describe=describe,
    )


@mcp.tool()
def get_customer_360(
    customer_id: int | None = None,
    search: str = "",
    order_no: str = "",
    days: int = 365,
    limit: int = 20,
) -> dict:
    """Full customer/client briefing: profile, contacts, recent orders, revenue, product history, open orders, and ContactHistory notes."""
    return db.get_customer_360(customer_id=customer_id, search=search, order_no=order_no, days=days, limit=limit)


@mcp.tool()
def search_all_internal_notes(
    search: str,
    order_no: str = "",
    customer_id: int | None = None,
    days: int = 365,
    limit: int = 100,
    match_mode: str = "any",
) -> dict:
    """Search staff notes/comments across ContactHistory, OrderHistory, Orders, OrderDetails, EmbData, and DecorationDetails."""
    return db.search_all_internal_notes(search=search, order_no=order_no, customer_id=customer_id, days=days, limit=limit, match_mode=match_mode)


@mcp.tool()
def find_order_risks(
    days: int = 120,
    limit: int = 50,
    customer_id: int | None = None,
    include_invoiced: bool = False,
) -> dict:
    """Find orders that look risky: overdue, rush, stalled, spoiled, missing tracking, or risk keywords in history."""
    return db.find_order_risks(days=days, limit=limit, customer_id=customer_id, include_invoiced=include_invoiced)


@mcp.tool()
def explain_order_timeline(order_no: str, limit: int = 100) -> dict:
    """Explain an order timeline using order header fields, OrderHistory, ContactHistory, assignments, shipping, and invoice data."""
    return db.explain_order_timeline(order_no=order_no, limit=limit)


@mcp.tool()
def get_reorder_opportunities(
    days_since_last_order: int = 180,
    min_prior_orders: int = 2,
    min_total_revenue: float = 500,
    category: str = "",
    brand: str = "",
    limit: int = 50,
) -> dict:
    """Find customers with meaningful prior purchase history who have not reordered recently."""
    return db.get_reorder_opportunities(days_since_last_order=days_since_last_order, min_prior_orders=min_prior_orders, min_total_revenue=min_total_revenue, category=category, brand=brand, limit=limit)


@mcp.tool()
def get_customer_product_history(
    customer_id: int | None = None,
    search: str = "",
    days: int = 1095,
    limit: int = 50,
) -> dict:
    """Show what a customer usually buys: products, categories, colors, sizes, designs, quantities, and seasonality."""
    return db.get_customer_product_history(customer_id=customer_id, search=search, days=days, limit=limit)


@mcp.tool()
def find_similar_past_orders(
    prod_no: str = "",
    category: str = "",
    customer_type: str = "",
    quantity: int | None = None,
    decoration_type: str = "",
    search: str = "",
    days: int = 1095,
    limit: int = 50,
) -> dict:
    """Find past orders similar by product, category, quantity, customer type, decoration type, or free-text search."""
    return db.find_similar_past_orders(prod_no=prod_no, category=category, customer_type=customer_type, quantity=quantity, decoration_type=decoration_type, search=search, days=days, limit=limit)


@mcp.tool()
def get_design_usage_history(
    design_no: str = "",
    customer_id: int | None = None,
    search: str = "",
    limit: int = 50,
) -> dict:
    """Find orders/products/customers where a design or logo has been used."""
    return db.get_design_usage_history(design_no=design_no, customer_id=customer_id, search=search, limit=limit)


@mcp.tool()
def find_margin_leaks(
    qty: int = 12,
    max_margin: float = 20,
    days: int = 365,
    limit: int = 50,
    min_revenue: float = 0,
    category: str = "",
    brand: str = "",
) -> dict:
    """Find products with low gross margin and recent revenue exposure."""
    return db.find_margin_leaks(qty=qty, max_margin=max_margin, days=days, limit=limit, min_revenue=min_revenue, category=category, brand=brand)


@mcp.tool()
def get_orders_by_prodno(
    prod_no: str = "R112",
    status: str = "",
    days: int = 365,
    limit: int = 50,
) -> dict:
    """
    List orders containing a specific product number, sorted by line-item quantity
    (highest first). Defaults to ProdNo R112.

    Args:
        prod_no: Product number to filter by (default R112).
        status:  Optional order status filter (e.g. Open, Shipped, Invoiced).
        days:    How far back to search in days (default 365).
        limit:   Maximum results (default 50, max 500).
    """
    limit = min(max(1, limit), 500)
    return db.get_orders_by_prodno(prod_no=prod_no, status=status, days=days, limit=limit)


@mcp.tool()
def list_order_statuses() -> dict:
    """
    List all valid order status values from the OrderStatus lookup table.
    """
    return db.list_order_statuses()


@mcp.tool()
def list_embroidery_designs(
    customer_id: int | None = None,
    search: str = "",
    limit: int = 20,
) -> dict:
    """
    Browse the embroidery design catalog (EmbDesign table).

    Args:
        customer_id: Filter to designs belonging to a specific customer (optional).
        search:      Search by design number or description.
        limit:       Maximum results (default 20).
    """
    limit = min(max(1, limit), 100)
    return db.list_embroidery_designs(customer_id=customer_id, search=search, limit=limit)


@mcp.tool()
def get_embroidery_design(design_no: str) -> dict:
    """
    Get full details for an embroidery design including stitch count, dimensions,
    digitizer notes, and which orders have used it.

    Args:
        design_no: The EmbDesignNo (e.g. E-1234), NOT the internal EmbDesignID.
    """
    return db.get_embroidery_design(design_no=design_no)


@mcp.tool()
def search_products(search: str = "", category: str = "", limit: int = 20) -> dict:
    """
    Search the product catalog. Searches ProdNo, Brand, Title, and ShortDescription.
    Inactive products are excluded.

    Args:
        search:   Keyword - product number, brand name, or title fragment.
        category: Filter to a specific category (e.g. Headwear, T-Shirts).
        limit:    Maximum results (default 20).
    """
    limit = min(max(1, limit), 100)
    return db.search_products(search=search, category=category, limit=limit)


@mcp.tool()
def search_invoices(
    customer_id: int | None = None,
    search: str = "",
    date_from: str = "",
    date_to: str = "",
    min_total: float | None = None,
    max_total: float | None = None,
    sort_by: str = "date",
    order: str = "desc",
    limit: int = 50,
) -> dict:
    """
    Search invoices with optional filters, sorted by date or amount.
    Always returns a 'totals' summary (count, EmbTotal, SalesTax, Shipping,
    InvoiceTotal) across all matched invoices.

    Args:
        customer_id: Filter to a specific customer (optional).
        search:      Search by InvoiceNo, OrderNo, or customer Organisation name.
        date_from:   Start of invoice date range, inclusive (YYYY-MM-DD).
        date_to:     End of invoice date range, inclusive (YYYY-MM-DD).
        min_total:   Minimum InvoiceTotal amount (e.g. 500.00).
        max_total:   Maximum InvoiceTotal amount (e.g. 2000.00).
        sort_by:     Column to sort by: "date" (default), "total", "embtotal", "tax", "shipping".
        order:       "desc" (default, highest/newest first) or "asc" (lowest/oldest first).
        limit:       Maximum invoices to return (default 50).

    Examples:
        Largest invoices this year:
            search_invoices(sort_by="total", order="desc", date_from="2026-01-01")
        Invoices over $1000, largest first:
            search_invoices(min_total=1000, sort_by="total", order="desc")
        Smallest invoices in a date range:
            search_invoices(date_from="2026-01-01", date_to="2026-03-31", sort_by="total", order="asc")
        Large invoices for a customer:
            search_invoices(customer_id=123, min_total=1000, sort_by="total", order="desc")
    """
    limit = min(max(1, limit), 500)
    return db.search_invoices(
        customer_id=customer_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        min_total=min_total,
        max_total=max_total,
        sort_by=sort_by,
        order=order,
        limit=limit,
    )


@mcp.tool()
def get_popular_colors(prod_no: str, days: int = 365, limit: int = 20) -> dict:
    """
    Find the most popular colours for a product, ranked by total quantity ordered.

    Args:
        prod_no: The product number to analyse (e.g. PC61).
        days:    How far back to look in order history (default 365 days).
        limit:   Maximum number of colours to return (default 20).
    """
    limit = min(max(1, limit), 100)
    return db.get_popular_colors(prod_no=prod_no, days=days, limit=limit)


@mcp.tool()
def get_sales_breakdown(days: int = 90, limit: int = 20) -> dict:
    """
    Return best-selling totals broken down by category, brand, and colour
    for invoiced orders within a recent period.

    Each breakdown is ranked by total revenue descending and includes
    TotalQty, TotalRevenue, and OrderCount.

    Args:
        days:  Number of days to look back from today (default 90).
        limit: Maximum entries per breakdown (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.get_sales_breakdown(days=days, limit=limit)


@mcp.tool()
def get_product_sales_by_period(
    prod_no: str,
    period: str = "month",
    days: int = 365,
    limit: int = 50,
) -> dict:
    """
    Show quantity sold and total revenue for a product grouped by time period.

    Args:
        prod_no: The product number to analyse (e.g. PC61).
        period:  Grouping - 'day', 'week', 'month', or 'year'. Default 'month'.
        days:    How far back to look (default 365).
        limit:   Maximum number of period rows returned (default 50).
    """
    limit = min(max(1, limit), 500)
    return db.get_product_sales_by_period(prod_no=prod_no, period=period, days=days, limit=limit)


@mcp.tool()
def get_orders_by_period(
    period: str = "month",
    days: int = 365,
    date_from: str = "",
    date_to: str = "",
    customer_id: int | None = None,
    status: str = "",
    limit: int = 50,
) -> dict:
    """
    Show order volume, quantities, revenue, invoiced totals, tax, and shipping
    grouped by time period. Use date_from/date_to for specific ranges (e.g. to
    compare this week vs the same week last year - call twice with different ranges).

    Args:
        period:      Grouping - 'day', 'week', 'month', or 'year'. Default 'month'.
        days:        How far back to look (default 365). Ignored if date_from is set.
        date_from:   Start date inclusive (YYYY-MM-DD). Overrides days.
        date_to:     End date inclusive (YYYY-MM-DD).
        customer_id: Limit to a specific customer (optional).
        status:      Filter by order status, e.g. Invoiced, Shipped (optional).
        limit:       Maximum number of period rows returned (default 50).
    """
    limit = min(max(1, limit), 500)
    return db.get_orders_by_period(
        period=period, days=days, date_from=date_from, date_to=date_to,
        customer_id=customer_id, status=status, limit=limit
    )


@mcp.tool()
def get_best_sellers_filtered(
    days: int = 90,
    category: str = "",
    brand: str = "",
    color: str = "",
    limit: int = 20,
) -> dict:
    """
    Return best-selling products filtered by category, brand, and/or colour,
    ranked by total invoiced revenue. All filters are optional and combinable.

    Args:
        days:     Number of days to look back (default 90).
        category: Filter by product category (e.g. Headwear, T-Shirts, Polos).
        brand:    Filter by brand name (e.g. Richardson, Port Authority, Gildan).
        color:    Filter by colour ordered (e.g. Navy, Black, Red).
        limit:    Maximum results (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.get_best_sellers_filtered(
        days=days, category=category, brand=brand, color=color, limit=limit
    )


@mcp.tool()
def get_best_sellers(days: int = 90, limit: int = 20) -> dict:
    """
    Return the best-selling products by total invoiced revenue over a recent period.

    Only includes orders that have a linked invoice. Results are grouped by ProdNo
    and ranked by total revenue descending.

    Args:
        days:  Number of days to look back from today (default 90).
        limit: Maximum number of products to return (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.get_best_sellers(days=days, limit=limit)


@mcp.tool()
def search_products_advanced(
    brand: str = "",
    category: str = "",
    alt_category: str = "",
    description: str = "",
    color: str = "",
    size: str = "",
    limit: int = 20,
) -> dict:
    """
    Search the product catalog using any combination of brand, category,
    alternate category, description, colour, and size. All parameters are
    optional - pass only the ones you want to filter by.

    Colour and size matching is based on order history (products previously
    ordered in that colour/size). Partial matches are supported for all fields.

    Args:
        brand:        Brand name (e.g. Port Authority, Gildan, Nike).
        category:     Primary category (e.g. Headwear, T-Shirts, Polos, Outerwear).
        alt_category: Secondary/alternate category filter.
        description:  Keyword to match against Title or ShortDescription.
        color:        Colour to filter by (e.g. Navy, Black, White, Royal).
        size:         Size to filter by (e.g. L, XL, OSFA).
        limit:        Maximum results (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.search_products_advanced(
        brand=brand,
        category=category,
        alt_category=alt_category,
        description=description,
        color=color,
        size=size,
        limit=limit,
    )


@mcp.tool()
def get_product_colors_sizes(prod_no: str) -> dict:
    """
    Get available colours and sizes for a product.

    Looks up the product by ProdNo and returns all colour/size detail rows
    from any related tables in the database (e.g. ProdColors, ProdSizes,
    Inventory). The exact tables returned depend on your database schema.

    Args:
        prod_no: The product number (e.g. PC61 or C112), as used in ProdNo.
    """
    return db.get_product_colors_sizes(prod_no=prod_no)


@mcp.tool()
def get_product_pricing(prod_no: str) -> dict:
    """
    Return a full pricing breakdown for a product at the six standard
    quantity breakpoints: 12, 24, 48, 96, 144, and 288 units.

    Each row shows:
      - GarmentCost:       product cost per unit (from Products.Cost)
      - EmbroideryPerUnit: per-garment embroidery cost for that qty bracket
      - Digitizing:        one-time digitizing fee for that qty bracket
      - TotalPerUnit:      GarmentCost + EmbroideryPerUnit
      - OrderTotal:        (TotalPerUnit x Qty) + Digitizing

    Args:
        prod_no: The product number to price (e.g. PC61, C112).
    """
    return db.get_product_pricing(prod_no=prod_no)


@mcp.tool()
def get_embroidery_pricing() -> dict:
    """
    Return the full embroidery pricing table (Embellishment table).
    Each row has a quantity bracket (Qty), digitizing fee, and per-garment embroidery cost.
    """
    return db.get_embroidery_pricing()


@mcp.tool()
def analyze_product_pricing(
    qty: int = 12,
    sort_by: str = "price",
    order: str = "desc",
    limit: int = 10,
    category: str = "",
    brand: str = "",
    min_price: float | None = None,
    max_price: float | None = None,
    min_margin: float | None = None,
    max_margin: float | None = None,
) -> dict:
    """
    Rank, filter, and analyse products by sale price, supplier cost, or gross margin
    at any standard quantity breakpoint. Answers questions like:
      - "What are the 10 most expensive SKUs I sell?"
      - "Which headwear products have the lowest margin?"
      - "Show me all Port Authority items priced over $40 at 48 units."
      - "What are my highest markup products?"
      - "Which products have margin below 30%?"

    Args:
        qty:        Quantity breakpoint to evaluate pricing at.
                    Snaps to the nearest standard: 12, 24, 48, 96, 144, 288 (default 12).
        sort_by:    Column to rank by - "price" (default), "cost", "margin", or "markup".
        order:      "desc" (default, highest first) or "asc" (lowest first).
        limit:      Number of results to return (default 10, max 100).
        category:   Filter by product category (e.g. Headwear, T-Shirts, Polos).
        brand:      Filter by brand name (e.g. Port Authority, Gildan, Nike).
        min_price:  Only include products with sale_price >= this value.
        max_price:  Only include products with sale_price <= this value.
        min_margin: Only include products with margin_pct >= this value.
        max_margin: Only include products with margin_pct <= this value.
    """
    limit = min(max(1, limit), 100)
    return db.analyze_product_pricing(
        qty=qty, sort_by=sort_by, order=order, limit=limit,
        category=category, brand=brand,
        min_price=min_price, max_price=max_price,
        min_margin=min_margin, max_margin=max_margin,
    )


@mcp.tool()
def get_product_cost_and_price(prod_no: str) -> dict:
    """
    Look up the supplier cost and customer sale prices for a single product,
    with gross margin and markup calculated at every standard quantity breakpoint
    (12, 24, 48, 96, 144, 288 units).

    Returns:
      - prod_no, brand, title
      - supplier_cost: what your shop pays the supplier per unit
      - prices: for each qty breakpoint -
          regular_price   (standard list price)
          special_price   (promotional price, if set)
          sale_price      (the lower of regular / special)
          supplier_cost   (repeated for easy comparison)
          margin_pct      (gross margin %)
          markup_pct      (markup over cost %)

    Args:
        prod_no: The product number to look up (e.g. PC61, C112, DT6000).
    """
    return db.get_product_cost_and_price(prod_no=prod_no)


@mcp.tool()
def lookup_product_costs(
    prod_nos: list[str] | None = None,
    search: str = "",
    limit: int = 20,
) -> dict:
    """
    Look up supplier cost and customer sale prices for multiple products at once.
    Useful for comparing margins across a product range or checking a batch of ProdNos.

    Pass either a list of specific ProdNos, or a search term (matches ProdNo, Brand,
    or Title). At least one of prod_nos or search must be provided.

    Returns a list of products, each with:
      - prod_no, brand, title, supplier_cost
      - prices: dict keyed by qty (12/24/48/96/144/288), each with sale_price,
        regular_price, special_price, margin_pct, markup_pct

    Args:
        prod_nos: Optional list of exact product numbers (e.g. ["PC61","C112"]).
        search:   Keyword to match against ProdNo, Brand, or Title.
        limit:    Max products to return when using search (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    return db.lookup_product_costs(prod_nos=prod_nos, search=search, limit=limit)


if __name__ == "__main__":
    import os
    import sys

    transport = os.getenv("MCP_TRANSPORT", "stdio")

    if transport == "http":
        import uvicorn

        mcp_app = mcp.streamable_http_app()

        async def app(scope, receive, send):
            if scope.get("type") == "http":
                scope["path"]     = scope["path"].lower()
                scope["raw_path"] = scope["path"].encode()
                port_val = os.getenv("MCP_PORT", "8001")
                new_headers = []
                for name, value in scope.get("headers", []):
                    if name.lower() == b"host":
                        value = f"localhost:{port_val}".encode()
                    new_headers.append((name, value))
                scope["headers"] = new_headers
            await mcp_app(scope, receive, send)

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8001"))
        uvicorn.run(app, host=host, port=port)
    else:
        import io

        class FilteredStdin(io.RawIOBase):
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def readable(self):
                return True

            def readinto(self, b):
                while True:
                    line = self._wrapped.readline()
                    if not line:
                        return 0
                    if line.strip():
                        data = line if isinstance(line, bytes) else line.encode()
                        n = len(data)
                        b[:n] = data
                        return n

        sys.stdin = io.TextIOWrapper(
            io.BufferedReader(FilteredStdin(sys.stdin.buffer)),
            encoding=sys.stdin.encoding,
            errors=sys.stdin.errors,
        )

        try:
            db.warm()
        except Exception:
            pass

        mcp.run()
