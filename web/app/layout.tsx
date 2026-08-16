import type { Metadata } from "next";
import { Fraunces, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";

// Three typographic roles instead of one default font: a serif for
// headings (Fraunces — warm, editorial, not the "default SaaS" feel), a
// clean sans for body/UI text (IBM Plex Sans), and a monospace for prices
// and other figures specifically (IBM Plex Mono) — numbers get tabular
// alignment and a slightly ledger-like, precise feel that sets them apart
// from surrounding prose.
const display = Fraunces({
  subsets: ["latin"],
  variable: "--font-display",
  weight: ["500", "600"],
  style: ["normal", "italic"],
});
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  weight: ["400", "500", "600"],
});
const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Daraz Price Tracker",
  description: "Track Daraz product prices, competitors, and stock — get alerted the moment something changes.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${display.variable} ${sans.variable} ${mono.variable} bg-paper font-sans text-ink antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
