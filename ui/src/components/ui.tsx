import { Component, useEffect, useMemo, useState, type ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { downloadCsv, num } from "../lib/format";
import type { TableData } from "../lib/types";

export const Loading = ({ label = "Memuat…" }: { label?: string }) => (
  <div className="state" role="status" aria-live="polite"><span className="spin" aria-hidden />{label}</div>
);
export const Empty = ({ children }: { children: ReactNode }) => <div className="state empty">{children}</div>;

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <div className="state error" role="alert">
      <strong>Gagal memuat data.</strong>
      <span>{error instanceof Error ? error.message : String(error)}</span>
      {retry && <button onClick={retry}>Coba lagi</button>}
    </div>
  );
}

/** Menangkap error saat render sehingga halaman tidak kosong total. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { console.error(error); }
  render() {
    if (this.state.error) {
      return (
        <div className="state error" role="alert">
          <strong>Halaman gagal ditampilkan.</strong>
          <span>{this.state.error.message}</span>
          <button onClick={() => this.setState({ error: null })}>Coba lagi</button>
        </div>
      );
    }
    return this.props.children;
  }
}

/** Satu pola untuk loading / error / kosong / data. Data lama tetap tampil (redup) saat parameter berubah. */
export function Async<R>({ q, isEmpty, children }: {
  q: UseQueryResult<R>; isEmpty?: (d: R) => string | false; children: (d: R) => ReactNode;
}) {
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} retry={() => q.refetch()} />;
  const msg = isEmpty?.(q.data);
  if (msg) return <Empty>{msg}</Empty>;
  return <div className={q.isPlaceholderData ? "stale" : ""} aria-busy={q.isPlaceholderData}>{children(q.data)}</div>;
}

export const Kpi = ({ label, value, hint, tone = "" }: { label: string; value: ReactNode; hint?: string; tone?: string }) => (
  <div className="kpi" title={hint}><span>{label}</span><strong className={tone}>{value}</strong></div>
);

export function Segmented<V extends string>({ value, options, onChange, label }: {
  value: V; options: readonly { value: V; label: string }[]; onChange: (v: V) => void; label: string;
}) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={o.value === value} className={o.value === value ? "on" : ""} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs({ value, tabs, onChange }: { value: string; tabs: { id: string; label: string }[]; onChange: (v: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={t.id === value} className={t.id === value ? "on" : ""} onClick={() => onChange(t.id)}>{t.label}</button>
      ))}
    </div>
  );
}

export const Section = ({ title, children }: { title: string; children: ReactNode }) => (
  <section className="card"><h3>{title}</h3>{children}</section>
);

/** Input angka: nilai dikomit saat Enter/blur, jadi mengetik tidak memicu hitung ulang di tiap tombol. */
export function NumberField({ label, value, onChange, min, max, step, hint }: {
  label: string; value: number; onChange: (v: number) => void; min: number; max: number; step: number; hint?: string;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = (s: string) => {
    const n = Number(s);
    if (s.trim() !== "" && Number.isFinite(n)) onChange(Math.min(max, Math.max(min, n)));
    else setDraft(String(value));
  };
  return (
    <label className="field">
      <span>{label}</span>
      <input type="number" inputMode="decimal" min={min} max={max} step={step} value={draft}
        onChange={(e) => setDraft(e.target.value)} onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && commit(e.currentTarget.value)} />
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function TextField({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <label className="field">
      <span>{label}</span>
      <input value={draft} placeholder={placeholder} onChange={(e) => setDraft(e.target.value)}
        onBlur={(e) => onChange(e.target.value)} onKeyDown={(e) => e.key === "Enter" && onChange(e.currentTarget.value)} />
    </label>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Col<R> = { key: string; label: string; get?: (r: R) => unknown; fmt?: (v: any, r: R) => ReactNode; num?: boolean };
const val = <R,>(c: Col<R>, r: R) => (c.get ? c.get(r) : (r as Record<string, unknown>)[c.key]);

export function DataTable<R extends object>({ cols, rows, pageSize = 50, csv, sort: initial }: {
  cols: Col<R>[]; rows: R[]; pageSize?: number; csv?: string; sort?: { key: string; dir: 1 | -1 };
}) {
  const [sort, setSort] = useState(initial ?? null);
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [rows]);
  const sorted = useMemo(() => {
    const c = cols.find((x) => x.key === sort?.key);
    if (!c || !sort) return rows;
    return [...rows].sort((a, b) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const x = val(c, a) as any, y = val(c, b) as any;
      if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1; // null selalu di bawah
      return (x > y ? 1 : x < y ? -1 : 0) * sort.dir;
    });
  }, [rows, cols, sort]);
  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const view = sorted.slice(page * pageSize, (page + 1) * pageSize);

  return (
    <div>
      <div className="tablewrap" tabIndex={0} role="region" aria-label="Tabel data">
        <table>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.key} className={c.num ? "r" : ""} aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                  <button onClick={() => setSort(sort?.key === c.key ? (sort.dir === 1 ? { key: c.key, dir: -1 } : null) : { key: c.key, dir: 1 })}>
                    {c.label}{sort?.key === c.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.map((r, i) => (
              <tr key={i}>
                {cols.map((c) => {
                  const v = val(c, r);
                  return <td key={c.key} className={c.num ? "r" : ""}>{c.fmt ? c.fmt(v, r) : v == null ? "-" : String(v)}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="tablebar">
        <span>{sorted.length} baris</span>
        {pages > 1 && (
          <span>
            <button disabled={page === 0} onClick={() => setPage(page - 1)}>‹</button> {page + 1}/{pages}{" "}
            <button disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>›</button>
          </span>
        )}
        {csv && <button onClick={() => downloadCsv(csv, cols.map((c) => c.label), sorted.map((r) => cols.map((c) => val(c, r))))}>Unduh CSV</button>}
      </div>
    </div>
  );
}

/** Tabel generik dari payload {columns, rows} milik API. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function DataGrid({ data, fmt = {}, csv }: { data: TableData; fmt?: Record<string, (v: any) => ReactNode>; csv?: string }) {
  const first = data.rows[0] ?? {};
  const cols: Col<Record<string, unknown>>[] = data.columns.map((k) => ({
    key: k, label: k === "Item" ? "" : k, num: typeof first[k] === "number",
    fmt: fmt[k] ?? ((v) => (typeof v === "number" ? num(v, 2) : v == null ? "-" : String(v))),
  }));
  return <DataTable cols={cols} rows={data.rows} csv={csv} />;
}

export const Chip = ({ children, kind }: { children: ReactNode; kind?: string }) => <span className={`chip ${kind ?? ""}`}>{children}</span>;