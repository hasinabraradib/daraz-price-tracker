// Client-side mirror of api/app/url_utils.py::normalize_daraz_url's shape
// check — instant feedback in the form. The backend re-validates and
// normalizes on submit regardless; this only exists so a typo doesn't
// cost a round trip to discover.
export function isValidDarazUrl(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  const labels = parsed.hostname.toLowerCase().split(".");
  return labels.includes("daraz");
}
