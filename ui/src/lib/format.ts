const cache = new Map<string, Intl.NumberFormat>();
function nf(d: number, signed = false) {
  const k = `${d}|${signed}`;
  let f = cache.get(k);
  if (!f) {
    f = new Intl.NumberFormat("id-ID", { minimumFractionDigits: d, maximumFractionDigits: d, signDisplay: signed ? "exceptZero" : "auto" });
    cache.set(k, f);
  }
  return f;
}
const ok = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export const num = (v: number | null | undefined, d = 1) => (ok(v) ? nf(d).format(v) : "-");
export const pct = (v: number | null | undefined, d = 1, signed = false) => (ok(v) ? `${nf(d, signed).format(v * 100)}%` : "-");
export const compact = (v: number) => new Intl.NumberFormat("id-ID", { notation: "compact" }).format(v);

const dfmt = new Intl.DateTimeFormat("id-ID", { day: "2-digit", month: "short", year: "numeric" });
export const day = (s?: string | null) => (s ? dfmt.format(new Date(`${s.slice(0, 10)}T00:00:00`)) : "-");
export const stampLabel = (s: string) => `${s.slice(6, 8)}/${s.slice(4, 6)}/${s.slice(0, 4)} ${s.slice(9, 11)}:${s.slice(11, 13)}`;
export const daysAgo = (n: number) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
export const tone = (v: number | null | undefined) => (!ok(v) || v === 0 ? "" : v > 0 ? "up" : "down");

/** Indeks pertama dengan a[i] >= t (array tanggal ISO terurut). */
export function lowerBound(a: string[], t: string) {
  let lo = 0, hi = a.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < t) lo = m + 1; else hi = m; }
  return lo;
}

export function downloadCsv(name: string, header: string[], rows: unknown[][]) {
  const esc = (v: unknown) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s; };
  const blob = new Blob(["\uFEFF" + [header, ...rows].map((r) => r.map(esc).join(",")).join("\n")], { type: "text/csv;charset=utf-8" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
  a.click();
  URL.revokeObjectURL(a.href);
}