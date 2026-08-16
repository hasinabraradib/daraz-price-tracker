"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getOwnerEmail, getSavedWebhookUrl } from "@/lib/storage";
import { isValidDarazUrl } from "@/lib/validation";
import { RULE_TYPE_EXPLANATIONS, RULE_TYPE_LABELS } from "@/lib/format";
import type { Channel, RuleType } from "@/lib/types";
import { Header } from "@/components/Header";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { InlineError } from "@/components/ui/ErrorState";

const RULE_TYPES: RuleType[] = ["price_below", "price_drop_pct", "back_in_stock", "undercut"];

export default function AddProductPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [darazUrl, setDarazUrl] = useState("");
  const [urlTouched, setUrlTouched] = useState(false);

  const [wantsCompetitor, setWantsCompetitor] = useState(false);
  const [competitorName, setCompetitorName] = useState("");
  const [competitorUrl, setCompetitorUrl] = useState("");

  const [wantsAlertRule, setWantsAlertRule] = useState(true);
  const [ruleType, setRuleType] = useState<RuleType>("price_below");
  const [thresholdPrice, setThresholdPrice] = useState("");
  const [thresholdPct, setThresholdPct] = useState("10");
  const [channel, setChannel] = useState<Channel>("webhook");
  const [destination, setDestination] = useState(getSavedWebhookUrl() ?? "");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const urlIsValid = darazUrl.trim().length === 0 || isValidDarazUrl(darazUrl.trim());

  function handleChannelChange(next: Channel) {
    setChannel(next);
    if (next === "webhook") setDestination(getSavedWebhookUrl() ?? "");
    else setDestination(getOwnerEmail() ?? "");
  }

  async function findOrCreateProductId(productName: string, url: string): Promise<number> {
    const existing = await api.findProductByUrl(url);
    if (existing.length > 0) return existing[0].id;
    const created = await api.createProduct({ name: productName, daraz_url: url });
    return created.id;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!name.trim() || !darazUrl.trim() || !isValidDarazUrl(darazUrl.trim())) {
      setUrlTouched(true);
      setError("Enter a product name and a valid Daraz product URL.");
      return;
    }
    if (wantsCompetitor && (!competitorName.trim() || !isValidDarazUrl(competitorUrl.trim()))) {
      setError("Enter a competitor name and a valid Daraz URL, or turn off the competitor toggle.");
      return;
    }
    if (wantsAlertRule) {
      if (ruleType === "price_below" && !thresholdPrice.trim()) {
        setError("Set a price threshold for the price_below rule.");
        return;
      }
      if (ruleType === "price_drop_pct") {
        const pct = parseFloat(thresholdPct);
        if (!thresholdPct.trim() || Number.isNaN(pct) || pct <= 0 || pct > 100) {
          setError("Set a drop percentage between 0 and 100.");
          return;
        }
      }
      if (!destination.trim()) {
        setError("Set where alerts should be delivered.");
        return;
      }
    }

    setSubmitting(true);
    try {
      const product = await api.createProduct({ name: name.trim(), daraz_url: darazUrl.trim() });

      if (wantsCompetitor) {
        const competitorId = await findOrCreateProductId(competitorName.trim(), competitorUrl.trim());
        await api.addCompetitor(product.id, competitorId);
      }

      if (wantsAlertRule) {
        await api.createAlertRule(product.id, {
          rule_type: ruleType,
          channel,
          destination: destination.trim(),
          ...(ruleType === "price_below" ? { threshold_price: thresholdPrice.trim() } : {}),
          ...(ruleType === "price_drop_pct" ? { threshold_pct: thresholdPct.trim() } : {}),
        });
      }

      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong adding this product.");
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-paper">
      <Header />
      <main className="mx-auto max-w-xl px-6 py-14">
        <h1 className="font-display text-3xl text-ink">Add a product</h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          Paste a Daraz product page URL. Optionally link a competitor for comparison, and set up
          an alert rule so you hear about changes without checking manually.
        </p>

        <form onSubmit={handleSubmit} className="mt-10 space-y-10">
          <div className="space-y-4">
            <Field label="Product name" htmlFor="name">
              <Input
                id="name"
                placeholder="e.g. Silicone iPhone Case"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field
              label="Daraz product URL"
              htmlFor="url"
              error={urlTouched && !urlIsValid ? "That doesn't look like a Daraz product URL." : undefined}
              hint={urlIsValid ? "e.g. https://www.daraz.com.bd/products/…-i123-s456.html" : undefined}
            >
              <Input
                id="url"
                placeholder="https://www.daraz.com.bd/products/…"
                value={darazUrl}
                onChange={(e) => setDarazUrl(e.target.value)}
                onBlur={() => setUrlTouched(true)}
                error={urlTouched && !urlIsValid}
              />
            </Field>
          </div>

          <div className="space-y-4 border-t border-border pt-6">
            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-ink">
              <input
                type="checkbox"
                checked={wantsCompetitor}
                onChange={(e) => setWantsCompetitor(e.target.checked)}
                className="h-4 w-4 rounded border-border-strong text-accent focus:ring-accent/40"
              />
              Also track a competitor, for comparison
            </label>
            {wantsCompetitor && (
              <div className="space-y-4 pl-6">
                <Field label="Competitor name" htmlFor="competitor-name">
                  <Input
                    id="competitor-name"
                    placeholder="e.g. Similar case, other seller"
                    value={competitorName}
                    onChange={(e) => setCompetitorName(e.target.value)}
                  />
                </Field>
                <Field label="Competitor Daraz URL" htmlFor="competitor-url">
                  <Input
                    id="competitor-url"
                    placeholder="https://www.daraz.com.bd/products/…"
                    value={competitorUrl}
                    onChange={(e) => setCompetitorUrl(e.target.value)}
                  />
                </Field>
              </div>
            )}
          </div>

          <div className="space-y-4 border-t border-border pt-6">
            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-ink">
              <input
                type="checkbox"
                checked={wantsAlertRule}
                onChange={(e) => setWantsAlertRule(e.target.checked)}
                className="h-4 w-4 rounded border-border-strong text-accent focus:ring-accent/40"
              />
              Set up an alert rule now
            </label>

            {wantsAlertRule && (
              <div className="space-y-4 pl-6">
                <Field label="Alert type" htmlFor="rule-type">
                  <Select
                    id="rule-type"
                    value={ruleType}
                    onChange={(e) => setRuleType(e.target.value as RuleType)}
                  >
                    {RULE_TYPES.map((rt) => (
                      <option key={rt} value={rt}>
                        {RULE_TYPE_LABELS[rt]}
                      </option>
                    ))}
                  </Select>
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-muted">
                    {RULE_TYPE_EXPLANATIONS[ruleType]}
                  </p>
                  {ruleType === "undercut" && !wantsCompetitor && (
                    <p className="mt-1 text-xs text-danger">
                      This needs a linked competitor to ever fire — turn on the toggle above.
                    </p>
                  )}
                </Field>

                {ruleType === "price_below" && (
                  <Field label="Alert when price drops below" htmlFor="threshold-price">
                    <Input
                      id="threshold-price"
                      type="number"
                      step="0.01"
                      placeholder="e.g. 1000"
                      value={thresholdPrice}
                      onChange={(e) => setThresholdPrice(e.target.value)}
                    />
                  </Field>
                )}

                {ruleType === "price_drop_pct" && (
                  <Field label="Alert when price drops by more than (%)" htmlFor="threshold-pct">
                    <Input
                      id="threshold-pct"
                      type="number"
                      step="1"
                      min="0"
                      max="100"
                      value={thresholdPct}
                      onChange={(e) => setThresholdPct(e.target.value)}
                    />
                  </Field>
                )}

                <Field label="Send alert via" htmlFor="channel">
                  <Select
                    id="channel"
                    value={channel}
                    onChange={(e) => handleChannelChange(e.target.value as Channel)}
                  >
                    <option value="webhook">Discord webhook</option>
                    <option value="email">Email</option>
                  </Select>
                </Field>

                <Field
                  label={channel === "webhook" ? "Webhook URL" : "Email address"}
                  htmlFor="destination"
                >
                  <Input
                    id="destination"
                    placeholder={channel === "webhook" ? "https://discord.com/api/webhooks/…" : "you@example.com"}
                    value={destination}
                    onChange={(e) => setDestination(e.target.value)}
                  />
                </Field>
              </div>
            )}
          </div>

          {error && <InlineError message={error} />}

          <div className="flex items-center gap-3 border-t border-border pt-6">
            <Button type="submit" loading={submitting}>
              Add product
            </Button>
            <Button type="button" variant="ghost" onClick={() => router.push("/")}>
              Cancel
            </Button>
          </div>
        </form>
      </main>
    </div>
  );
}
