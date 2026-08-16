import { getOwnerEmail } from "./storage";
import type {
  AlertEvent,
  AlertRule,
  AlertRuleCreate,
  CompetitorWithPrice,
  PriceSnapshot,
  Product,
  ProductWithLatestPrice,
  ScrapeQueuedResponse,
} from "./types";

const API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function extractErrorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      // FastAPI/Pydantic validation error shape: a list of {msg, loc, ...}
      return body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ");
    }
  } catch {
    // response body wasn't JSON (or was empty) — fall through to the
    // generic status-based message below.
  }
  return response.statusText || `request failed with status ${response.status}`;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  const ownerEmail = getOwnerEmail();
  if (ownerEmail) headers.set("X-Owner-Email", ownerEmail);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch {
    // fetch() itself throws on network failure (backend down, CORS
    // blocked, DNS, ...) — before there's any HTTP response to inspect.
    throw new ApiError(
      `Could not reach the API at ${API_BASE}. Is the backend running?`,
      0
    );
  }

  if (!response.ok) {
    throw new ApiError(await extractErrorDetail(response), response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function toQueryString(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined) as [string, string | number][];
  if (entries.length === 0) return "";
  return `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}`;
}

export const api = {
  listProducts: () => request<ProductWithLatestPrice[]>("/products"),

  findProductByUrl: (darazUrl: string) =>
    request<ProductWithLatestPrice[]>(`/products${toQueryString({ daraz_url: darazUrl })}`),

  createProduct: (payload: { name: string; daraz_url: string }) =>
    request<Product>("/products", { method: "POST", body: JSON.stringify(payload) }),

  getHistory: (productId: number) =>
    request<PriceSnapshot[]>(`/products/${productId}/history`),

  triggerScrape: (productId: number) =>
    request<ScrapeQueuedResponse>(`/products/${productId}/scrape`, { method: "POST" }),

  listCompetitors: (productId: number) =>
    request<CompetitorWithPrice[]>(`/products/${productId}/competitors`),

  addCompetitor: (productId: number, competitorProductId: number) =>
    request(`/products/${productId}/competitors`, {
      method: "POST",
      body: JSON.stringify({ competitor_product_id: competitorProductId }),
    }),

  removeCompetitor: (productId: number, competitorLinkId: number) =>
    request<void>(`/products/${productId}/competitors/${competitorLinkId}`, {
      method: "DELETE",
    }),

  listAlertRules: (productId: number) =>
    request<AlertRule[]>(`/products/${productId}/alert-rules`),

  createAlertRule: (productId: number, payload: AlertRuleCreate) =>
    request<AlertRule>(`/products/${productId}/alert-rules`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  deleteAlertRule: (productId: number, ruleId: number) =>
    request<void>(`/products/${productId}/alert-rules/${ruleId}`, { method: "DELETE" }),

  listAlerts: (params?: { product_id?: number; status?: "open" | "resolved" }) =>
    request<AlertEvent[]>(`/alerts${toQueryString(params ?? {})}`),

  testWebhook: (webhookUrl: string) =>
    request<{ sent: boolean }>("/alerts/test-webhook", {
      method: "POST",
      body: JSON.stringify({ webhook_url: webhookUrl }),
    }),
};
