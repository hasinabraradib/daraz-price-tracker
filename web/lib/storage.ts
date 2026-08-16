// All client-side, no cookies/session — this is deliberately not
// authentication (see api/app/deps.py::get_owner_email). The email
// stored here is sent back as a plain X-Owner-Email header on every API
// request (see api.ts); anyone can open devtools and change it.

const OWNER_EMAIL_KEY = "daraz_tracker_owner_email";
const WEBHOOK_URL_KEY = "daraz_tracker_webhook_url";

function readStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(key);
}

function writeStorage(key: string, value: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(key, value);
}

export function getOwnerEmail(): string | null {
  return readStorage(OWNER_EMAIL_KEY);
}

export function setOwnerEmail(email: string) {
  writeStorage(OWNER_EMAIL_KEY, email.trim());
}

export function getSavedWebhookUrl(): string | null {
  return readStorage(WEBHOOK_URL_KEY);
}

export function setSavedWebhookUrl(url: string) {
  writeStorage(WEBHOOK_URL_KEY, url.trim());
}
