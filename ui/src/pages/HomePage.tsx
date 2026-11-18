import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useUser } from "@clerk/react";
import { useMacroOverview, useMacroSeries, useMeta, useScanPeriods, useScanResults } from "../lib/api";
import { day, num, pct, tone } from "../lib/format";
import { macroLabel } from "../lib/macroNames";
import { useEvaluation, usePortfolios, type Evaluation, type SharpeBlock } from "../lib/portfolio";
import type { MacroRow, ScanRow } from "../lib/types";
import { Chip } from "../components/ui";
import { Icon } from "../components/Icon";
import { TickerSearch } from "../components/TickerSearch";

const VERDICT: Record<SharpeBlock["verdict"], string> = {
  positif: "Di atas suku bunga",
  negatif: "Di bawah suku bunga",
  tidak_pasti: "Belum pasti",
  tidak_dapat_dihitung: "Tidak dapat dihitung",
};

const DEFAULT_PORTFOLIO = "default";
/** Indikator makro dianggap terbaru bila tanggalnya paling jauh ini dari data paling baru. */
const FRESH_DAYS = 45;
const PALETTE = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s5)", "var(--s6)"];
const OTHERS = "var(--s8)";

const greeting = () => {
  const h = new Date().getHours();
  return h < 11 ? "Selamat pagi" : h < 15 ? "Selamat siang" : h < 18 ? "Selamat sore" : "Selamat malam";
};
const reason = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

/* ---------- Komponen dasar ---------- */

function Panel({ title, area, action, children }: { title: string; area: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className={`card panel ${area}`}>
      <header className="card-head"><h3>{title}</h3>{action}</header>
      {children}
    </section>
  );
}

const More = ({ to, children }: { to: string; children: ReactNode }) => (
  <Link to={to} className="more-link">{children}<Icon name="arrow" size={14} /></Link>
);

const Skel = ({ rows = 3 }: { rows?: number }) => (
  <div className="skel" role="status" aria-label="Memuat">
    {Array.from({ length: rows }, (_, i) => <i key={i} style={{ width: `${92 - i * 12}%` }} />)}
  </div>
);

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

/* ---------- Sinyal ---------- */

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

/* ---------- Portofolio ---------- */

type Slice = { t: string; w: number; r: number; color: string; other?: boolean };

/** Lima saham terbesar berwarna sendiri, sisanya digabung agar donat tetap mudah dibaca. */
function toSlices(e: Evaluation): Slice[] {
  const all = Object.keys(e.weights)
    .map((t) => ({ t, w: e.weights[t] ?? 0, r: e.risk_contribution[t] ?? 0 }))
    .filter((x) => x.w > 0)
    .sort((a, b) => b.w - a.w);
  const head: Slice[] = all.slice(0, PALETTE.length).map((x, i) => ({ ...x, color: PALETTE[i] }));
  const rest = all.slice(PALETTE.length);
  if (!rest.length) return head;
  const sum = (k: "w" | "r") => rest.reduce((a, x) => a + x[k], 0);
  return [...head, { t: `Lainnya (${rest.length})`, w: sum("w"), r: sum("r"), color: OTHERS, other: true }];
}

/** Satu kalimat temuan: apakah risiko terpusat lebih besar daripada porsi bobotnya. */
function finding(slices: Slice[]): string {
  const named = slices.filter((s) => !s.other);
  if (slices.length < 2) return "Portofolio satu saham: seluruh risiko terpusat pada satu emiten.";
  const top = [...named].sort((a, b) => b.r - a.r)[0];
  if (top && top.r - top.w > 0.05) return `${top.t} menyumbang ${pct(top.r, 1)} risiko dengan bobot ${pct(top.w, 1)}.`;
  return "Kontribusi risiko sejalan dengan bobot tiap saham.";
}

function Donut({ slices, value, caption }: { slices: Slice[]; value: string; caption: string }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setReady(true), 40);
    return () => window.clearTimeout(id);
  }, []);
  const R = 42, C = 2 * Math.PI * R, gap = slices.length > 1 ? 1.4 : 0;
  let acc = 0;
  return (
    <svg className="donut" viewBox="0 0 100 100" role="img" aria-label="Komposisi bobot portofolio">
      <circle className="donut-track" cx="50" cy="50" r={R} />
      {slices.map((s, i) => {
        const len = s.w * C;
        const offset = -acc;
        acc += len;
        return (
          <circle
            key={s.t} className="donut-seg" cx="50" cy="50" r={R} transform="rotate(-90 50 50)"
            style={{ stroke: s.color, transitionDelay: `${i * 90}ms` }}
            strokeDasharray={ready ? `${Math.max(len - gap, 0)} ${C}` : `0 ${C}`} strokeDashoffset={offset}
          />
        );
      })}
      <text className="donut-val" x="50" y="52" textAnchor="middle">{value}</text>
      <text className="donut-cap" x="50" y="64" textAnchor="middle">{caption}</text>
    </svg>
  );
}

function PortfolioGlance({ name, loading, listError, evalError, e, href }: {
  name?: string; loading: boolean; listError: unknown; evalError?: string; e?: Evaluation; href: string;
}) {
  if (loading) return <Skel rows={4} />;
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
        <p className="muted">{evalError ?? "Ringkasan cepat belum tersedia."}</p>
        <Link to={href} className="btn">Buka portofolio</Link>
      </div>
    );
  }
  const slices = toSlices(e);
  const sharpe = e.sharpe.current_weights;
  return (
    <div className="pf-glance">
      <div className="pf-donut">
        <Donut slices={slices} value={sharpe.sharpe == null ? "-" : num(sharpe.sharpe, 2)} caption="Sharpe" />
      </div>
      <div className="pf-side">
        <div className="pf-verdict">
          <Chip kind={sharpe.verdict}>{VERDICT[sharpe.verdict]}</Chip>
        </div>
        <p className="pf-finding">{finding(slices)}</p>
        <ul className="hl">
          <li className="hl-row hl-head"><span /><span>Saham</span><span className="n">Bobot</span><span className="n">Risiko</span></li>
          {slices.map((s) => (
            <li key={s.t} className="hl-row">
              <i className="hl-dot" style={{ background: s.color }} />
              <strong>{s.t}</strong>
              <span className="n">{pct(s.w, 1)}</span>
              <span className={`n${!s.other && s.r - s.w > 0.05 ? " over" : ""}`}>{pct(s.r, 1)}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/* ---------- Makro ---------- */

function MacroMovers({ rows, loading, error }: { rows?: MacroRow[]; loading: boolean; error: boolean }) {
  if (loading) return <Skel rows={5} />;
  if (error) return <p className="muted">Data makro belum bisa dimuat.</p>;
  const dated = (rows ?? []).filter((r) => r.change1m != null);
  const newest = Math.max(0, ...dated.map((r) => new Date(r.last_date).getTime() || 0));
  const top = dated
    .filter((r) => newest - (new Date(r.last_date).getTime() || 0) <= FRESH_DAYS * 86_400_000)
    .sort((a, b) => Math.abs(b.change1m ?? 0) - Math.abs(a.change1m ?? 0))
    .slice(0, 5);
  if (!top.length) return <p className="muted">Belum ada data makro terbaru.</p>;
  return (
    <ul className="rows">
      {top.map((r) => (
        <li key={r.key}>
          <Link to={`/makro?k=${encodeURIComponent(r.key)}`} className="row-link">
            <span className="row-main"><strong>{macroLabel(r)}</strong><small>{r.key}</small></span>
            <span className="row-meta"><span>{num(r.last_value, 2)}</span><b className={tone(r.change1m)}>{pct(r.change1m, 1, true)}</b></span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/* ---------- Halaman ---------- */

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
  const pfName = pfs.data?.find((p) => p.name === DEFAULT_PORTFOLIO)?.name ?? pfs.data?.[0]?.name;
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

  const scanLoading = periods.isPending || (Boolean(latest) && scan.isPending);
  const pfLoading = pfs.isPending || (Boolean(pfName) && quick.isPending);
  // Nama portofolio hanya ditampilkan bila bukan bawaan.
  const pfTitle = pfName && pfName !== DEFAULT_PORTFOLIO ? `Portofolio · ${pfName}` : "Portofolio";

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
      </div>

      <div className="home-grid">
        <Panel title={pfTitle} area="home-portfolio" action={<More to={pfHref}>Kelola portofolio</More>}>
          <PortfolioGlance
            name={pfName} href={pfHref} loading={pfLoading} listError={pfs.error}
            evalError={quick.isError ? reason(quick.error, "") || undefined : undefined} e={quick.data}
          />
        </Panel>
        <Panel title="Sinyal terbaru" area="home-signals" action={<More to="/sinyal?tab=scan">Buka pemindai</More>}>
          {scanLoading ? <Skel rows={5} />
            : periods.isError || scan.isError ? <p className="muted">Hasil pemindaian belum tersedia.</p>
            : <SignalList rows={top} />}
        </Panel>
        <Panel title="Pergerakan makro 1 bulan" area="home-macro" action={<More to="/makro">Jelajahi makro</More>}>
          <MacroMovers rows={macro.data} loading={macro.isPending} error={macro.isError} />
        </Panel>
      </div>
    </>
  );
}