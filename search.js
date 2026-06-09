import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import Fuse from "fuse.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

// Load product catalog at startup
let products = [];
try {
  products = JSON.parse(readFileSync(join(__dirname, "products.json"), "utf8"));
} catch (err) {
  console.error("ERROR: Failed to load products.json:", err.message);
}

// Fuse.js configuration — weighted fields for best relevance
const fuse = new Fuse(products, {
  includeScore: true,
  threshold: 0.65,
  minMatchCharLength: 2,
  ignoreLocation: true,
  keys: [
    { name: "ProdNo",           weight: 3.0 },
    { name: "Title",            weight: 2.5 },
    { name: "Brand",            weight: 2.0 },
    { name: "Category",         weight: 1.0 },
    { name: "LongDescription",  weight: 0.8 },
    { name: "Colors",           weight: 0.4 },
    { name: "SizeScale",        weight: 0.2 }
  ]
});

/**
 * Convert Fuse.js score (0=perfect, 1=no match) to a SimilarityScore (0–1, higher=better).
 * Fuse threshold is 0.65, so raw scores range 0..0.65 for returned results.
 * We map that to roughly 0.65..0.90 to match the expected schema range.
 */
function toSimilarity(fuseScore) {
  const clamped = Math.min(fuseScore, 0.65);
  const similarity = 0.65 + ((0.65 - clamped) / 0.65) * 0.25;
  return parseFloat(similarity.toFixed(9));
}

function getFieldStr(field) {
  if (Array.isArray(field)) return field.join(" ");
  return String(field ?? "");
}

/**
 * Main search function. Returns an array matching the exact JSON schema.
 *
 * @param {object} params
 * @param {string}  params.query       - Natural language search string
 * @param {number}  [params.limit=10]  - Max results
 * @param {string}  [params.brand]     - Filter by brand (partial match)
 * @param {string}  [params.category]  - Filter by category (partial match)
 * @param {number}  [params.minPrice]  - Filter by PriceLow >= minPrice
 * @param {number}  [params.maxPrice]  - Filter by PriceHigh <= maxPrice
 * @param {boolean} [params.onSale]    - Filter to sale items only
 * @param {string}  [params.color]     - Filter by color name (partial match)
 * @param {string}  [params.sort]      - "default" | "most popular"
 */
export function searchProducts({
  query = "",
  limit = 10,
  brand,
  category,
  minPrice,
  maxPrice,
  onSale,
  color,
  sort = "default"
} = {}) {
  const q = (query ?? "").trim();
  let scored;

  if (q) {
    // Fuzzy search with Fuse.js
    const fuseResults = fuse.search(q, { limit: 500 });
    scored = fuseResults.map((r) => ({
      product: r.item,
      SimilarityScore: toSimilarity(r.score ?? 0.65)
    }));
  } else {
    // No query — return all products with a neutral score
    scored = products.map((p) => ({
      product: p,
      SimilarityScore: 0.75
    }));
  }

  // ── Filters ──────────────────────────────────────────────────────────────

  if (brand) {
    const b = brand.toLowerCase();
    scored = scored.filter((r) =>
      getFieldStr(r.product.Brand).toLowerCase().includes(b)
    );
  }

  if (category) {
    const c = category.toLowerCase();
    scored = scored.filter((r) =>
      getFieldStr(r.product.Category).toLowerCase().includes(c)
    );
  }

  if (onSale === true || onSale === "true") {
    scored = scored.filter((r) => r.product["on-sale"] === "true");
  } else if (onSale === false || onSale === "false") {
    scored = scored.filter((r) => r.product["on-sale"] !== "true");
  }

  if (minPrice !== undefined && minPrice !== null) {
    const min = Number(minPrice);
    scored = scored.filter((r) => (r.product.PriceLow ?? 0) >= min);
  }

  if (maxPrice !== undefined && maxPrice !== null) {
    const max = Number(maxPrice);
    scored = scored.filter((r) => (r.product.PriceHigh ?? 99999) <= max);
  }

  if (color) {
    const col = color.toLowerCase();
    scored = scored.filter(
      (r) =>
        Array.isArray(r.product.Colors) &&
        r.product.Colors.some((c) => c.toLowerCase().includes(col))
    );
  }

  // ── Sort ─────────────────────────────────────────────────────────────────

  if (sort === "most popular") {
    scored.sort((a, b) => {
      const aR = a.product.popularRank ?? 999999;
      const bR = b.product.popularRank ?? 999999;
      return aR !== bR ? aR - bR : b.SimilarityScore - a.SimilarityScore;
    });
  } else {
    // Default: sort by similarity descending
    scored.sort((a, b) => b.SimilarityScore - a.SimilarityScore);
  }

  // ── Format output to match exact JSON schema ──────────────────────────────

  return scored.slice(0, limit).map((r, index) => {
    const p = r.product;
    const out = { ...p };

    // Always overwrite computed/dynamic fields
    out.SimilarityScore = r.SimilarityScore;
    out._sortKeys = {
      defaultIndex: index,
      mostPopular: p.popularRank ?? 999999
    };
    out._availableSorts = ["default", "most popular"];

    return out;
  });
}

/**
 * Look up a single product by exact product number (case-insensitive).
 */
export function getProductByNo(prodNo) {
  const p = products.find(
    (x) => String(x.ProdNo ?? "").toLowerCase() === String(prodNo ?? "").toLowerCase()
  );
  if (!p) return null;
  return {
    ...p,
    SimilarityScore: 1.0,
    _sortKeys: { defaultIndex: 0, mostPopular: p.popularRank ?? 999999 },
    _availableSorts: ["default", "most popular"]
  };
}

/**
 * Return catalog statistics (brands, categories, total count).
 */
export function getCatalogStats() {
  const brands = [...new Set(products.map((p) => getFieldStr(p.Brand)))].sort();
  const categories = [...new Set(products.map((p) => getFieldStr(p.Category)))].sort();
  return { totalProducts: products.length, brands, categories };
}
