"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { PriceSnapshot } from "@/lib/types";

function CustomTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: { price: number; scraped_at: string; currency: string } }>;
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-card-hover">
      <p className="font-figure font-medium text-ink">
        {point.currency} {point.price.toFixed(2)}
      </p>
      <p className="text-ink-faint">{new Date(point.scraped_at).toLocaleDateString()}</p>
    </div>
  );
}

export function PriceChart({ snapshots }: { snapshots: PriceSnapshot[] }) {
  if (snapshots.length < 2) {
    return (
      <div className="flex h-32 items-center justify-center rounded-md bg-black/[0.02] text-xs text-ink-faint">
        Not enough history yet — check a couple more times to see a trend.
      </div>
    );
  }

  // snapshots arrive newest-first from the API; charts read left-to-right
  // oldest-first.
  const data = [...snapshots]
    .reverse()
    .map((s) => ({ price: parseFloat(s.price), scraped_at: s.scraped_at, currency: s.currency }));

  return (
    <div className="h-32 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 8 }}>
          <XAxis dataKey="scraped_at" hide />
          <YAxis domain={["auto", "auto"]} hide />
          <Tooltip content={<CustomTooltip />} />
          <Line
            type="monotone"
            dataKey="price"
            stroke="#1F6F5C"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: "#1F6F5C" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
