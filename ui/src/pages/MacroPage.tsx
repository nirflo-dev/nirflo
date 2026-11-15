import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMacroOverview, useMacroSeries } from "../lib/api";
import { day, num, pct, tone } from "../lib/format";
import { useColors, useParam } from "../lib/hooks";
import type { MacroRow, MacroSeries } from "../lib/types";
import { baseOption, Chart, line, X, Y } from "../components/Chart";
import { Async, DataTable, Section, Segmented, type Col } from "../components/ui";

const YEARS: Record<string, number | null> = { "1 tahun": 1, "3 tahun": 3, "5 tahun": 5, "10 tahun": 10, Semua: null };

export default function MacroPage() {
  const q = useMacroOverview();
  return (
    <>
      <h2>Makro</h2>
      <Async q={q} isEmpty={(d) => d.length === 0 && "Belum ada data di macro_indicators."}>{(rows) => <MacroView rows={rows} />}</Async>
    </>
  );
}

function MacroView({ rows }: { rows: MacroRow[] }) {
  const c = useColors();
  const [sp, setSp] = useSearchParams();
  const [range, setRange] = useParam("rentang", "5 tahun");
  const [mode, setMode] = useParam("mode", "per");
  const [filter, setFilter] = useState("");
  const keys = rows.map((r) => r.key);
  const fromUrl = sp.getAll("k").filter((k) => keys.includes(k));
  const def = ["IHSG", "USDIDR"].filter((k) => keys.includes(k));
  const chosen = fromUrl.length ? fromUrl : def.length ? def : keys.slice(0, 2);
  const label = (k: string) => { const n = rows.find((r) => r.key === k)?.name; return n ? `${k} - ${n}` : k; };

  const toggle = (k: string) => {
    const next = chosen.includes(k) ? chosen.filter((x) => x !== k) : [...chosen, k];
    if (!next.length) return; // minimal satu indikator
    setSp((p) => { const n = new URLSearchParams(p); n.delete("k"); next.forEach((x) => n.append("k", x)); return n; }, { replace: true });
  };
  const visible = rows.filter((r) => label(r.key).toLowerCase().includes(filter.toLowerCase()));
  const cols: Col<MacroRow>[] = [
    { key: "sel", label: "Grafik", get: (r) => chosen.includes(r.key), fmt: (v, r) => <input type="checkbox" checked={v} aria-label={`Tampilkan ${r.key} di grafik`} onChange={() => toggle(r.key)} /> },
    { key: "key", label: "Indikator", get: (r) => label(r.key) },
    { key: "last_value", label: "Terakhir", num: true, fmt: (v) => num(v, 2) },
    { key: "last_date", label: "Tanggal", fmt: (v) => day(v) },
    { key: "change1m", label: "1 bulan", num: true, fmt: (v) => <span className={tone(v)}>{pct(v, 1, true)}</span> },
    { key: "change12m", label: "12 bulan", num: true, fmt: (v) => <span className={tone(v)}>{pct(v, 1, true)}</span> },
    { key: "source", label: "Sumber" },
  ];
  const s = useMacroSeries(chosen, YEARS[range]);

  return (
    <>
      <Section title="Daftar indikator">
        <label className="field"><span>Cari indikator</span><input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="mis. IHSG" /></label>
        <DataTable cols={cols} rows={visible} pageSize={15} csv="daftar_makro.csv" />
      </Section>
      <div className="toolbar">
        <Segmented label="Rentang" value={range} onChange={setRange} options={Object.keys(YEARS).map((k) => ({ value: k, label: k }))} />
        <Segmented label="Tampilan" value={mode} onChange={setMode} options={[{ value: "per", label: "Per indikator" }, { value: "cmp", label: "Perbandingan" }]} />
      </div>
      <Async q={s}>
        {(d) => (mode === "per"
          ? <div className="two">{chosen.map((k) => <Mini key={k} k={k} title={label(k)} d={d} i={chosen.indexOf(k)} />)}</div>
          : <Compare d={d} chosen={chosen} label={label} />)}
      </Async>
    </>
  );
}

function Mini({ k, title, d, i }: { k: string; title: string; d: MacroSeries; i: number }) {
  const c = useColors();
  const s = d.series[k];
  const opt = useMemo(() => s && baseOption(c, {
    legend: { show: false }, grid: { left: 56, right: 12, top: 12, bottom: 24 }, xAxis: X(c), yAxis: Y(c),
    series: [line(k, s.x, s.y, c.series[i % c.series.length])],
  }), [c, s, k, i]);
  return (
    <Section title={title}>
      {opt ? <Chart option={opt} height={260} label={`Grafik ${title}`} /> : <p className="muted">Tidak ada data pada rentang ini.</p>}
    </Section>
  );
}

function Compare({ d, chosen, label }: { d: MacroSeries; chosen: string[]; label: (k: string) => string }) {
  const c = useColors();
  const opt = useMemo(() => baseOption(c, {
    grid: { left: 56, right: 16, top: 36, bottom: 28 }, xAxis: X(c), yAxis: Y(c), dataZoom: [{ type: "inside" }],
    series: chosen.flatMap((k, i) => {
      const s = d.series[k];
      if (!s || !s.y[0]) return [];
      return [line(label(k), s.x, s.y.map((v) => (v / s.y[0]) * 100), c.series[i % c.series.length], {
        ...(i === 0 ? { markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: c.muted }, data: [{ yAxis: 100 }] } } : {}),
      })];
    }),
  }), [c, d, chosen, label]);
  const cor = d.correlation;
  return (
    <>
      <Section title="Indeks (awal rentang = 100)"><Chart option={opt} height={440} label="Perbandingan indikator, dinormalisasi ke 100" /></Section>
      {cor && (
        <Section title="Korelasi perubahan bulanan">
          <div className="tablewrap"><table>
            <thead><tr><th />{cor.keys.map((k) => <th key={k} className="r">{k}</th>)}</tr></thead>
            <tbody>{cor.keys.map((k, i) => (
              <tr key={k}><th scope="row">{k}</th>{cor.matrix[i].map((v, j) => (
                <td key={j} className="r" style={{ background: `color-mix(in srgb, var(--accent) ${Math.abs(v ?? 0) * 55}%, transparent)` }}>{num(v, 2)}</td>
              ))}</tr>
            ))}</tbody>
          </table></div>
        </Section>
      )}
    </>
  );
}