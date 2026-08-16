"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getOwnerEmail, getSavedWebhookUrl, setOwnerEmail, setSavedWebhookUrl } from "@/lib/storage";
import { Header } from "@/components/Header";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";
import { InlineError } from "@/components/ui/ErrorState";

type TestState = { kind: "idle" } | { kind: "sending" } | { kind: "sent" } | { kind: "error"; message: string };

export default function SetupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [emailError, setEmailError] = useState<string | undefined>();
  const [testState, setTestState] = useState<TestState>({ kind: "idle" });

  useEffect(() => {
    setEmail(getOwnerEmail() ?? "");
    setWebhookUrl(getSavedWebhookUrl() ?? "");
  }, []);

  function handleSave(next: "products" | "dashboard") {
    if (!email.trim() || !email.includes("@")) {
      setEmailError("Enter a valid email address.");
      return;
    }
    setEmailError(undefined);
    setOwnerEmail(email);
    if (webhookUrl.trim()) setSavedWebhookUrl(webhookUrl);
    router.push(next === "products" ? "/products/new" : "/");
  }

  async function handleSendTest() {
    if (!webhookUrl.trim()) {
      setTestState({ kind: "error", message: "Paste a webhook URL first." });
      return;
    }
    setTestState({ kind: "sending" });
    try {
      await api.testWebhook(webhookUrl.trim());
      setTestState({ kind: "sent" });
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Something went wrong sending the test alert.";
      setTestState({ kind: "error", message });
    }
  }

  return (
    <div className="min-h-screen bg-paper">
      <Header />
      <main className="mx-auto max-w-xl px-6 py-14">
        <h1 className="font-display text-3xl text-ink">Get set up</h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          Two things, and you&apos;re tracking prices: an email to keep your products separate
          from anyone else using this demo, and a Discord webhook so alerts have somewhere to go.
        </p>

        <div className="mt-10 space-y-8">
          <Field
            label="Your email"
            htmlFor="email"
            error={emailError}
            hint="Not a login — just a label so your products stay grouped as yours. Anyone can type any email; see the README."
          >
            <Input
              id="email"
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              error={Boolean(emailError)}
            />
          </Field>

          <div className="space-y-3">
            <Field
              label="Discord webhook URL"
              htmlFor="webhook"
              hint="Optional for now — you can add it later when creating an alert rule."
            >
              <Input
                id="webhook"
                type="url"
                placeholder="https://discord.com/api/webhooks/…"
                value={webhookUrl}
                onChange={(e) => setWebhookUrl(e.target.value)}
              />
            </Field>

            <details className="group rounded-md border border-border bg-surface px-4 py-3 open:pb-4">
              <summary className="cursor-pointer select-none text-sm font-medium text-ink-muted transition-colors duration-150 hover:text-ink">
                How do I get a Discord webhook?
              </summary>
              <ol className="mt-3 space-y-2 text-sm leading-relaxed text-ink-muted">
                <li>1. Create a Discord server if you don&apos;t already have one (or use an existing one).</li>
                <li>2. Right-click the text channel you want alerts posted to.</li>
                <li>3. Choose <span className="text-ink">Edit Channel</span> → <span className="text-ink">Integrations</span> → <span className="text-ink">Webhooks</span>.</li>
                <li>4. Click <span className="text-ink">New Webhook</span>.</li>
                <li>5. Click <span className="text-ink">Copy Webhook URL</span> and paste it above.</li>
              </ol>
            </details>

            <div className="flex items-center gap-3">
              <Button variant="secondary" size="sm" onClick={handleSendTest} loading={testState.kind === "sending"}>
                Send test alert
              </Button>
              {testState.kind === "sent" && (
                <span className="text-xs text-accent">Sent — check your Discord channel.</span>
              )}
              {testState.kind === "error" && <InlineError message={testState.message} />}
            </div>
          </div>

          <div className="flex items-center gap-3 border-t border-border pt-6">
            <Button onClick={() => handleSave("products")}>Save and add a product</Button>
            <Button variant="ghost" onClick={() => handleSave("dashboard")}>
              Skip to dashboard
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}
