"use client";

import { useState } from "react";
import type { CompetitorWithPrice } from "@/lib/types";
import { formatPrice } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";

export function CompetitorTable({
  competitors,
  onRemove,
}: {
  competitors: CompetitorWithPrice[];
  // Takes the competitor's own product id, not the link row's id — that's
  // what DELETE /products/{id}/competitors/{competitor_id} actually
  // matches on (see api/app/routers/competitors.py::remove_competitor),
  // despite the path segment being named ambiguously.
  onRemove: (competitorProductId: number) => Promise<void>;
}) {
  const [removingId, setRemovingId] = useState<number | null>(null);

  if (competitors.length === 0) return null;

  const cheapestPrice = competitors.reduce<number | null>((min, c) => {
    if (c.latest_price === null) return min;
    const price = parseFloat(c.latest_price);
    return min === null || price < min ? price : min;
  }, null);

  async function handleRemove(competitorProductId: number) {
    setRemovingId(competitorProductId);
    try {
      await onRemove(competitorProductId);
    } finally {
      setRemovingId(null);
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-ink-faint">
            <th className="pb-2 font-medium">Competitor</th>
            <th className="pb-2 font-medium">Price</th>
            <th className="pb-2 font-medium">Gap</th>
            <th className="pb-2 font-medium" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {competitors.map((c) => {
            const price = c.latest_price ? parseFloat(c.latest_price) : null;
            const isCheapest = price !== null && price === cheapestPrice;
            return (
              <tr key={c.id}>
                <td className="py-2 pr-2">
                  <a
                    href={c.competitor_daraz_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-ink transition-colors duration-150 hover:text-accent"
                  >
                    {c.competitor_name}
                  </a>
                  {isCheapest && (
                    <Badge tone="accent" className="ml-2">
                      cheapest
                    </Badge>
                  )}
                </td>
                <td className="py-2 pr-2 font-figure">
                  {c.latest_price && c.currency ? formatPrice(c.latest_price, c.currency) : "—"}
                </td>
                <td className="py-2 pr-2 font-figure">
                  {c.gap !== null ? (
                    <span className={parseFloat(c.gap) > 0 ? "text-danger" : "text-accent"}>
                      {parseFloat(c.gap) > 0 ? "+" : ""}
                      {parseFloat(c.gap).toFixed(2)}
                      {c.gap_pct !== null && ` (${c.gap_pct.toFixed(1)}%)`}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="py-2 text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={removingId === c.competitor_product_id}
                    onClick={() => handleRemove(c.competitor_product_id)}
                  >
                    Remove
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
