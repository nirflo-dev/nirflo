import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useUser } from "@clerk/react";
import { useMacroOverview, useMacroSeries, useMeta, useScanPeriods, useScanResults } from "../lib/api";
import { day, num, pct, tone } from "../lib/format";
import { macroLabel } from "../lib/macroNames";
import { useCountUp, useMounted } from "../lib/motion";
import { useEvaluation, usePortfolios, type Evaluation, type SharpeBlock } from "../lib/portfolio";
import type { MacroRow, ScanRow } from "../lib/types";
import { Donut, type Slice } from "../components/Donut";
import { Icon } from "../components/Icon";
import { TickerSearch } from "../components/TickerSearch";
import { Chip, Segmented } from "../components/ui";
import "./portfolio.css";

const VERDICT: Record<SharpeBlock["verdict"], string> = {
  positif: "Di atas suku bunga",
  negatif: "Di bawah suku bunga",
  tidak_pasti: "Belum pasti",
  tidak_dapat_dihitung: "Tidak dapat dihitung",
};

const DEFAULT_PORTFOLIO = "default";
const SIGNAL_LIMIT = 30;
/** Indikator makro dianggap terbaru bila tanggalnya paling jauh ini dari data paling baru. */
const FRESH_DAYS = 45;

const greeting = () => {
  const h = new Date().getHours();
  return h < 11 ? "Selamat pagi" : h < 15 ? "Selamat siang" : h < 18 ? "Selamat sore" : "Selamat malam";
};
const reason = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);
const bar = (v: number) => `${Math.max(0, Math.min(100, v * 100))}%`;

/**
 * Titik 1 bulan terakhir, dimulai dari penutupan terakhir pada atau sebelum tanggal yang sama bulan lalu.
 * Dasarnya sama dengan perubahan 1 bulan dari API, sehingga garis dan angka selalu searah.
 */
function lastMonth(s?: { x: string[]; y: number[] }): { y: number[]; since: string } | undefined {
  if (!s || s.y.length < 2) return undefined;
  const end = new Date(s.x[s.x.length - 1]);
  if (Number.isNaN(end.getTime())) {
    const i = Math.max(0, s.x.length - 22); // cadangan: sekitar 22 hari bursa
    return { y: s.y.slice(i), since: s.x[i] };
  }
  const cut = new Date(end);
  cut.setMonth(cut.getMonth() - 1);
  let start = 0;
  for (let i = 0; i < s.x.length; i++) {
    if (new Date(s.x[i]) <= cut) start = i;
    else break;
  }
  return { y: s.y.slice(start), since: s.x[start] };
}

/* ---------- Komponen dasar ---------- */

function Panel({ title, meta, area, action, fill, children }: {
  title: string; meta?: string; area: string; action?: ReactNode; fill?: boolean; children: ReactNode;
}) {
  return (
    <section className={`card home-panel ${area}`}>
      <header className="card-head">
        <h3>{title}{meta && <span className="panel-count">{meta}</span>}</h3>
        {action}
      </header>
      {fill ? <div className="panel-fill">{children}</div> : children}
    </section>
  );
}

const More = ({ to, children }: { to: string; children: ReactNode }) => (
  <Link to={to} className="more-link">{children}<Icon name="arrow" size={14} /></Link>
);

const Skel = ({ rows = 3 }: { rows?: number }) => (
  <div className="sk-list" role="status" aria-label="Memuat">
    {Array.from({ length: rows }, (_, i) => <i key={i} style={{ width: `${92 - i * 12}%` }} />)}
  </div>
);

/** Daftar yang dapat digulir. Bayangan pudar di tepi muncul hanya bila masih ada isi di arah itu. */
function ScrollList({ label, className = "", children }: { label: string; className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ up: false, down: false });
  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const up = el.scrollTop > 2;
    const down = el.scrollHeight - el.clientHeight - el.scrollTop > 2;
    setEdge((p) => (p.up === up && p.down === down ? p : { up, down }));
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    return () => ro.disconnect();
  }, [measure]);
  return (
    <div
      ref={ref} role="region" aria-label={label} tabIndex={0} onScroll={measure}
      className={`scroll ${className}${edge.up ? " fade-top" : ""}${edge.down ? " fade-bottom" : ""}`}
    >
      <div className="scroll-in">{children}</div>
    </div>
  );
}

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

function Stat({ label, value, delta, hint, spark, since, href, loading }: {
  label: string; value: string; delta?: number | null; hint?: string; spark?: number[]; since?: string; href: string; loading?: boolean;
}) {
  // Warna garis mengikuti arah garis itu sendiri, bukan tanda persen, agar keduanya selalu cocok.
  const trend = spark && spark.length > 1 ? spark[spark.length - 1] - spark[0] : delta;
  const color = trend == null || trend === 0 ? "var(--accent)" : trend < 0 ? "var(--down)" : "var(--up)";
  return (
    <Link to={href} className={`stat${loading ? " skeleton" : ""}`}>
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{loading ? "…" : value}</strong>
      <span className="stat-sub">
        {delta != null && (
          <b className={tone(delta)} title={since ? `Dibanding penutupan ${day(since)}` : undefined}>{pct(delta, 1, true)}</b>
        )}
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

type PfRow = { t: string; w: number; r: number };

/** Satu kalimat temuan: apakah risiko terpusat lebih besar daripada porsi bobotnya. */
function finding(rows: PfRow[]): string {
  if (rows.length < 2) return "Portofolio satu saham: seluruh risiko terpusat pada satu emiten.";
  const top = [...rows].sort((a, b) => b.r - a.r)[0];
  if (top.r - top.w > 0.05) return `${top.t} menyumbang ${pct(top.r, 1)} risiko dengan bobot ${pct(top.w, 1)}.`;
  return "Kontribusi risiko sejalan dengan bobot tiap saham.";
}

function PortfolioBody({ e, href }: { e: Evaluation; href: string }) {
  const [view, setView] = useState<"w" | "r">("w");
  const [hover, setHover] = useState<string | null>(null);
  const grown = useMounted();
  const sharpe = e.sharpe.current_weights;
  const animated = useCountUp(sharpe.sharpe);

  // Urutan dan warna sama dengan halaman Portofolio, supaya saham yang sama selalu berwarna sama.
  const rows: PfRow[] = useMemo(
    () => Object.keys(e.weights)
      .map((t) => ({ t, w: e.weights[t] ?? 0, r: e.risk_contribution[t] ?? 0 }))
      .filter((x) => x.w > 0)
      .sort((a, b) => b.w - a.w),
    [e],
  );
  const colorOf = (t: string) => `var(--s${(Math.max(0, rows.findIndex((x) => x.t === t)) % 8) + 1})`;

  const raw: Slice[] = rows.map((x) => ({ key: x.t, value: Math.max(0, view === "w" ? x.w : x.r), color: colorOf(x.t) }));
  const slices: Slice[] = (() => {
    if (raw.length <= 8) return raw;
    const sorted = [...raw].sort((a, b) => b.value - a.value);
    const rest = sorted.slice(7).reduce((s, x) => s + x.value, 0);
    return [...sorted.slice(0, 7), { key: "Lainnya", value: rest, color: "var(--s8)" }];
  })();
  const total = slices.reduce((s, x) => s + x.value, 0) || 1;
  const hot = hover ? slices.find((s) => s.key === hover) : undefined;

  return (
    <div className="hp-body">
      <div className="hp-top">
        <div className="hp-donut">
          <div className="donut-wrap">
            <Donut slices={slices} hover={hover} onHover={setHover} label={view === "w" ? "Komposisi bobot per saham" : "Kontribusi risiko per saham"} />
            <div className="donut-center">
              {hot ? <><strong>{pct(hot.value / total, 1)}</strong><small>{hot.key}</small></> : <><strong>{rows.length}</strong><small>saham</small></>}
            </div>
          </div>
          <Segmented
            label="Tampilan donat" value={view} onChange={setView}
            options={[{ value: "w", label: "Bobot" }, { value: "r", label: "Risiko" }]}
          />
        </div>

        <div className="hp-read">
          <Link to={href} className="hp-sharpe" title="Buka perhitungan lengkap">
            <span className="lbl">Sharpe ratio</span>
            <strong className={`hp-sharpe-val ${tone(sharpe.sharpe)}`}>{animated == null ? "-" : num(animated, 2)}</strong>
            <Chip kind={sharpe.verdict}>{VERDICT[sharpe.verdict]}</Chip>
          </Link>
          <p className="hp-finding">{finding(rows)}</p>
          {!e.window && <p className="hp-note">Estimasi cepat dari statistik universe. Perhitungan lengkap ada di halaman Portofolio.</p>}
        </div>
      </div>

      <div className="hp-cols" aria-hidden>
        <span /><span>Saham</span><span />
        <span className="n"><i className="sw bw" />Bobot</span>
        <span className="n"><i className="sw br" />Risiko</span>
      </div>
      <ScrollList label="Daftar saham portofolio" className="box pf">
        <ul className="hp-list">
          {rows.map((x) => (
            <li key={x.t}>
              <Link
                to={`/emiten/${x.t}`} title={`Buka ${x.t}`} className={`hp-row${hover === x.t ? " hot" : ""}`}
                onMouseEnter={() => setHover(x.t)} onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(x.t)} onBlur={() => setHover(null)}
              >
                <i className="hp-dot" style={{ background: colorOf(x.t) }} />
                <strong>{x.t}</strong>
                <span className="hp-bars">
                  <i className="bar bw" style={{ width: grown ? bar(x.w) : "0%" }} />
                  <i className="bar br" style={{ width: grown ? bar(x.r) : "0%" }} />
                </span>
                <span className="n">{pct(x.w, 1)}</span>
                <span className={`n${x.r - x.w > 0.05 ? " over" : ""}`}>{pct(x.r, 1)}</span>
              </Link>
            </li>
          ))}
        </ul>
      </ScrollList>
    </div>
  );
}

function PortfolioGlance({ name, loading, listError, evalError, e, href }: {
  name?: string; loading: boolean; listError: unknown; evalError?: string; e?: Evaluation; href: string;
}) {
  if (loading) return <Skel rows={5} />;
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
  return <PortfolioBody e={e} href={href} />;
}

/* ---------- Makro ---------- */

/** Indikator yang datanya terbaru, diurutkan dari perubahan 1 bulan terbesar. */
function freshMovers(rows: MacroRow[] | undefined): MacroRow[] {
  const time = (r: MacroRow) => new Date(r.last_date).getTime() || 0;
  const dated = (rows ?? []).filter((r) => r.change1m != null);
  const newest = Math.max(0, ...dated.map(time));
  return dated
    .filter((r) => newest - time(r) <= FRESH_DAYS * 86_400_000)
    .sort((a, b) => Math.abs(b.change1m ?? 0) - Math.abs(a.change1m ?? 0));
}

function MacroMovers({ rows, loading, error }: { rows: MacroRow[]; loading: boolean; error: boolean }) {
  if (loading) return <Skel rows={5} />;
  if (error) return <p className="muted">Data makro belum bisa dimuat.</p>;
  if (!rows.length) return <p className="muted">Belum ada data makro terbaru.</p>;
  return (
    <ScrollList label="Daftar pergerakan makro" className="box mac">
      <ul className="rows">
        {rows.map((r) => (
          <li key={r.key}>
            <Link to={`/makro?k=${encodeURIComponent(r.key)}`} className="row-link">
              <span className="row-main"><strong>{macroLabel(r)}</strong><small>{r.key}</small></span>
              <span className="row-meta"><span>{num(r.last_value, 2)}</span><b className={tone(r.change1m)}>{pct(r.change1m, 1, true)}</b></span>
            </Link>
          </li>
        ))}
      </ul>
    </ScrollList>
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
  const ihsgSeries = lastMonth(series.data?.series["IHSG"]);
  const fxSeries = lastMonth(series.data?.series["USDIDR"]);
  const counts = scan.data?.counts;
  const active = (counts?.STRONG ?? 0) + (counts?.MODERATE ?? 0);
  const signals = (scan.data?.rows ?? [])
    .filter((r) => r.status === "STRONG" || r.status === "MODERATE")
    .sort((a, b) => Math.abs(b.z_score ?? 0) - Math.abs(a.z_score ?? 0));
  const shown = signals.slice(0, SIGNAL_LIMIT);
  const movers = freshMovers(macro.data);

  const scanLoading = periods.isPending || (Boolean(latest) && scan.isPending);
  const pfLoading = pfs.isPending || (Boolean(pfName) && quick.isPending);
  // Nama portofolio hanya ditampilkan bila bukan bawaan.
  const pfTitle = pfName && pfName !== DEFAULT_PORTFOLIO ? `Portofolio · ${pfName}` : "Portofolio";
  const signalMeta = signals.length
    ? (shown.length < signals.length ? `${shown.length} dari ${signals.length}` : String(signals.length))
    : undefined;

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
        <Stat
          label="IHSG" value={num(ihsg?.last_value, 2)} delta={ihsg?.change1m} hint="1 bulan"
          spark={ihsgSeries?.y} since={ihsgSeries?.since} loading={macro.isPending} href="/makro?k=IHSG"
        />
        <Stat
          label="USD/IDR" value={num(fx?.last_value, 0)} delta={fx?.change1m} hint="1 bulan"
          spark={fxSeries?.y} since={fxSeries?.since} loading={macro.isPending} href="/makro?k=USDIDR"
        />
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
        <Panel title="Sinyal terbaru" meta={signalMeta} area="home-signals" fill action={<More to="/sinyal?tab=scan">Buka pemindai</More>}>
          {scanLoading ? <Skel rows={6} />
            : periods.isError || scan.isError ? <p className="muted">Hasil pemindaian belum tersedia.</p>
            : <ScrollList label="Daftar sinyal" className="abs"><SignalList rows={shown} /></ScrollList>}
        </Panel>
        <Panel
          title="Pergerakan makro 1 bulan" meta={movers.length ? `${movers.length} indikator` : undefined}
          area="home-macro" action={<More to="/makro">Jelajahi makro</More>}
        >
          <MacroMovers rows={movers} loading={macro.isPending} error={macro.isError} />
        </Panel>
      </div>
    </>
  );
}