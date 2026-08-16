"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getOwnerEmail } from "@/lib/storage";
import type { AlertEvent, ProductWithLatestPrice } from "@/lib/types";
import { Header } from "@/components/Header";
import { ProductCard } from "@/components/ProductCard";
import { AlertEventsFeed } from "@/components/AlertEventsFeed";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { Skeleton } from "@/components/ui/Skeleton";

type LoadState = "checking-setup" | "loading" | "loaded" | "error";

export default function DashboardPage() {
  const router = useRouter();
  const [state, setState] = useState<LoadState>("checking-setup");
  const [products, setProducts] = useState<ProductWithLatestPrice[]>([]);
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setState("loading");
    setError(null);
    try {
      const [productList, alertList] = await Promise.all([api.listProducts(), api.listAlerts()]);
      setProducts(productList);
      setAlerts(alertList);
      setState("loaded");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong loading your dashboard.");
      setState("error");
    }
  }

  useEffect(() => {
    if (!getOwnerEmail()) {
      router.replace("/setup");
      return;
    }
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (state === "checking-setup") return null;

  return (
    <div className="min-h-screen bg-paper">
      <Header showAddProduct />
      <main className="mx-auto max-w-5xl px-6 py-10">
        {state === "loading" && (
          <div className="space-y-4">
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        )}

        {state === "error" && <ErrorState message={error ?? "Something went wrong."} onRetry={load} />}

        {state === "loaded" && (
          <>
            {products.length === 0 ? (
              <EmptyState
                title="No products yet"
                description="Add a Daraz product URL to start tracking its price. You can link a competitor and set up an alert rule at the same time."
                action={
                  <Link href="/products/new">
                    <Button>Add your first product</Button>
                  </Link>
                }
              />
            ) : (
              <div className="space-y-6">
                {products.map((product) => (
                  <ProductCard key={product.id} product={product} />
                ))}
              </div>
            )}

            <section className="mt-12">
              <h2 className="font-display text-xl text-ink">Recent alerts</h2>
              <Card className="mt-4">
                <AlertEventsFeed events={alerts.slice(0, 20)} />
              </Card>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
