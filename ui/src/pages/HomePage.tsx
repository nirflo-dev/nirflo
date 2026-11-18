import { Link } from "react-router-dom";
import { useUser } from "@clerk/react";
import { useMacroOverview, useMacroSeries, useMeta, useScanPeriods, useScanResults } from "../lib/api";
import { day, num, pct, tone } from "../lib/format";
import { useEvaluation, usePortfolios, type Evaluation, type SharpeBlock } from "../lib/portfolio";
import type { MacroRow, ScanRow } from "../lib/types";
import { Chip, Section } from "../components/ui";
import { TickerSearch } from "../components/TickerSearch";

const VERDICT: Record<SharpeBlock["verdict"], string> = {
  positif: "di atas suku bunga",
  negatif: "di bawah suku bunga",
  tidak_pasti: "belum pasti",
  tidak_dapat_dihitung: "tidak dapat dihitung",
};

const greeting = () => {
  const h = new Date().getHours();
  return h < 11 ? "Selamat pagi" : h < 15 ? "Selamat siang" : h < 18 ? "Selamat sore" : "Selamat malam";
};

/** Lebar bar dalam persen, dibatasi 0-100. Kontribusi risiko bisa negatif untuk aset lindung nilai. */
const width = (v: number | null | undefined) => `${Math.max(0, Math.min(100, (v ?? 0) * 100))}%`;
const reason = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

function Spark({ values, color }: { values: number[]; color: string }) {
  if (values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${27 - ((v - min) / span) * 26}`).join(" ");
  return (
    <svg className="spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden>
      <polyline points={pts} fill="none" strokeWidth="1.6" vectorEffect="non-scaling-stroke" style={{ stroke: color }} />
    </svg>
  );
}

function Stat({ label, value, delta, hint, spark, href, loading }: {
  label: string; value: string; delta?: number | null; hint?: string; spark?: number[]; href: string; loading?: boolean;
}) {
  const color = delta == null ? "var(--accent)" : delta < 0 ? "var(--down)" : "var(--up)";
  return (
    <Link to={href} className={`stat${loading ? " skeleton" : ""}`}>
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{loading ? "…" : value}</strong>
      <span className="stat-sub">
        {delta != null && <b className={tone(delta)}>{pct(delta, 1, true)}</b>}
        {hint && <small>{hint}</small>}
      </span>
      {spark && <Spark values={spark} color={color} />}
    </Link>
  );
}

function SignalList({ rows }: { rows: ScanRow[] }) {
  if (!rows.length) return <p className="muted">Belum ada sinyal kuat atau moderat pada periode ini.</p>;
  return (
    <ul className="rows">
      {rows.map((r) => (
        <li key={r.ticker}>
          <Link to={`/emiten/${r.ticker}?tab=sinyal`} className="row-link">
            <span className="row-main"><strong>{r.ticker}</strong><small>{r.sector ?? "-"}</small></span>
            <span className="row-meta">
              <Chip kind={r.status.toLowerCase().replace(" ", "-")}>{r.status}</Chip>
              <span className={tone(r.growth_yoy)}>{pct(r.growth_yoy, 1, true)}</span>
              <small className="muted">z {num(r.z_score, 1)}</small>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function PortfolioGlance({ name, href, loading, listError, evalError, e }: {
  name?: string; href: string; loading: boolean; listError: unknown; evalError?: string; e?: Evaluation;
}) {
  if (loading) return <p className="muted">Memuat…</p>;
  if (listError) return <p className="muted">{reason(listError, "Portofolio belum dapat dimuat.")}</p>;
  if (!name) {
    return (
      <div className="empty-cta">
        <p className="muted">Belum ada portofolio. Tambahkan posisi pertama untuk melihat risiko dan Sharpe ratio.</p>
        <Link to="/portofolio" className="btn primary">Mulai portofolio</Link>
      </div>
    );
  }
  if (!e) {
    return (
      <div className="empty-cta">
        <p className="muted">{evalError ?? `Ringkasan cepat belum tersedia untuk “${name}”.`}</p>
        <Link to={href} className="btn">Buka portofolio</Link>
      </div>
    );
  }
  const items = Object.keys(e.weights)
    .map((t) => ({ t, w: e.weights[t] ?? 0, r: e.risk_contribution[t] ?? 0 }))
    .sort((a, b) => b.w - a.w)
    .slice(0, 4);
  return (
    <>
      <p className="muted">“{name}” · {e.correlation.tickers.length} saham</p>
      <ul className="alloc">
        {items.map(({ t, w, r }) => (
          <li key={t}>
            <strong>{t}</strong>
            <div className="bars">
              <i className="bar bw" style={{ width: width(w) }} />
              <i className="bar br" style={{ width: width(r) }} />
            </div>
            <span className="nums">{pct(w, 1)} <small>· risiko {pct(r, 1)}</small></span>
          </li>
        ))}
      </ul>
    </>
  );
}

function MacroMovers({ rows, error }: { rows?: MacroRow[]; error: boolean }) {
  if (error) return <p className="muted">Data makro belum bisa dimuat.</p>;
  const top = [...(rows ?? [])]
    .filter((r) => r.change1m != null)
    .sort((a, b) => Math.abs(b.change1m ?? 0) - Math.abs(a.change1m ?? 0))
    .slice(0, 5);
  if (!top.length) return <p className="muted">Belum ada data makro.</p>;
  return (
    <ul className="rows">
      {top.map((r) => (
        <li key={r.key}>
          <Link to={`/makro?k=${encodeURIComponent(r.key)}`} className="row-link">
            <span className="row-main"><strong>{r.key}</strong><small>{r.name ?? ""}</small></span>
            <span className="row-meta"><span>{num(r.last_value, 2)}</span><b className={tone(r.change1m)}>{pct(r.change1m, 1, true)}</b></span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function HomePage() {
  const { user } = useUser();
  const meta = useMeta();
  const macro = useMacroOverview();
  const series = useMacroSeries(["IHSG", "USDIDR"], 1);
  const periods = useScanPeriods();
  const latest = periods.data?.[0];
  const scan = useScanResults(latest?.year, latest?.period);
  const pfs = usePortfolios();
  // Selaras dengan halaman Portofolio: utamakan "default", bila tidak ada pakai yang pertama.
  const pfName = pfs.data?.find((p) => p.name === "default")?.name ?? pfs.data?.[0]?.name;
  const pfHref = pfName ? `/portofolio?p=${encodeURIComponent(pfName)}` : "/portofolio";
  const quick = useEvaluation(pfName ?? "", "quick", Boolean(pfName));

  const pick = (k: string) => macro.data?.find((r) => r.key === k);
  const ihsg = pick("IHSG");
  const fx = pick("USDIDR");
  const counts = scan.data?.counts;
  const active = (counts?.STRONG ?? 0) + (counts?.MODERATE ?? 0);
  const top = (scan.data?.rows ?? [])
    .filter((r) => r.status === "STRONG" || r.status === "MODERATE")
    .sort((a, b) => Math.abs(b.z_score ?? 0) - Math.abs(a.z_score ?? 0))
    .slice(0, 6);
  const sharpe = quick.data?.sharpe.current_weights;

  const scanLoading = periods.isPending || (Boolean(latest) && scan.isPending);
  const pfLoading = pfs.isPending || (Boolean(pfName) && quick.isPending);
  const pfHint = pfs.isError
    ? "tidak tersedia"
    : pfName
      ? (sharpe ? `“${pfName}” · ${VERDICT[sharpe.verdict]}` : `“${pfName}”`)
      : "belum ada portofolio";

  return (
    <>
      <section className="hero">
        <div>
          <p className="eyebrow">{greeting()}</p>
          <h2>{user?.firstName ? `Halo, ${user.firstName}` : "Selamat datang di Nirflo"}</h2>
          <p className="muted">
            Ringkasan pasar, sinyal, dan portofolio Anda.{meta.data?.lastPriceDate ? ` Harga terakhir ${day(meta.data.lastPriceDate)}.` : ""}
          </p>
        </div>
        <TickerSearch globalKey />
      </section>

      <div className="stats">
        <Stat label="IHSG" value={num(ihsg?.last_value, 2)} delta={ihsg?.change1m} hint="1 bulan" spark={series.data?.series["IHSG"]?.y} loading={macro.isPending} href="/makro?k=IHSG" />
        <Stat label="USD/IDR" value={num(fx?.last_value, 0)} delta={fx?.change1m} hint="1 bulan" spark={series.data?.series["USDIDR"]?.y} loading={macro.isPending} href="/makro?k=USDIDR" />
        <Stat
          label="Sinyal aktif" value={String(active)} hint={latest ? `${latest.period} ${latest.year}` : "belum ada pemindaian"}
          loading={scanLoading} href="/sinyal?tab=scan"
        />
        <Stat
          label="Sharpe portofolio" value={sharpe ? num(sharpe.sharpe, 2) : "-"} hint={pfHint}
          loading={pfLoading} href={pfHref}
        />
      </div>

      <div className="home-grid">
        <Section title="Sinyal terbaru" className="home-signals" action={<Link className="more-link" to="/sinyal?tab=scan">Lihat semua</Link>}>
          {scanLoading ? <p className="muted">Memuat…</p>
            : periods.isError || scan.isError ? <p className="muted">Hasil pemindaian belum tersedia.</p>
            : <SignalList rows={top} />}
        </Section>
        <Section title="Portofolio" className="home-portfolio" action={<Link className="more-link" to={pfHref}>Buka</Link>}>
          <PortfolioGlance
            name={pfName} href={pfHref} loading={pfLoading} listError={pfs.error}
            evalError={quick.isError ? reason(quick.error, "") || undefined : undefined} e={quick.data}
          />
        </Section>
        <Section title="Makro, gerak 1 bulan terbesar" className="home-macro" action={<Link className="more-link" to="/makro">Semua</Link>}>
          <MacroMovers rows={macro.data} error={macro.isError} />
        </Section>
      </div>
    </>
  );
}