# web/ — Daraz Price Tracker frontend

A minimal Next.js 14 (App Router) + TypeScript + Tailwind frontend for the
tracker — built so someone who won't read the backend code can see the
whole thing actually working: add a product, link a competitor, set up an
alert rule, trigger a real scrape, and watch real price data show up.
Not a full product surface — three pages, plain `useState`/`fetch`, no
auth library, no state-management library. See the repo root
[`README.md`](../README.md) for what the system underneath it does and why.

## Running it

```bash
cd web
npm install
cp .env.local.example .env.local   # NEXT_PUBLIC_API_URL, defaults to localhost:8000
npm run dev
```

Open http://localhost:3000. It needs the API running and reachable at
`NEXT_PUBLIC_API_URL` (see the repo root's **Getting started** —
`docker compose up --build` from the repo root, or `uvicorn` directly).
The API's CORS allowlist (`api/app/main.py`) also needs `FRONTEND_ORIGIN`
set to wherever this is actually running — it defaults to
`http://localhost:3000`, matching this app's own default port, so local
dev needs no extra config either side.

## Pages

- **`/setup`** — email + Discord webhook URL, both saved to
  `localStorage`. Includes a "Send test alert" button (hits
  `POST /alerts/test-webhook`) so you can confirm the webhook actually
  works before creating anything that depends on it, and an expandable
  walkthrough for getting a Discord webhook URL in the first place. First
  visit with no email saved redirects here automatically.
- **`/products/new`** — add a product by Daraz URL (validated client-side
  against the same URL shape the backend accepts, before it ever hits the
  API), optionally link a competitor by URL, optionally set up an alert
  rule in the same step. Every rule type gets a one-sentence plain-English
  explanation inline — no assumption you know what "edge-triggered" means.
- **`/`** — the dashboard. One card per product: current price, stock
  status, last-checked time, a price history line chart (recharts),
  linked competitors with the cheapest one flagged, active alert rules
  (deletable inline), and a manual "Check now" button that queues a real
  scrape and polls briefly for the result. A "Recent alerts" panel below
  the cards shows what's fired across everything you're tracking.

## How "linking a competitor by URL" actually works

The backend's `POST /products/{id}/competitors` takes a **product id**,
not a URL — there was no way to go from a URL to an id without one more
small backend addition made alongside this frontend:
`GET /products?daraz_url=...` (see `api/app/routers/products.py`), an
exact-match lookup. The add-product page uses it to check whether a
product for that URL already exists before creating one, so linking a
competitor that's already tracked doesn't create a duplicate row.

One consequence worth knowing, not a bug: a competitor product created
this way is a **real, independently tracked `Product`** — it gets its own
dashboard card, its own price history, and could have its own alert
rules, exactly like anything added directly. There's no lighter-weight
"just a reference" concept in the data model; linking a competitor means
the system starts tracking it too.

## "Not authentication" — read this before treating it as such

`X-Owner-Email` is a plain header the frontend sends on every request,
sourced from whatever's in `localStorage` — **there is no password, no
token, no verification that you control that email address.** Setup
doesn't check it's real; nothing stops you opening devtools and changing
`daraz_tracker_owner_email` to anyone else's email to see their products.
It exists only so multiple people can demo this at once without each
seeing everyone else's data by default — see `api/app/deps.py`'s
`get_owner_email` docstring for the full reasoning, and the root
README's **What I'd do next** for what real auth here would actually
require (a `User` model, real sessions/JWTs, every query scoped to a
verified identity — none of which exists today).

Products/alert rules created before this existed (or created with no
header sent) have `owner_email = NULL` and are visible to everyone,
unfiltered — "fail open," not "fail closed," on purpose for a demo where
losing access to your own data because of a typo would be a worse
experience than mild oversharing.

## Design notes

Deliberately not default Tailwind: a warm off-white/near-black palette
with one accent color (deep teal-green — chosen for "tracking savings"
without being the obvious `blue-500`), three typographic roles instead of
one font (Fraunces serif for headings, IBM Plex Sans for body/UI, IBM
Plex Mono with tabular numerals specifically for prices — see
`app/layout.tsx` and the `.font-figure` utility in `app/globals.css`),
generous card padding, and restrained transitions (`duration-150`,
nothing bouncy). Every list-shaped view (product cards, alert rules,
competitors, alert events) has a real empty state explaining what to do
next rather than saying "no data," and every fetch has a loading skeleton
and an error state that shows the actual message from the API
(`lib/api.ts`'s `ApiError`), not a generic "something went wrong."

## Structure

```
web/
├── app/
│   ├── layout.tsx          # fonts, metadata
│   ├── globals.css         # palette tokens, base typography
│   ├── page.tsx            # dashboard
│   ├── setup/page.tsx
│   └── products/new/page.tsx
├── components/
│   ├── ui/                 # Button, Input, Select, Card, Badge, Skeleton, EmptyState, ErrorState
│   ├── Header.tsx
│   ├── ProductCard.tsx     # composes the four pieces below per product
│   ├── PriceChart.tsx      # recharts line chart
│   ├── CompetitorTable.tsx
│   ├── AlertRulesList.tsx
│   └── AlertEventsFeed.tsx
└── lib/
    ├── api.ts              # fetch wrapper: base URL, X-Owner-Email header, ApiError
    ├── types.ts            # mirrors api/app/schemas.py
    ├── storage.ts           # localStorage read/write for email + webhook
    ├── format.ts            # price/date formatting, rule-type labels & explanations
    ├── validation.ts        # client-side Daraz URL shape check
    └── cn.ts                # tiny classnames helper (no dependency pulled in for this)
```

## Verified live

Ran against the real backend (not mocked): set up an email + a real
Discord webhook and sent a real test alert through it, added a product
with a linked competitor and a `price_below` alert rule through the UI,
landed on the dashboard, clicked "Check now" on the tracker's original
real product and watched a genuine Playwright scrape complete and the
card update to the new price/timestamp with no manual refresh, removed a
competitor link and deleted an alert rule and watched both disappear from
the UI. Checked at 390px mobile width. `npx tsc --noEmit` and
`npm run build` both clean.
