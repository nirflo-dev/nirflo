import { useEffect, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent } from "react";
import { TickerSearch } from "../components/TickerSearch";
import { useFx, usePortfolioMutations, type Currency } from "../lib/portfolio";

export type Composer = "saham" | "cash";
export type CashBalance = { idr: number; usd: number };
type CashOp = "add" | "sub" | "set";

const nf = (v: number, digits = 0) =>
  v.toLocaleString("id-ID", { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const rp = (v: number) => `Rp ${nf(Math.round(v))}`;
export const usd = (v: number) => `US$ ${nf(v, 2)}`;
const money = (v: number, cur: Currency) => (cur === "IDR" ? rp(v) : usd(v));

/* ---------- input angka: format ribuan saat mengetik ---------- */

const sanitize = (text: string, cur: Currency) => {
  if (cur === "IDR") return text.replace(/\D/g, "").replace(/^0+(?=\d)/, "").slice(0, 16);
  const t = text.replace(/\./g, "").replace(",", ".").replace(/[^\d.]/g, "");
  const [int = "", ...rest] = t.split(".");
  const head = int.replace(/^0+(?=\d)/, "").slice(0, 16);
  return rest.length ? `${head || "0"}.${rest.join("").slice(0, 2)}` : head;
};

const display = (raw: string) => {
  const [int = "", dec] = raw.split(".");
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return dec !== undefined ? `${grouped || "0"},${dec}` : grouped;
};

/* ---------- animasi angka ---------- */

function useTween(target: number, ms = 450) {
  const [value, setValue] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      from.current = target;
      setValue(target);
      return;
    }
    const start = from.current;
    const t0 = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / ms);
      const cur = start + (target - start) * (1 - Math.pow(1 - k, 3));
      from.current = cur;
      setValue(cur);
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return value;
}

/* ---------- kontrol segmented dengan pil yang bergeser ---------- */

function Pill<V extends string>({ value, options, onChange, label, small = false }: {
  value: V; options: readonly { value: V; label: string }[]; onChange: (v: V) => void; label: string; small?: boolean;
}) {
  const index = Math.max(0, options.findIndex((o) => o.value === value));
  return (
    <div
      className={`pill${small ? " sm" : ""}`} role="radiogroup" aria-label={label}
      style={{ "--n": options.length, "--i": index } as CSSProperties}
    >
      <i className="pill-thumb" aria-hidden />
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- ikon ---------- */

const WalletIcon = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M20 12V8a2 2 0 0 0-2-2H5a2 2 0 0 1 0-4h11" /><path d="M3 4v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-4" /><path d="M21 12h-4a2 2 0 0 0 0 4h4z" />
  </svg>
);
const StockIcon = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" />
  </svg>
);
const CheckIcon = () => (
  <svg className="pfx-check" viewBox="0 0 52 52" aria-hidden>
    <circle cx="26" cy="26" r="24" fill="none" strokeWidth="3" />
    <path d="M15 27l8 8 14-16" fill="none" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/* ---------- form cash ---------- */

const PRESETS: Record<Currency, readonly { label: string; value: number }[]> = {
  IDR: [
    { label: "1 jt", value: 1_000_000 },
    { label: "5 jt", value: 5_000_000 },
    { label: "10 jt", value: 10_000_000 },
    { label: "50 jt", value: 50_000_000 },
    { label: "100 jt", value: 100_000_000 },
  ],
  USD: [
    { label: "100", value: 100 },
    { label: "500", value: 500 },
    { label: "1.000", value: 1_000 },
    { label: "5.000", value: 5_000 },
  ],
};
const OP_TEXT: Record<CashOp, string> = {
  add: "Jumlah yang ditambahkan",
  sub: "Jumlah yang dikurangi",
  set: "Saldo baru",
};

function CashForm({ name, cash, onDone }: { name: string; cash: CashBalance; onDone: () => void }) {
  const { setCash } = usePortfolioMutations(name);
  const fx = useFx();
  const [cur, setCur] = useState<Currency>("IDR");
  const [op, setOp] = useState<CashOp>("add");
  const [raw, setRaw] = useState("");
  const [done, setDone] = useState<{ cur: Currency; value: number } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const balance = cur === "IDR" ? cash.idr : cash.usd;
  const other = cur === "IDR" ? cash.usd : cash.idr;
  const amount = raw === "" || raw === "." ? NaN : Number(raw);
  const has = Number.isFinite(amount) && amount >= 0;
  const target = !has ? null : op === "add" ? balance + amount : op === "sub" ? balance - amount : amount;
  const next = target == null ? null : Math.round(target * 100) / 100;
  const over = next != null && next < 0;
  const valid = next != null && next >= 0 && (op === "set" || amount > 0);
  const delta = next == null ? 0 : next - balance;

  const shownBalance = useTween(balance);
  const shownNext = useTween(next ?? balance);
  const scale = Math.max(balance, next != null && next > 0 ? next : 0, 1);
  const rate = fx.data?.rate;

  useEffect(() => {
    input.current?.focus({ preventScroll: true });
  }, [cur, op]);

  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => {
      setDone(null);
      onDoneRef.current();
    }, 1700);
    return () => clearTimeout(t);
  }, [done]);

  const pickCur = (c: Currency) => {
    setCur(c);
    setRaw("");
  };
  const bump = (v: number) => setRaw(sanitize(String((has ? amount : 0) + v), cur));
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (cur === "USD" && e.key === "." && !raw.includes(".")) {
      e.preventDefault();
      setRaw(raw === "" ? "0." : `${raw}.`);
    }
  };
  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    if (!valid || next == null) return;
    setCash.mutate(
      { amount: next, currency: cur },
      { onSuccess: () => { setRaw(""); setDone({ cur, value: next }); } },
    );
  };

  if (done) {
    return (
      <div className="pfx-done" role="status">
        <CheckIcon />
        <strong>Cash tersimpan</strong>
        <p>Saldo {done.cur === "IDR" ? "rupiah" : "dolar"} kini {money(done.value, done.cur)}.</p>
      </div>
    );
  }

  return (
    <form className="cf" onSubmit={submit} noValidate>
      <div className="cf-top">
        <Pill
          label="Mata uang" value={cur} onChange={pickCur}
          options={[{ value: "IDR", label: "Rupiah · IDR" }, { value: "USD", label: "Dolar · USD" }]}
        />
        <Pill
          small label="Jenis perubahan" value={op} onChange={setOp}
          options={[{ value: "add", label: "Tambah" }, { value: "sub", label: "Kurangi" }, { value: "set", label: "Atur saldo" }]}
        />
      </div>

      <div className="cf-balance">
        <div>
          <small>Saldo {cur === "IDR" ? "rupiah" : "dolar"} sekarang</small>
          <strong>{money(shownBalance, cur)}</strong>
        </div>
        <div className="cf-side">
          {other > 0 && <span>{cur === "IDR" ? "Cash dolar" : "Cash rupiah"}: {money(other, cur === "IDR" ? "USD" : "IDR")}</span>}
          {cur === "USD" && (
            <span className="cf-fx">
              {rate ? `Kurs USD/IDR ${nf(rate, 0)}${fx.data ? ` · ${fx.data.date}` : ""}` : fx.isPending ? "Memuat kurs…" : "Kurs belum tersedia"}
            </span>
          )}
        </div>
      </div>

      <label className="amt">
        <span className="amt-label">{OP_TEXT[op]}</span>
        <span className={`amt-box${over ? " bad" : ""}`} key={over ? "bad" : "ok"}>
          <span className="amt-sym">{cur === "IDR" ? "Rp" : "US$"}</span>
          <input
            ref={input} value={display(raw)} placeholder="0" autoComplete="off"
            inputMode={cur === "IDR" ? "numeric" : "decimal"} aria-invalid={over}
            onChange={(e) => setRaw(sanitize(e.target.value, cur))} onKeyDown={onKey}
          />
        </span>
      </label>

      {op !== "set" && (
        <div className="chips" role="group" aria-label="Jumlah cepat" key={`${cur}-${op}`}>
          {PRESETS[cur].map((p, i) => (
            <button type="button" key={p.value} style={{ "--i": i } as CSSProperties} onClick={() => bump(p.value)}>{p.label}</button>
          ))}
          {op === "sub" && balance > 0 && (
            <button type="button" className="all" style={{ "--i": PRESETS[cur].length } as CSSProperties} onClick={() => setRaw(sanitize(String(balance), cur))}>
              Semua saldo
            </button>
          )}
        </div>
      )}

      <div className={`cf-preview${next == null ? " idle" : ""}`} aria-live="polite">
        <div className="cf-row">
          <span>Sekarang</span>
          <i className="cf-bar"><b style={{ width: `${(balance / scale) * 100}%` }} /></i>
          <em>{money(balance, cur)}</em>
        </div>
        <div className="cf-row after">
          <span>Setelah</span>
          <i className={`cf-bar${over ? " bad" : ""}`}><b style={{ width: next == null || over ? "0%" : `${(next / scale) * 100}%` }} /></i>
          <em>{next == null ? "-" : over ? "-" : money(shownNext, cur)}</em>
        </div>
        <div className="cf-foot">
          {next == null && <span>Isi jumlah untuk melihat saldo baru.</span>}
          {over && <span className="bad">Jumlah melebihi saldo cash saat ini.</span>}
          {next != null && !over && delta !== 0 && (
            <span className={`delta ${delta > 0 ? "up" : "down"}`} key={`${op}-${delta}`}>
              {delta > 0 ? "+" : "-"}{money(Math.abs(delta), cur)}
            </span>
          )}
          {next != null && !over && cur === "USD" && rate && (
            <span className="eq">Setara {rp(next * rate)}</span>
          )}
        </div>
      </div>

      <button className="primary pfx-submit" type="submit" disabled={!valid || setCash.isPending}>
        {setCash.isPending ? "Menyimpan…" : "Simpan cash"}
      </button>
      {setCash.isError && <p className="down" role="alert">{setCash.error.message}</p>}
    </form>
  );
}

/* ---------- form saham ---------- */

function AddHolding({ name }: { name: string }) {
  const { add } = usePortfolioMutations(name);
  const [ticker, setTicker] = useState<string | null>(null);
  const [lots, setLots] = useState("");
  const [date, setDate] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    if (!ticker) return;
    const t = ticker;
    add.mutate(
      { ticker: t, lots: Number(lots), buy_date: date },
      { onSuccess: () => { setSaved(t); setTicker(null); setLots(""); } },
    );
  };
  return (
    <form className="pfx-form" onSubmit={submit}>
      <div className="field">
        <span>Emiten</span>
        {ticker ? (
          <span className="pfx-pick"><strong>{ticker}</strong><button type="button" className="pfx-link" onClick={() => setTicker(null)}>Ganti</button></span>
        ) : (
          <TickerSearch compact remember={false} label="Cari emiten" placeholder="Kode atau sektor" onPick={(t) => { setSaved(null); setTicker(t); }} />
        )}
      </div>
      <label className="field">
        <span>
          Jumlah lot
          <small>{Number(lots) > 0 ? `${Math.round(Number(lots) * 100).toLocaleString("id-ID")} lembar` : "1 lot = 100 lembar"}</small>
        </span>
        <span className="pfx-unit">
          <input required type="number" inputMode="decimal" min={0.01} step={0.01} value={lots} onChange={(e) => setLots(e.target.value)} />
          <em aria-hidden>lot</em>
        </span>
      </label>
      <label className="field">
        <span>Tanggal beli</span>
        <input required type="date" max={today} value={date} onChange={(e) => setDate(e.target.value)} />
      </label>
      <button className="primary" type="submit" disabled={add.isPending || !ticker}>{add.isPending ? "Menyimpan…" : "Simpan posisi"}</button>
      {saved && (
        <p className="pfx-saved" role="status" key={saved}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M5 13l4 4L19 7" /></svg>
          {saved} tersimpan. Tambahkan saham lain atau tutup panel ini.
        </p>
      )}
      {add.isError && <p className="down" role="alert">{add.error.message}</p>}
    </form>
  );
}

/* ---------- panel ---------- */

export function AddPanel({ name, cash, tab, setTab, onClose, inline = false }: {
  name: string; cash: CashBalance; tab: Composer; setTab: (t: Composer) => void; onClose: () => void; inline?: boolean;
}) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!inline) ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [inline, tab]);
  return (
    <section className={`card pfx-add${inline ? " inline" : ""}`} ref={ref} aria-label="Tambah ke portofolio">
      <div className="pfx-add-head">
        <span className="pfx-badge" key={tab} aria-hidden>{tab === "saham" ? <StockIcon /> : <WalletIcon />}</span>
        <div className="pfx-add-title">
          <strong>Tambah ke “{name}”</strong>
          <p>{tab === "saham" ? "Catat pembelian saham beserta tanggal belinya." : "Catat saldo kas dalam rupiah atau dolar."}</p>
        </div>
        <Pill
          label="Jenis yang ditambahkan" value={tab} onChange={setTab}
          options={[{ value: "saham", label: "Saham" }, { value: "cash", label: "Cash" }]}
        />
        {!inline && <button type="button" className="pfx-x" onClick={onClose} aria-label="Tutup panel">Tutup</button>}
      </div>
      <div className="pfx-swap" key={tab}>
        {tab === "saham" ? <AddHolding name={name} /> : <CashForm name={name} cash={cash} onDone={inline ? () => undefined : onClose} />}
      </div>
    </section>
  );
}