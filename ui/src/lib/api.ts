import { keepPreviousData, useQuery } from "@tanstack/react-query";
import type * as T from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

type Params = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

function qs(params?: Params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v == null || v === "") continue;
    (Array.isArray(v) ? v : [v]).forEach((x) => u.append(k, String(x)));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

let tokenGetter: (() => Promise<string | null>) | null = null;
export const setTokenGetter = (f: (() => Promise<string | null>) | null) => { tokenGetter = f; };

export async function api<R>(path: string, params?: Params, init?: RequestInit): Promise<R> {
  const token = tokenGetter ? await tokenGetter() : null;
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`/api${path}${qs(params)}`, { ...init, headers });
  if (!res.ok) {
    let msg: unknown = res.statusText;
    try { msg = (await res.json()).detail ?? msg; } catch { /* body kosong */ }
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  if (res.status === 204) return undefined as R;
  return res.json();
}

const MIN = 60_000;
// staleTime mengikuti ttl di dashboard.py (300 dtk data, 600 dtk metadata)
function useGet<R>(key: unknown[], path: string, params?: Params, o: { min?: number; keep?: boolean; enabled?: boolean } = {}) {
  return useQuery({
    queryKey: key,
    queryFn: ({ signal }) => api<R>(path, params, { signal }),
    staleTime: (o.min ?? 5) * MIN,
    enabled: o.enabled ?? true,
    placeholderData: o.keep ? keepPreviousData : undefined,
  });
}

export const useMeta = () =>
  useQuery({ queryKey: ["meta"], queryFn: ({ signal }) => api<T.Meta>("/meta", undefined, { signal }), staleTime: 30_000, refetchInterval: 60_000, retry: 0 });
export const useTickers = () => useGet<T.Ticker[]>(["tickers"], "/tickers", undefined, { min: 10 });
export const usePrices = (t: string) => useGet<T.Prices>(["prices", t], `/emiten/${t}/prices`);
export const useTickerSignals = (t: string) => useGet<T.SignalMark[]>(["tsig", t], `/emiten/${t}/signals`);
export const useFinancials = (t: string) => useGet<T.Financials>(["fin", t], `/emiten/${t}/financials`);
export const useScanHistory = (t: string) => useGet<T.HistRow[]>(["hist", t], `/emiten/${t}/scans`);
export const usePerformance = (p: Params) => useGet<T.Performance>(["perf", p], "/signals/performance", p, { keep: true });
export const useScanPeriods = () => useGet<T.ScanPeriod[]>(["periods"], "/scans/periods");
export const useScanResults = (year?: number, period?: string) =>
  useGet<T.ScanResults>(["scan", year, period], `/scans/${year}/${period}`, undefined, { enabled: year != null && !!period });
export const useMacroOverview = () => useGet<T.MacroRow[]>(["macro-ov"], "/macro/overview", undefined, { min: 10 });
export const useMacroSeries = (keys: string[], years: number | null) =>
  useGet<T.MacroSeries>(["macro", keys, years], "/macro/series", { keys, years }, { keep: true, min: 10, enabled: keys.length > 0 });
export const useStatus = () => useGet<T.StatusData>(["status"], "/status", undefined, { min: 1 });

export function useRegime(ticker: string, p: T.RegimeParams, days: number | null, enabled: boolean) {
  return useQuery({
    queryKey: ["regime", ticker, p, days],
    queryFn: ({ signal }) => api<T.RegimeResponse>(`/regime/${ticker}`, { ...p, days }, { signal }),
    enabled,
    staleTime: 60 * MIN,
    retry: 0,
    placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.state === "running" ? 2000 : false), // polling selama job berjalan
  });
}