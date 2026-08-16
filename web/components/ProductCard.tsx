"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatPrice, formatRelativeTime } from "@/lib/format";
import type { AlertRule, CompetitorWithPrice, PriceSnapshot, ProductWithLatestPrice } from "@/lib/types";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { PriceChart } from "./PriceChart";
import { CompetitorTable } from "./CompetitorTable";
import { AlertRulesList } from "./AlertRulesList";

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function ProductCard({ product }: { product: ProductWithLatestPrice }) {
  const [history, setHistory] = useState<PriceSnapshot[] | null>(null);
  const [competitors, setCompetitors] = useState<CompetitorWithPrice[] | null>(null);
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [detailsError, setDetailsError] = useState<string | null>(null);

  const [checking, setChecking] = useState(false);
  const [checkMessage, setCheckMessage] = useState<string | null>(null);

  const loadDetails = useCallback(async () => {
    const results = await Promise.allSettled([
      api.getHistory(product.id),
      api.listCompetitors(product.id),
      api.listAlertRules(product.id),
    ]);
    const [historyResult, competitorsResult, rulesResult] = results;

    if (results.every((r) => r.status === "rejected")) {
      const firstRejected = results.find((r) => r.status === "rejected") as PromiseRejectedResult;
      setDetailsError(
        firstRejected.reason instanceof ApiError
          ? firstRejected.reason.message
          : "Could not load this product's details."
      );
      return;
    }
    setDetailsError(null);
    if (historyResult.status === "fulfilled") setHistory(historyResult.value);
    if (competitorsResult.status === "fulfilled") setCompetitors(competitorsResult.value);
    if (rulesResult.status === "fulfilled") setRules(rulesResult.value);
  }, [product.id]);

  useEffect(() => {
    loadDetails();
  }, [loadDetails]);

  async function handleCheckNow() {
    setChecking(true);
    setCheckMessage(null);
    const previousLatest = history?.[0]?.scraped_at;
    try {
      await api.triggerScrape(product.id);
      setCheckMessage("Queued — a real browser has to load the page, this can take several seconds.");

      // A light poll rather than a full state-management setup: scraping
      // is async on the backend (worker picks the job off a queue), so
      // there's no single request/response to await here. Give it a few
      // chances to land before giving up and asking the user to check
      // back — no dependency needed for something this small.
      for (let attempt = 0; attempt < 5; attempt += 1) {
        await sleep(3000);
        const latest = await api.getHistory(product.id);
        if (latest[0]?.scraped_at !== previousLatest) {
          setHistory(latest);
          setCheckMessage("Updated just now.");
          return;
        }
      }
      setCheckMessage("Still processing — refresh in a moment to see the result.");
    } catch (err) {
      setCheckMessage(err instanceof ApiError ? err.message : "Could not queue a check right now.");
    } finally {
      setChecking(false);
    }
  }

  async function handleDeleteRule(ruleId: number) {
    await api.deleteAlertRule(product.id, ruleId);
    setRules((prev) => prev?.filter((r) => r.id !== ruleId) ?? null);
  }

  async function handleRemoveCompetitor(competitorProductId: number) {
    await api.removeCompetitor(product.id, competitorProductId);
    setCompetitors(
      (prev) => prev?.filter((c) => c.competitor_product_id !== competitorProductId) ?? null
    );
  }

  // Prefer the card's own fresher history fetch once it's in; fall back
  // to the summary the dashboard's product list already had, so there's
  // no flash of "no price" while this card's own requests are in flight.
  const current = history?.[0]
    ? {
        price: history[0].price,
        currency: history[0].currency,
        in_stock: history[0].in_stock,
        scraped_at: history[0].scraped_at,
      }
    : product.latest_price;

  return (
    <div className="rounded-card border border-border bg-surface p-6 shadow-card transition-shadow duration-150 ease-calm hover:shadow-card-hover">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <a
            href={product.daraz_url}
            target="_blank"
            rel="noreferrer"
            className="font-display text-lg text-ink transition-colors duration-150 hover:text-accent"
          >
            {product.name}
          </a>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            {current ? (
              <>
                <span className="font-figure text-base font-medium text-ink">
                  {formatPrice(current.price, current.currency)}
                </span>
                <Badge tone={current.in_stock ? "accent" : "danger"}>
                  {current.in_stock ? "In stock" : "Out of stock"}
                </Badge>
                <span className="text-ink-faint">checked {formatRelativeTime(current.scraped_at)}</span>
              </>
            ) : (
              <span className="text-ink-faint">Not checked yet.</span>
            )}
          </div>
        </div>
        <div className="text-right">
          <Button variant="secondary" size="sm" loading={checking} onClick={handleCheckNow}>
            Check now
          </Button>
          {checkMessage && <p className="mt-1.5 max-w-[16rem] text-xs text-ink-muted">{checkMessage}</p>}
        </div>
      </div>

      {detailsError ? (
        <p className="mt-6 text-sm text-danger">{detailsError}</p>
      ) : (
        <div className="mt-6 space-y-6">
          <div>
            {history === null ? (
              <Skeleton className="h-32 w-full" />
            ) : (
              <PriceChart snapshots={history} />
            )}
          </div>

          {competitors !== null && competitors.length > 0 && (
            <div>
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
                Competitors
              </h4>
              <CompetitorTable competitors={competitors} onRemove={handleRemoveCompetitor} />
            </div>
          )}

          <div>
            <h4 className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-faint">
              Alert rules
            </h4>
            {rules === null ? (
              <Skeleton className="h-9 w-full" />
            ) : (
              <AlertRulesList rules={rules} onDelete={handleDeleteRule} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
