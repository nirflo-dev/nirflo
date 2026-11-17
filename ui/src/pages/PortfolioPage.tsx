import { useEffect, useMemo, useState, type FormEvent } from "react";
import { day, num, pct, tone } from "../lib/format";
import { useColors, useParam } from "../lib/hooks";
import {
  useEvaluation, useHistory, useHoldings, usePortfolioMutations, usePortfolios,
  type Evaluation, type Holding, type PfHistory, type SharpeBlock,
} from "../lib/portfolio";
import { baseOption, Chart, line, X, Y, yName } from "../components/Chart";
import { Async, Chip, DataTable, ErrorState, Kpi, Loading, Section, Segmented, type Col } from "../components/ui";

const VERDICTS: Record<SharpeBlock["verdict"], string> = {
  positif: "di atas suku bunga",
  negatif: "di bawah suku bunga",
  tidak_pasti: "belum dapat dibedakan dari nol",
  tidak_dapat_dihitung: "tidak dapat dihitung",
};
const VERDICT_SHORT: Record<SharpeBlock["verdict"], string> = {
  positif: "Di atas suku bunga",
  negatif: "Di bawah suku bunga",
  tidak_pasti: "Belum pasti",
  tidak_dapat_dihitung: "Tidak dapat dihitung",
};
const SHARPE_ROWS = [["since_buy", "Sejak beli"], ["current_weights", "Bobot sekarang"], ["equal_weight", "Sama rata"]] as const;

export default function PortfolioPage() {
  const [name, setName] = useParam("p", "default");
  const [mode, setMode] = useParam("mode", "full");
  const [adding, setAdding] = useState(false);
  const list = usePortfolios();
  const holdings = useHoldings(name);
  const rows = holdings.data ?? [];
  const hasHoldings = rows.length > 0;
  const ev = useEvaluation(name, mode, hasHoldings);
  const e = hasHoldings ? ev.data : undefined;
  const count = e?.correlation.tickers.length ?? 0;
  const gridClass = ["pf-grid", e ? "" : "solo", e && count < 2 ? "nocorr" : ""].filter(Boolean).join(" ");

  useEffect(() => setAdding(false), [name]);

  return (
    <>
      <header className="pf-head">
        <div>
          <h2>Portofolio</h2>
          <p className="muted">{e ? `${count} saham · data per ${day(e.as_of)}` : "Kinerja dan risiko portofolio saham IDX."}</p>
        </div>
        <div className="pf-controls">
          <PortfolioPicker value={name} names={list.data?.map((p) => p.name) ?? []} onChange={setName} />
          <Segmented
            label="Mode perhitungan" value={mode} onChange={setMode}
            options={[{ value: "full", label: "Lengkap" }, { value: "quick", label: "Cepat" }]}
          />
        </div>
      </header>
      {mode === "quick" && (
        <p className="muted">Mode cepat memakai statistik universe tanpa membaca harga. Hasilnya hanya Sharpe bobot sekarang, tanpa Sharpe sejak beli.</p>
      )}

      {holdings.isPending ? <Loading />
        : holdings.isError ? <ErrorState error={holdings.error} retry={() => holdings.refetch()} />
        : !hasHoldings ? <EmptyPortfolio name={name} />
        : (
          <>
            {ev.isPending && <Loading label="Menghitung evaluasi…" />}
            {ev.isError && <ErrorState error={ev.error} retry={() => ev.refetch()} />}
            {e && <Summary e={e} />}
            <div className={gridClass}>
              {e && <SharpeSection e={e} />}
              {e && <AllocationSection e={e} />}
              <PositionsSection name={name} holdings={rows} adding={adding} setAdding={setAdding} />
              {e && count > 1 && <Section title="Korelasi antar saham" className="pf-corr"><CorrelationTable e={e} /></Section>}
            </div>
          </>
        )}

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
        list="pf-list" value={text} autoComplete="off"
        onChange={(e) => { setText(e.target.value); if (names.includes(e.target.value)) commit(e.target.value); }}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && commit(e.currentTarget.value)}
      />
      <datalist id="pf-list">{names.map((n) => <option key={n} value={n} />)}</datalist>
      <small>Ketik nama baru untuk membuat portofolio.</small>
    </label>
  );
}

function EmptyPortfolio({ name }: { name: string }) {
  return (
    <Section title={`Mulai portofolio “${name}”`} className="pf-empty">
      <p className="muted">Tambahkan posisi pertama untuk melihat Sharpe ratio, alokasi risiko, dan korelasi antar saham.</p>
      <AddHolding name={name} />
    </Section>
  );
}

function Summary({ e }: { e: Evaluation }) {
  const now = e.sharpe.current_weights;
  const n = e.correlation.tickers.length;
  return (
    <>
      {e.warnings.length > 0 && (
        <div className="banner" role="alert">
          <ul>{e.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        </div>
      )}
      <div className="kpis">
        <Kpi label="Sharpe (bobot sekarang)" value={num(now.sharpe, 2)} tone={tone(now.sharpe)} hint={`Return ${VERDICTS[now.verdict]}`} />
        <Kpi label="Rata-rata korelasi" value={num(e.correlation.average, 2)} hint={n > 1 ? "Rata-rata berpasangan antar saham" : "Perlu minimal 2 saham"} />
        <Kpi label="Jumlah saham" value={n} />
        <Kpi label="Data per" value={day(e.as_of)} hint={e.window ? `Jendela ${e.window.weeks} minggu` : "Estimasi dari statistik universe"} />
      </div>
    </>
  );
}

type SRow = { label: string; b: SharpeBlock };

function SharpeSection({ e }: { e: Evaluation }) {
  const rows: SRow[] = SHARPE_ROWS.flatMap(([k, label]) => (e.sharpe[k] ? [{ label, b: e.sharpe[k] as SharpeBlock }] : []));
  const ok = (r: SRow) => r.b.sharpe != null && r.b.ci_low != null && r.b.ci_high != null;
  const plotted = rows.filter(ok);
  const lo = Math.min(0, ...plotted.map((r) => r.b.ci_low as number));
  const hi = Math.max(0, ...plotted.map((r) => r.b.ci_high as number));
  const pad = (hi - lo) * 0.06 || 0.5;
  const a = lo - pad, b = hi + pad;
  const at = (v: number) => `${Math.min(100, Math.max(0, ((v - a) / (b - a)) * 100))}%`;

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
    <Section title="Sharpe ratio" className="pf-sharpe">
      <ul className="rngs">
        {rows.map((r) => (
          <li key={r.label}>
            <div className="rng-head"><span>{r.label}</span><strong className={tone(r.b.sharpe)}>{num(r.b.sharpe, 2)}</strong></div>
            {ok(r) ? (
              <>
                <div
                  className="rng-track" role="img"
                  aria-label={`${r.label}: Sharpe ${num(r.b.sharpe, 2)}, selang 95% ${num(r.b.ci_low, 2)} sampai ${num(r.b.ci_high, 2)}`}
                >
                  <i className="rng-zero" style={{ left: at(0) }} />
                  <i className="rng-ci" style={{ left: at(r.b.ci_low as number), width: `${(((r.b.ci_high as number) - (r.b.ci_low as number)) / (b - a)) * 100}%` }} />
                  <i className="rng-dot" style={{ left: at(r.b.sharpe as number) }} />
                </div>
                <div className="rng-foot">
                  <span>{num(r.b.ci_low, 2)}</span>
                  <Chip kind={r.b.verdict}>{VERDICT_SHORT[r.b.verdict]}</Chip>
                  <span>{num(r.b.ci_high, 2)}</span>
                </div>
              </>
            ) : (
              <p className="muted">Tidak dapat dihitung (n={r.b.n_obs}).</p>
            )}
          </li>
        ))}
      </ul>
      <p className="muted small">
        Batang menunjukkan selang kepercayaan 95%, garis putus-putus adalah nol. Jika batang melewati nol, hasilnya belum dapat dibedakan dari suku bunga bebas risiko.
      </p>
      <details className="more">
        <summary>Rincian perhitungan</summary>
        <DataTable cols={cols} rows={rows} />
        <p className="muted small">
          “Bobot sekarang” mengandaikan bobot hari ini konstan selama jendela data, sedangkan “Sejak beli” memakai return yang benar-benar terjadi.
        </p>
      </details>
    </Section>
  );
}

function AllocationSection({ e }: { e: Evaluation }) {
  const items = Object.keys(e.weights)
    .map((t) => ({ t, w: e.weights[t] ?? 0, r: e.risk_contribution[t] ?? 0 }))
    .sort((x, y) => y.w - x.w);
  return (
    <Section title="Alokasi dan risiko" className="pf-alloc">
      <ul className="alloc">
        {items.map(({ t, w, r }) => (
          <li key={t}>
            <strong>{t}</strong>
            <div className="bars" role="img" aria-label={`${t}: bobot ${pct(w, 1)}, kontribusi risiko ${pct(r, 1)}`}>
              <i className="bar bw" style={{ width: `${Math.max(0, Math.min(100, w * 100))}%` }} />
              <i className="bar br" style={{ width: `${Math.max(0, Math.min(100, r * 100))}%` }} />
            </div>
            <span className="nums">{pct(w, 1)} <small>· risiko {pct(r, 1)}</small></span>
          </li>
        ))}
      </ul>
      <div className="legend">
        <span><i className="sw bw" />Bobot</span>
        <span><i className="sw br" />Kontribusi risiko</span>
      </div>
    </Section>
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
      <p className="muted small">
        Return mingguan{e.window ? `, ${e.window.weeks} minggu` : ""}. Rata-rata berpasangan {num(average, 2)}. Semakin rendah, semakin menyebar risikonya.
      </p>
    </>
  );
}

function PositionsSection({ name, holdings, adding, setAdding }: {
  name: string; holdings: Holding[]; adding: boolean; setAdding: (v: boolean) => void;
}) {
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
    <Section
      title="Posisi" className="pf-hold"
      action={<button className={adding ? "" : "primary"} onClick={() => setAdding(!adding)}>{adding ? "Tutup" : "+ Tambah posisi"}</button>}
    >
      {adding && <AddHolding name={name} onDone={() => setAdding(false)} />}
      <DataTable cols={cols} rows={holdings} />
      {remove.isError && <p className="down" role="alert">{remove.error.message}</p>}
    </Section>
  );
}

function AddHolding({ name, onDone }: { name: string; onDone?: () => void }) {
  const { add } = usePortfolioMutations(name);
  const [ticker, setTicker] = useState("");
  const [lots, setLots] = useState("");
  const [date, setDate] = useState("");
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    add.mutate(
      { ticker: ticker.trim(), lots: Number(lots), buy_date: date },
      { onSuccess: () => { setTicker(""); setLots(""); onDone?.(); } },
    );
  };
  return (
    <form className="formgrid" onSubmit={submit}>
      <label className="field"><span>Ticker</span>
        <input required value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="mis. BBCA" autoComplete="off" />
      </label>
      <label className="field"><span>Lot</span>
        <input required type="number" inputMode="decimal" min={0.01} step={0.01} value={lots} onChange={(e) => setLots(e.target.value)} />
      </label>
      <label className="field"><span>Tanggal beli</span>
        <input required type="date" max={today} value={date} onChange={(e) => setDate(e.target.value)} />
      </label>
      <button className="primary" type="submit" disabled={add.isPending}>{add.isPending ? "Menyimpan…" : "Simpan posisi"}</button>
      {add.isError && <p className="down" role="alert">{add.error.message}</p>}
    </form>
  );
}

function HistorySection({ name }: { name: string }) {
  const q = useHistory(name);
  return (
    <Section title="Riwayat evaluasi tersimpan" className="pf-history">
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