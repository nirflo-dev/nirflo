import { useEffect, useMemo, useState, type FormEvent } from "react";
import { day, num, pct, tone } from "../lib/format";
import { useColors, useParam, type Palette } from "../lib/hooks";
import {
  useEvaluation, useHistory, useHoldings, usePortfolioMutations, usePortfolios,
  type Evaluation, type Holding, type PfHistory, type SharpeBlock,
} from "../lib/portfolio";
import { baseOption, Chart, line, X, Y, yName } from "../components/Chart";
import { Async, DataTable, Empty, ErrorState, Kpi, Loading, Section, Segmented, type Col } from "../components/ui";

const VERDICTS: Record<SharpeBlock["verdict"], string> = {
  positif: "di atas suku bunga",
  negatif: "di bawah suku bunga",
  tidak_pasti: "belum dapat dibedakan dari nol",
  tidak_dapat_dihitung: "tidak dapat dihitung",
};
const SHARPE_ROWS = [["since_buy", "Sejak beli"], ["current_weights", "Bobot sekarang"], ["equal_weight", "Sama rata"]] as const;

export default function PortfolioPage() {
  const [name, setName] = useParam("p", "default");
  const [mode, setMode] = useParam("mode", "full");
  const list = usePortfolios();
  const holdings = useHoldings(name);
  const hasHoldings = (holdings.data?.length ?? 0) > 0;

  return (
    <>
      <header className="pagehead">
        <PortfolioPicker value={name} names={list.data?.map((p) => p.name) ?? []} onChange={setName} />
        <Segmented
          label="Mode perhitungan" value={mode} onChange={setMode}
          options={[{ value: "full", label: "Lengkap" }, { value: "quick", label: "Cepat" }]}
        />
      </header>
      {mode === "quick" && (
        <p className="muted">
          Mode cepat memakai statistik universe tanpa membaca harga. Hasilnya hanya Sharpe bobot sekarang, tanpa Sharpe sejak beli.
        </p>
      )}

      {hasHoldings ? <EvaluationSection name={name} mode={mode} />
        : holdings.isPending ? <Loading />
        : holdings.isError ? <ErrorState error={holdings.error} retry={() => holdings.refetch()} />
        : <Empty>Portofolio “{name}” belum memiliki posisi. Tambahkan posisi pertama di bawah.</Empty>}

      <HoldingsSection name={name} holdings={holdings.data ?? []} />
      <HistorySection name={name} />
    </>
  );
}

function PortfolioPicker({ value, names, onChange }: { value: string; names: string[]; onChange: (v: string) => void }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = (s: string) => { const v = s.trim(); if (v && v !== value) onChange(v); };
  return (
    <label className="field">
      <span>Portofolio</span>
      <input
        list="pf-list" value={text} autoComplete="off" style={{ width: 220 }}
        onChange={(e) => { setText(e.target.value); if (names.includes(e.target.value)) commit(e.target.value); }}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && commit(e.currentTarget.value)}
      />
      <datalist id="pf-list">{names.map((n) => <option key={n} value={n} />)}</datalist>
      <small>Ketik nama baru untuk membuat portofolio.</small>
    </label>
  );
}

function EvaluationSection({ name, mode }: { name: string; mode: string }) {
  const q = useEvaluation(name, mode);
  return <Async q={q}>{(e) => <EvaluationView e={e} />}</Async>;
}

// ------------------------------------------------------------------ hasil evaluasi
type SRow = { label: string; b: SharpeBlock };

function sharpeOption(c: Palette, rows: SRow[]) {
  return baseOption(c, {
    legend: { show: false },
    tooltip: {
      trigger: "item",
      formatter: (p: { dataIndex: number }) => {
        const r = rows[p.dataIndex];
        return `${r.label}<br/>Sharpe ${num(r.b.sharpe, 2)} (95%: ${num(r.b.ci_low, 2)} sampai ${num(r.b.ci_high, 2)})`;
      },
    },
    grid: { left: 110, right: 24, top: 16, bottom: 40 },
    xAxis: Y(c, { ...yName("Sharpe ratio tahunan", 28) }),
    yAxis: { type: "category", inverse: true, data: rows.map((r) => r.label), axisLabel: { color: c.text }, axisLine: { lineStyle: { color: c.border } } },
    series: [
      { // selang kepercayaan 95%
        type: "custom", silent: true, z: 1,
        data: rows.map((r, i) => [i, r.b.ci_low, r.b.ci_high]),
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        renderItem: (_: unknown, api: any) => {
          const i = api.value(0) as number;
          const a = api.coord([api.value(1), i]), b = api.coord([api.value(2), i]);
          const style = { stroke: c.text, lineWidth: 1.6 };
          const cap = (p: number[]) => ({ type: "line", shape: { x1: p[0], y1: p[1] - 6, x2: p[0], y2: p[1] + 6 }, style });
          return { type: "group", children: [{ type: "line", shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] }, style }, cap(a), cap(b)] };
        },
      },
      {
        type: "scatter", symbolSize: 11, itemStyle: { color: c.accent },
        data: rows.map((r, i) => [r.b.sharpe, i]),
        markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.muted }, data: [{ xAxis: 0 }] },
      },
    ],
  });
}

function weightsOption(c: Palette, e: Evaluation) {
  const tickers = Object.keys(e.weights);
  const pctOf = (m: Record<string, number | null>) => tickers.map((t) => (m[t] == null ? null : (m[t] as number) * 100));
  return baseOption(c, {
    grid: { left: 56, right: 16, top: 36, bottom: 28 },
    xAxis: { type: "category", data: tickers, axisLabel: { color: c.muted } },
    yAxis: Y(c, { ...yName("Persen (%)", 40), scale: false }),
    series: [
      { type: "bar", name: "Bobot", data: pctOf(e.weights), itemStyle: { color: c.accent }, barMaxWidth: 32 },
      { type: "bar", name: "Kontribusi risiko", data: pctOf(e.risk_contribution), itemStyle: { color: c.warn }, barMaxWidth: 32 },
    ],
  });
}

function EvaluationView({ e }: { e: Evaluation }) {
  const c = useColors();
  const rows: SRow[] = SHARPE_ROWS.flatMap(([k, label]) => (e.sharpe[k] ? [{ label, b: e.sharpe[k] as SharpeBlock }] : []));
  const plotted = rows.filter((r) => r.b.sharpe != null && r.b.ci_low != null && r.b.ci_high != null);
  const sOpt = useMemo(() => sharpeOption(c, plotted), [c, plotted]); // eslint-disable-line react-hooks/exhaustive-deps
  const wOpt = useMemo(() => weightsOption(c, e), [c, e]);
  const now = e.sharpe.current_weights;

  const cols: Col<SRow>[] = [
    { key: "label", label: "Perhitungan" },
    { key: "sharpe", label: "Sharpe", num: true, get: (r) => r.b.sharpe, fmt: (v) => num(v, 2) },
    { key: "ci", label: "Selang 95%", get: (r) => r.b.ci_low, fmt: (_, r) => (r.b.ci_low == null ? "-" : `${num(r.b.ci_low, 2)} sampai ${num(r.b.ci_high, 2)}`) },
    { key: "excess", label: "Excess return/thn", num: true, get: (r) => r.b.annual_excess_return, fmt: (v) => pct(v, 1, true) },
    { key: "vol", label: "Volatilitas/thn", num: true, get: (r) => r.b.annual_vol, fmt: (v) => pct(v, 1) },
    { key: "n", label: "Observasi", get: (r) => r.b.n_obs, fmt: (v, r) => `${v} ${r.b.basis}` },
    { key: "period", label: "Periode", get: (r) => r.b.start, fmt: (_, r) => (r.b.start ? `${day(r.b.start)} - ${day(r.b.end)}` : "-") },
    { key: "verdict", label: "Kesimpulan", get: (r) => VERDICTS[r.b.verdict] },
  ];

  return (
    <>
      {e.warnings.length > 0 && (
        <div className="banner" role="alert">
          <ul>{e.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </div>
      )}
      <div className="kpis">
        <Kpi label="Sharpe (bobot sekarang)" value={num(now.sharpe, 2)} tone={tone(now.sharpe)} hint={VERDICTS[now.verdict]} />
        <Kpi label="Rata-rata korelasi" value={num(e.correlation.average, 2)} hint="Rata-rata korelasi berpasangan antar saham" />
        <Kpi label="Jumlah saham" value={e.correlation.tickers.length} />
        <Kpi label="Data per" value={day(e.as_of)} hint={e.window ? `Jendela ${e.window.weeks} minggu` : "Estimasi dari statistik universe"} />
      </div>

      <Section title="Sharpe ratio">
        {plotted.length > 0 && <Chart option={sOpt} height={70 + 44 * plotted.length} label="Sharpe ratio dengan selang kepercayaan 95%" />}
        <DataTable cols={cols} rows={rows} />
        <p className="muted">
          Garis menunjukkan selang kepercayaan 95%. Jika selang melewati nol, hasilnya belum dapat dibedakan dari suku bunga bebas risiko.
          “Bobot sekarang” mengandaikan bobot hari ini konstan selama jendela data, sedangkan “Sejak beli” memakai return yang benar-benar terjadi.
        </p>
      </Section>

      <div className="two">
        <Section title="Bobot dan kontribusi risiko">
          <Chart option={wOpt} height={320} label="Bobot dan kontribusi risiko tiap saham" />
        </Section>
        <Section title="Korelasi antar saham">
          <CorrelationTable e={e} />
        </Section>
      </div>
    </>
  );
}

function CorrelationTable({ e }: { e: Evaluation }) {
  const { tickers, matrix, average } = e.correlation;
  const shade = (v: number | null) =>
    v == null ? undefined : `color-mix(in srgb, var(${v >= 0 ? "--accent" : "--down"}) ${Math.min(1, Math.abs(v)) * 65}%, transparent)`;
  return (
    <>
      <div className="tablewrap">
        <table>
          <thead><tr><th />{tickers.map((t) => <th key={t} className="r">{t}</th>)}</tr></thead>
          <tbody>
            {tickers.map((t, i) => (
              <tr key={t}>
                <th scope="row">{t}</th>
                {matrix[i].map((v, j) => <td key={j} className="r" style={{ background: shade(v) }}>{num(v, 2)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">
        Return mingguan{e.window ? `, ${e.window.weeks} minggu` : ""}. Rata-rata berpasangan {num(average, 2)}. Semakin rendah, semakin menyebar risikonya.
      </p>
    </>
  );
}

// ------------------------------------------------------------------ posisi
function HoldingsSection({ name, holdings }: { name: string; holdings: Holding[] }) {
  const { remove } = usePortfolioMutations(name);
  const cols: Col<Holding>[] = [
    { key: "ticker", label: "Ticker" },
    { key: "lots", label: "Lot", num: true, fmt: (v) => num(v, 2) },
    { key: "buy_date", label: "Tanggal beli", fmt: (v) => day(v) },
    {
      key: "id", label: "Aksi",
      fmt: (_, r) => (
        <button
          disabled={remove.isPending}
          onClick={() => window.confirm(`Hapus posisi ${r.ticker} (${r.lots} lot, ${r.buy_date})?`) && remove.mutate(r.id)}
        >
          Hapus
        </button>
      ),
    },
  ];
  return (
    <Section title={`Posisi di “${name}”`}>
      {holdings.length > 0 && <DataTable cols={cols} rows={holdings} />}
      {remove.isError && <p className="down" role="alert">{remove.error.message}</p>}
      <AddHolding name={name} />
    </Section>
  );
}

function AddHolding({ name }: { name: string }) {
  const { add } = usePortfolioMutations(name);
  const [ticker, setTicker] = useState("");
  const [lots, setLots] = useState("");
  const [date, setDate] = useState("");
  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    add.mutate({ ticker: ticker.trim(), lots: Number(lots), buy_date: date }, { onSuccess: () => { setTicker(""); setLots(""); } });
  };
  return (
    <form className="formgrid" onSubmit={submit}>
      <label className="field"><span>Ticker</span>
        <input required value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="mis. BBCA" autoComplete="off" />
      </label>
      <label className="field"><span>Lot</span>
        <input required type="number" min={0.01} step={0.01} value={lots} onChange={(e) => setLots(e.target.value)} />
      </label>
      <label className="field"><span>Tanggal beli</span>
        <input required type="date" max={new Date().toISOString().slice(0, 10)} value={date} onChange={(e) => setDate(e.target.value)} />
      </label>
      <button className="primary" type="submit" disabled={add.isPending}>{add.isPending ? "Menyimpan…" : "Tambah posisi"}</button>
      {add.isError && <p className="down" role="alert">{add.error.message}</p>}
    </form>
  );
}

// ------------------------------------------------------------------ riwayat
function HistorySection({ name }: { name: string }) {
  const q = useHistory(name);
  return (
    <Section title="Riwayat evaluasi tersimpan">
      <Async q={q} isEmpty={(d) => d.x.length === 0 && "Belum ada riwayat. Riwayat terisi saat python daily.py --only portfolio atau python portfolio_cli.py evaluate-all dijalankan."}>
        {(h) => <HistoryView h={h} />}
      </Async>
    </Section>
  );
}

type HRow = { as_of: string; since: number | null; now: number | null; equal: number | null; corr: number | null; warnings: number };

function HistoryView({ h }: { h: PfHistory }) {
  const c = useColors();
  const option = useMemo(() => baseOption(c, {
    grid: { left: 56, right: 16, top: 36, bottom: 28 },
    xAxis: X(c), yAxis: Y(c, yName("Sharpe ratio", 40)), dataZoom: [{ type: "inside" }],
    series: [
      line("Sejak beli", h.x, h.sharpe_since_buy, c.accent),
      line("Bobot sekarang", h.x, h.sharpe_current_weights, c.up),
      line("Sama rata", h.x, h.sharpe_equal_weight, c.muted),
    ],
  }), [c, h]);
  const rows: HRow[] = useMemo(
    () => h.x.map((d, i) => ({ as_of: d, since: h.sharpe_since_buy[i], now: h.sharpe_current_weights[i], equal: h.sharpe_equal_weight[i], corr: h.avg_correlation[i], warnings: h.warnings[i] })).reverse(),
    [h],
  );
  const cols: Col<HRow>[] = [
    { key: "as_of", label: "Per tanggal", fmt: (v) => day(v) },
    { key: "since", label: "Sharpe sejak beli", num: true, fmt: (v) => num(v, 2) },
    { key: "now", label: "Sharpe bobot sekarang", num: true, fmt: (v) => num(v, 2) },
    { key: "equal", label: "Sharpe sama rata", num: true, fmt: (v) => num(v, 2) },
    { key: "corr", label: "Rata-rata korelasi", num: true, fmt: (v) => num(v, 2) },
    { key: "warnings", label: "Peringatan", num: true },
  ];
  return (
    <>
      <Chart option={option} height={300} label="Riwayat Sharpe ratio portofolio" />
      <DataTable cols={cols} rows={rows} pageSize={15} csv="riwayat_portofolio.csv" />
    </>
  );
}