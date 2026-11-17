import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Num } from "./types";

export type PfSummary = { name: string; tickers: number; positions: number; firstBuy: string };
export type Holding = { id: number; portfolio: string; ticker: string; lots: number; buy_date: string };

export type Verdict = "positif" | "negatif" | "tidak_pasti" | "tidak_dapat_dihitung";
export type SharpeBlock = {
  sharpe: Num; ci_low: Num; ci_high: Num; verdict: Verdict; annual_excess_return: Num; annual_vol: Num;
  start: string | null; end: string | null; n_obs: number; basis: string;
};
export type Evaluation = {
  mode: "full" | "quick";
  as_of: string;
  window: { weeks: number; start: string; end: string } | null;
  weights: Record<string, Num>;
  risk_contribution: Record<string, Num>;
  correlation: { tickers: string[]; matrix: Num[][]; average: Num };
  sharpe: { since_buy: SharpeBlock | null; current_weights: SharpeBlock; equal_weight: SharpeBlock | null };
  price_jumps: { ticker: string; date: string; return: Num }[];
  illiquid_tickers: string[];
  warnings: string[];
};
export type PfHistory = {
  x: string[]; sharpe_since_buy: Num[]; sharpe_current_weights: Num[]; sharpe_equal_weight: Num[];
  avg_correlation: Num[]; warnings: number[];
};

const enc = encodeURIComponent;

export const usePortfolios = () =>
  useQuery({ queryKey: ["pf", "list"], queryFn: ({ signal }) => api<PfSummary[]>("/portfolio", undefined, { signal }), staleTime: 60_000, retry: 0 });

export const useHoldings = (name: string) =>
  useQuery({ queryKey: ["pf", "holdings", name], queryFn: ({ signal }) => api<Holding[]>(`/portfolio/${enc(name)}/holdings`, undefined, { signal }), staleTime: 60_000, retry: 0 });

export const useEvaluation = (name: string, mode: string) =>
  useQuery({ queryKey: ["pf", "eval", name, mode], queryFn: ({ signal }) => api<Evaluation>(`/portfolio/${enc(name)}/evaluate`, { mode }, { signal }), staleTime: 5 * 60_000, retry: 0 });

export const useHistory = (name: string) =>
  useQuery({ queryKey: ["pf", "history", name], queryFn: ({ signal }) => api<PfHistory>(`/portfolio/${enc(name)}/history`, undefined, { signal }), staleTime: 5 * 60_000, retry: 0 });

export function usePortfolioMutations(name: string) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["pf"] });
  const add = useMutation({
    mutationFn: (body: { ticker: string; lots: number; buy_date: string }) =>
      api(`/portfolio/${enc(name)}/holdings`, undefined, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/portfolio/holdings/${id}`, undefined, { method: "DELETE" }),
    onSuccess: refresh,
  });
  return { add, remove };
}