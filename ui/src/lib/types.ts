export type Num = number | null;

// Nilai (bukan hanya tipe): dipakai halaman Sinyal untuk daftar lama tahan
export const HORIZONS = [1, 5, 10, 20] as const;
export type Horizon = (typeof HORIZONS)[number];

export type Meta = { source: "database" | "snapshot"; snapshot: string | null; lastPriceDate: string | null; ageDays: number | null };
export type Ticker = { ticker: string; sector: string };
export type Prices = { x: string[]; close: number[]; volume: Num[]; noTrade: boolean[] };
export type SignalMark = { detected: string; period: string; year: number; status: string };

export type AnnualKey =
  | "revenue" | "gross_profit" | "operating_profit" | "net_income" | "operating_cash_flow" | "capital_expenditure"
  | "total_assets" | "total_liabilities" | "total_equity" | "cash_and_equivalents"
  | "revenue_yoy" | "net_income_yoy" | "gross_margin" | "net_margin" | "roe" | "der";
export type Annual = { year: number[]; label: string[] } & Record<AnnualKey, Num[]>;
export type Ytd = { period: string; year: number; rows: { name: string; before: Num; now: Num; growth: Num }[] };
export type Financials = { annual: Annual | null; currency?: string; unit?: string; ytd?: Ytd | null };

export type HistRow = {
  period: string; year: number; status: string; trigger_source: string | null; direction: string | null;
  confirmation: string | null; growth_yoy: Num; z_score: Num; sur: Num;
};
export type ScanRow = {
  ticker: string; sector: string | null; status: string; direction: string | null; trigger_source: string | null;
  confirmation: string | null; growth_yoy: Num; sector_median_growth: Num; z_score: Num; z_source?: string | null;
  sur: Num; revenue_curr: Num; revenue_prev: Num; net_income_curr: Num; net_income_prev: Num; note: string | null;
};
export type ScanResults = { counts: Record<string, number>; rows: ScanRow[] };
export type ScanPeriod = { year: number; period: string };

export type Summary = { n: number; mean: Num; median: Num; win: Num; ci: number };
export type PerfRow = Record<`ret_${Horizon}d` | `excess_${Horizon}d`, Num> & {
  ticker: string; year: number; period: string; status: string; direction: string; trigger_source: string;
  z: Num; sur: Num; confirmation: string; detected: string; entry_price: Num; flags: string[];
};
export type Performance = {
  available: boolean; rows: PerfRow[]; prefix: "ret" | "excess"; benchmarkMissing: boolean;
  stats: Record<string, Record<string, Summary>>;
  confirm: Record<string, Record<string, { label: string; mean: number; n: number }[]>>;
  assumptions?: { entryLag: number; maxGap: number };
};

export type MacroRow = { key: string; name: string | null; last_date: string; last_value: Num; change1m: Num; change12m: Num; source?: string };
export type MacroSeries = {
  series: Record<string, { x: string[]; y: number[] }>;
  correlation: { keys: string[]; matrix: Num[][] } | null;
};

export type TableData = { columns: string[]; rows: Record<string, unknown>[] };
export type StatusData = Record<"prices" | "signals" | "reports" | "scans" | "macro", TableData | { error: string } | null>;

export type RegimeParams = {
  enter_prob: number; exit_prob: number; vol_target: number; refit_every: number; execution_lag: number;
  prob_smooth_span: number; rebalance_band: number; risk_free: number; size_by_prob: boolean;
};
type Pack<K extends string> = { x: string[] } & Record<K, Num[]>;
export type RegimeReady = {
  state: "ready"; minDuration: number; thresholds: { enter: number; exit: number };
  equity: Pack<"buy_hold" | "macd_only" | "mrs_only" | "macd_mrs">;
  comp: Pack<"close" | "hist" | "calm" | "gate">;
  position: Pack<"macd_mrs">;
  events: Record<"entries" | "exits" | "blocked", Pack<"y">>;
  metrics: TableData; conditional: TableData; summary: TableData; fits: TableData;
  volatileDuration: number; switchesPerYear: number; refits: number; period: [string, string];
};
export type RegimeResponse = RegimeReady | { state: "running"; elapsed: number };