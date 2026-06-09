import "dotenv/config";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema
} from "@modelcontextprotocol/sdk/types.js";
import express from "express";
import { searchProducts, getProductByNo, getCatalogStats } from "./search.js";
import sql from "mssql";

const sqlConfig = {
  server:   process.env.DB_SERVER,
  database: process.env.DB_NAME,
  user:     process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  options: {
    trustServerCertificate: process.env.DB_TRUST_CERT === "yes",
    encrypt: false,
  },
  pool: { max: 5, min: 0, idleTimeoutMillis: 30000 },
};

let _sqlPool = null;
async function getPool() {
  if (!_sqlPool) _sqlPool = await sql.connect(sqlConfig);
  return _sqlPool;
}

const server = new Server(
  { name: "stitch-america-product-search", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "search_products",
      description: "Search the product catalog for embroidery-ready headwear and apparel.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Natural language search query" },
          limit: { type: "number", description: "Max results. Default 10, max 100." },
          brand: { type: "string", description: "Filter by brand name" },
          category: { type: "string", description: "Filter by category" },
          minPrice: { type: "number", description: "Minimum price" },
          maxPrice: { type: "number", description: "Maximum price" },
          onSale: { type: "boolean", description: "If true, return only sale items" },
          color: { type: "string", description: "Filter by color name" },
          sort: { type: "string", enum: ["default", "most popular"], description: "Sort order" }
        },
        required: ["query"]
      }
    },
    {
      name: "get_product",
      description: "Get a single product by its product number (e.g. 'R312', 'OC571').",
      inputSchema: {
        type: "object",
        properties: {
          prodNo: { type: "string", description: "The product number exactly as shown in the catalog" }
        },
        required: ["prodNo"]
      }
    },
    {
      name: "list_catalog",
      description: "Return catalog statistics: total product count, all available brands, and all available categories.",
      inputSchema: { type: "object", properties: {} }
    },
    {
      name: "query_specials",
      description: "Query the Specials table. Use describe/active/history/impact modes.",
      inputSchema: {
        type: "object",
        properties: {
          mode: { type: "string", enum: ["describe", "active", "history", "impact"] },
          from_date: { type: "string", description: "Start date (YYYY-MM-DD)" },
          to_date: { type: "string", description: "End date (YYYY-MM-DD)" },
          brand: { type: "string" },
          prod_no: { type: "string" }
        },
        required: ["mode"]
      }
    },
    {
      name: "get_r112_orders",
      description: "List orders containing product R112, sorted by order quantity (highest first).",
      inputSchema: {
        type: "object",
        properties: {
          status: { type: "string", description: "Filter by order status (e.g. Open, Shipped, Invoiced)" },
          days: { type: "number", description: "How far back to search in days. Default 365." },
          limit: { type: "number", description: "Maximum results. Default 50, max 500." }
        }
      }
    },
    {
      name: "get_proof_time",
      description: "Query average proof turnaround time from the orderPerformance table.",
      inputSchema: {
        type: "object",
        properties: {
          from_date: { type: "string" },
          to_date: { type: "string" },
          group_by: { type: "string", enum: ["month", "week", "none"] },
          describe: { type: "boolean" }
        }
      }
    }
  ]
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  try {
    let result;

    if (name === "search_products") {
      result = searchProducts(args);

    } else if (name === "get_product") {
      result = getProductByNo(args.prodNo);
      if (!result) {
        return { content: [{ type: "text", text: `No product found with ProdNo: ${args.prodNo}` }], isError: true };
      }

    } else if (name === "list_catalog") {
      result = getCatalogStats();

    } else if (name === "query_specials") {
      const pool = await getPool();
      const { mode, from_date, to_date, brand, prod_no } = args;

      if (mode === "describe") {
        const colRes = await pool.request().query(`SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'Specials' ORDER BY ORDINAL_POSITION`);
        const sampleRes = await pool.request().query("SELECT TOP 3 * FROM Specials");
        result = { columns: colRes.recordset, sample_rows: sampleRes.recordset };

      } else if (mode === "active") {
        const res = await pool.request().query(`SELECT * FROM Specials WHERE CAST(GETDATE() AS DATE) BETWEEN SpecialStartDate AND SpecialEndDate ORDER BY SpecialStartDate`);
        result = { active_specials: res.recordset, count: res.recordset.length };

      } else if (mode === "history") {
        const from = from_date ?? new Date(Date.now() - 365 * 864e5).toISOString().slice(0, 10);
        const to   = to_date   ?? new Date().toISOString().slice(0, 10);
        const req  = pool.request().input("from_date", sql.Date, from).input("to_date", sql.Date, to);
        let where = "WHERE SpecialStartDate <= @to_date AND SpecialEndDate >= @from_date";
        if (brand)   { req.input("brand",   sql.VarChar, brand);   where += " AND Brand = @brand"; }
        if (prod_no) { req.input("prod_no", sql.VarChar, prod_no); where += " AND ProdNo = @prod_no"; }
        const res = await req.query(`SELECT * FROM Specials ${where} ORDER BY SpecialStartDate DESC`);
        result = { specials: res.recordset, count: res.recordset.length, date_range: { from, to } };

      } else if (mode === "impact") {
        const from = from_date ?? new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
        const to   = to_date   ?? new Date().toISOString().slice(0, 10);
        const promoReq = pool.request().input("from_date", sql.Date, from).input("to_date", sql.Date, to);
        let joinFilter = "";
        if (brand)   { promoReq.input("brand",   sql.VarChar, brand);   joinFilter += " AND p.Brand = @brand"; }
        if (prod_no) { promoReq.input("prod_no", sql.VarChar, prod_no); joinFilter += " AND od.ProdNo = @prod_no"; }
        const duringRes = await promoReq.query(`SELECT COUNT(DISTINCT o.OrderID) AS Orders, SUM(od.Total) AS Revenue, AVG(od.Total) AS AvgLineValue, SUM(od.Qty) AS TotalQty FROM Orders o JOIN OrderDetails od ON od.OrderID = o.OrderID LEFT JOIN Products p ON p.ProdNo = od.ProdNo WHERE o.OrderDate BETWEEN @from_date AND @to_date AND o.OrderStatus NOT IN ('Cancelled','Tentative') ${joinFilter}`);
        const dayDiff   = Math.round((new Date(to) - new Date(from)) / 864e5);
        const priorTo   = new Date(new Date(from) - 864e5).toISOString().slice(0, 10);
        const priorFrom = new Date(new Date(from) - (dayDiff + 1) * 864e5).toISOString().slice(0, 10);
        const priorReq  = pool.request().input("from_date", sql.Date, priorFrom).input("to_date", sql.Date, priorTo);
        if (brand)   priorReq.input("brand",   sql.VarChar, brand);
        if (prod_no) priorReq.input("prod_no", sql.VarChar, prod_no);
        const priorRes = await priorReq.query(`SELECT COUNT(DISTINCT o.OrderID) AS Orders, SUM(od.Total) AS Revenue, AVG(od.Total) AS AvgLineValue, SUM(od.Qty) AS TotalQty FROM Orders o JOIN OrderDetails od ON od.OrderID = o.OrderID LEFT JOIN Products p ON p.ProdNo = od.ProdNo WHERE o.OrderDate BETWEEN @from_date AND @to_date AND o.OrderStatus NOT IN ('Cancelled','Tentative') ${joinFilter}`);
        const d = duringRes.recordset[0] ?? {};
        const p = priorRes.recordset[0]  ?? {};
        const pct = (a, b) => b && b !== 0 ? Math.round(((a - b) / b) * 1000) / 10 : null;
        result = {
          filter: { brand: brand ?? null, prod_no: prod_no ?? null },
          promo_window:  { from, to, days: dayDiff },
          prior_window:  { from: priorFrom, to: priorTo, days: dayDiff },
          during_promo:  { orders: d.Orders, revenue: d.Revenue, avg_line_value: d.AvgLineValue, qty: d.TotalQty },
          before_promo:  { orders: p.Orders, revenue: p.Revenue, avg_line_value: p.AvgLineValue, qty: p.TotalQty },
          change: { orders_pct: pct(d.Orders, p.Orders), revenue_pct: pct(d.Revenue, p.Revenue), avg_line_pct: pct(d.AvgLineValue, p.AvgLineValue), qty_pct: pct(d.TotalQty, p.TotalQty) }
        };
      }

    } else if (name === "get_r112_orders") {
      const pool  = await getPool();
      const days  = args.days  ? Number(args.days)  : 365;
      const limit = Math.min(args.limit ? Number(args.limit) : 50, 500);
      const req   = pool.request().input("days", sql.Int, days).input("limit", sql.Int, limit);
      let where = "od.ProdNo = 'R112' AND o.OrderDate >= DATEADD(day, -@days, GETDATE()) AND o.OrderNo NOT LIKE 'T%'";
      if (args.status) { req.input("status", sql.VarChar, args.status); where += " AND o.OrderStatus = @status"; }
      const res = await req.query(`SELECT TOP (@limit) o.OrderNo, o.OrderDate, o.OrderStatus, o.ItemStatus, o.Rush, o.PlannedShipDate, o.InHandsDate, o.DateShipped, c.CustomerID, c.Organization AS CustomerName, od.ProdNo, od.Color, od.Size, od.Quantity, od.Price, od.Total, od.Description AS LineDescription FROM OrderDetails od JOIN Orders o ON o.OrderID = od.OrderID JOIN Customers c ON c.CustomerID = o.CustomerID WHERE ${where} ORDER BY od.Quantity DESC, o.OrderDate DESC`);
      result = { prod_no: "R112", count: res.recordset.length, orders: res.recordset };

    } else if (name === "get_proof_time") {
      const pool = await getPool();
      if (args.describe) {
        const colRes    = await pool.request().query(`SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'orderPerformance' ORDER BY ORDINAL_POSITION`);
        const sampleRes = await pool.request().query("SELECT TOP 3 * FROM orderPerformance");
        result = { columns: colRes.recordset, sample_rows: sampleRes.recordset };
      } else {
        const fromDate = args.from_date ?? new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
        const toDate   = args.to_date   ?? new Date().toISOString().slice(0, 10);
        const groupBy  = args.group_by  ?? "month";
        const sumReq   = pool.request().input("from_date", sql.Date, fromDate).input("to_date", sql.Date, toDate);
        const baseWhere = "WHERE ProofSentDate >= @from_date AND ProofSentDate <= @to_date AND ApprovalDate IS NOT NULL AND ApprovalDate > ProofSentDate";
        const sumRes = await sumReq.query(`SELECT COUNT(*) AS TotalOrders, AVG(CAST(DATEDIFF(day, ProofSentDate, ApprovalDate) AS FLOAT)) AS AvgProofDays, MIN(DATEDIFF(day, ProofSentDate, ApprovalDate)) AS MinDays, MAX(DATEDIFF(day, ProofSentDate, ApprovalDate)) AS MaxDays FROM orderPerformance ${baseWhere}`);
        let trendSql = null;
        if (groupBy === "week") {
          trendSql = `SELECT CAST(DATEADD(day, -(DATEPART(weekday, ProofSentDate)-1), CAST(ProofSentDate AS DATE)) AS VARCHAR(10)) AS Period, COUNT(*) AS Orders, AVG(CAST(DATEDIFF(day, ProofSentDate, ApprovalDate) AS FLOAT)) AS AvgProofDays FROM orderPerformance ${baseWhere} GROUP BY DATEADD(day, -(DATEPART(weekday, ProofSentDate)-1), CAST(ProofSentDate AS DATE)) ORDER BY Period`;
        } else if (groupBy === "month") {
          trendSql = `SELECT FORMAT(ProofSentDate, 'yyyy-MM') AS Period, COUNT(*) AS Orders, AVG(CAST(DATEDIFF(day, ProofSentDate, ApprovalDate) AS FLOAT)) AS AvgProofDays FROM orderPerformance ${baseWhere} GROUP BY FORMAT(ProofSentDate, 'yyyy-MM') ORDER BY Period`;
        }
        const trendReq = pool.request().input("from_date", sql.Date, fromDate).input("to_date", sql.Date, toDate);
        const trendRes = trendSql ? await trendReq.query(trendSql) : { recordset: [] };
        const round1 = v => v != null ? Math.round(v * 10) / 10 : null;
        const s = sumRes.recordset[0] ?? {};
        result = {
          date_range: { from: fromDate, to: toDate },
          summary: { total_orders: s.TotalOrders, avg_proof_days: round1(s.AvgProofDays), min_days: s.MinDays, max_days: s.MaxDays },
          breakdown: trendRes.recordset.map(r => ({ period: r.Period, orders: r.Orders, avg_proof_days: round1(r.AvgProofDays) })),
        };
      }

    } else {
      throw new Error(`Unknown tool: ${name}`);
    }

    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  } catch (err) {
    return { content: [{ type: "text", text: `Error: ${err.message}` }], isError: true };
  }
});

const app = express();
app.use(express.json());
app.use((req, res, next) => {
  const apiKey = process.env.WEBHOOK_API_KEY;
  if (!apiKey) return next();
  const provided = req.headers["x-api-key"] || req.query.apiKey;
  if (provided !== apiKey) return res.status(401).json({ error: "Unauthorized" });
  next();
});
app.post("/webhook/search", (req, res) => { try { res.json(searchProducts(req.body)); } catch (err) { res.status(400).json({ error: err.message }); } });
app.get("/webhook/search", (req, res) => {
  try {
    const params = { ...req.query, limit: req.query.limit ? Number(req.query.limit) : undefined, minPrice: req.query.minPrice ? Number(req.query.minPrice) : undefined, maxPrice: req.query.maxPrice ? Number(req.query.maxPrice) : undefined, onSale: req.query.onSale === "true" ? true : req.query.onSale === "false" ? false : undefined };
    res.json(searchProducts(params));
  } catch (err) { res.status(400).json({ error: err.message }); }
});
app.get("/webhook/product/:prodNo", (req, res) => {
  const result = getProductByNo(req.params.prodNo);
  if (!result) return res.status(404).json({ error: `No product found: ${req.params.prodNo}` });
  res.json(result);
});
app.get("/webhook/catalog", (req, res) => { res.json(getCatalogStats()); });
app.get("/webhook/health", (req, res) => { res.json({ status: "ok", ...getCatalogStats(), timestamp: new Date().toISOString() }); });

const PORT = process.env.PORT || 3000;
if (process.env.MCP_ONLY !== "true") {
  app.listen(PORT, () => {
    console.error("Product Search MCP server running on port " + PORT);
  });
}

const transport = new StdioServerTransport();
await server.connect(transport);
