"use client";

import { useState } from "react";
import type { AlertRule } from "@/lib/types";
import { RULE_TYPE_LABELS } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";

function ruleSummary(rule: AlertRule): string {
  if (rule.rule_type === "price_below" && rule.threshold_price) {
    return `below ${rule.threshold_price}`;
  }
  if (rule.rule_type === "price_drop_pct" && rule.threshold_pct) {
    return `drops by more than ${rule.threshold_pct}%`;
  }
  return RULE_TYPE_LABELS[rule.rule_type] ?? rule.rule_type;
}

export function AlertRulesList({
  rules,
  onDelete,
}: {
  rules: AlertRule[];
  onDelete: (ruleId: number) => Promise<void>;
}) {
  const [deletingId, setDeletingId] = useState<number | null>(null);

  if (rules.length === 0) {
    return <p className="text-sm text-ink-faint">No alert rules yet for this product.</p>;
  }

  async function handleDelete(ruleId: number) {
    setDeletingId(ruleId);
    try {
      await onDelete(ruleId);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <ul className="space-y-2">
      {rules.map((rule) => (
        <li
          key={rule.id}
          className="flex flex-col gap-2 rounded-md border border-border px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge tone={rule.is_active ? "accent" : "neutral"}>
              {RULE_TYPE_LABELS[rule.rule_type] ?? rule.rule_type}
            </Badge>
            <span className="text-ink-muted">{ruleSummary(rule)}</span>
            <span className="text-ink-faint">via {rule.channel}</span>
          </div>
          <Button
            variant="ghost"
            size="sm"
            loading={deletingId === rule.id}
            onClick={() => handleDelete(rule.id)}
            className="self-end sm:self-auto"
          >
            Delete
          </Button>
        </li>
      ))}
    </ul>
  );
}
