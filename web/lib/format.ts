export function formatPrice(price: string | number, currency: string): string {
  const value = typeof price === "string" ? parseFloat(price) : price;
  const formatted = value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${currency} ${formatted}`;
}

export function formatRelativeTime(isoString: string): string {
  const then = new Date(isoString).getTime();
  const now = Date.now();
  const diffSeconds = Math.round((now - then) / 1000);

  if (diffSeconds < 60) return "just now";
  const diffMinutes = Math.round(diffSeconds / 60);
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.round(diffHours / 24);
  if (diffDays < 30) return `${diffDays}d ago`;
  return new Date(isoString).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(isoString: string): string {
  return new Date(isoString).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export const RULE_TYPE_LABELS: Record<string, string> = {
  undercut: "Competitor undercut",
  price_below: "Price below threshold",
  price_drop_pct: "Price drop",
  back_in_stock: "Back in stock",
};

export const RULE_TYPE_EXPLANATIONS: Record<string, string> = {
  undercut: "Alerts you the moment any linked competitor's price drops below yours.",
  price_below: "Alerts you the moment this product's price drops below a price you set.",
  price_drop_pct:
    "Alerts you the moment the price drops by more than a percentage you set, compared to the last check — a one-time notice for that specific drop, not a standing condition.",
  back_in_stock: "Alerts you the moment this product goes from out of stock to back in stock.",
};
