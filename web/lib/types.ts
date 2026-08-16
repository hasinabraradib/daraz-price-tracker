// Mirrors api/app/schemas.py. Kept as plain types (not generated) since
// the API surface is small and stable enough that a codegen step would
// be more ceremony than the hand-sync it's saving.

export interface Product {
  id: number;
  name: string;
  daraz_url: string;
  created_at: string;
  is_active: boolean;
  owner_email: string | null;
}

export interface LatestPrice {
  price: string;
  currency: string;
  in_stock: boolean;
  scraped_at: string;
}

export interface ProductWithLatestPrice extends Product {
  latest_price: LatestPrice | null;
}

export interface PriceSnapshot {
  id: number;
  product_id: number;
  price: string;
  currency: string;
  in_stock: boolean;
  scraped_at: string;
  raw_title: string | null;
}

export interface ScrapeQueuedResponse {
  queued: boolean;
  queue_depth: number;
}

export type RuleType = "undercut" | "price_below" | "price_drop_pct" | "back_in_stock";
export type Channel = "email" | "webhook";

export interface AlertRule {
  id: number;
  product_id: number;
  rule_type: RuleType;
  threshold_price: string | null;
  threshold_pct: string | null;
  channel: Channel;
  destination: string;
  is_active: boolean;
  created_at: string;
  owner_email: string | null;
}

export interface AlertRuleCreate {
  rule_type: RuleType;
  threshold_price?: string;
  threshold_pct?: string;
  channel: Channel;
  destination: string;
}

export interface AlertEvent {
  id: number;
  alert_rule_id: number;
  triggered_at: string;
  resolved_at: string | null;
  trigger_price: string | null;
  competitor_price: string | null;
  competitor_product_id: number | null;
  message: string;
  delivery_status: "pending" | "sent" | "failed";
  delivery_error: string | null;
}

export interface CompetitorWithPrice {
  id: number;
  competitor_product_id: number;
  competitor_name: string;
  competitor_daraz_url: string;
  latest_price: string | null;
  currency: string | null;
  gap: string | null;
  gap_pct: number | null;
  created_at: string;
}
