"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getOwnerEmail } from "@/lib/storage";
import { Button } from "./ui/Button";

export function Header({ showAddProduct = false }: { showAddProduct?: boolean }) {
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    setEmail(getOwnerEmail());
  }, []);

  return (
    <header className="border-b border-border">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-5">
        <Link href="/" className="font-display text-lg font-semibold text-ink">
          Daraz Price Tracker
        </Link>
        <div className="flex items-center gap-4">
          {email && (
            <Link
              href="/setup"
              className="hidden text-sm text-ink-muted transition-colors duration-150 hover:text-ink sm:inline"
              title="Change email or webhook"
            >
              {email}
            </Link>
          )}
          {showAddProduct && (
            <Link href="/products/new">
              <Button size="sm">+ Add product</Button>
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
