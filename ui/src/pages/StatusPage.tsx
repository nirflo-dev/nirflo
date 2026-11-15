import { useMeta, useStatus } from "../lib/api";
import { day } from "../lib/format";
import { Async, DataGrid, Kpi, Section } from "../components/ui";

const SECTIONS = [["prices", "Harga"], ["signals", "Sinyal"], ["reports", "Laporan keuangan terbaru"], ["scans", "Hasil pemindaian"], ["macro", "Makro (paling lama diperbarui di atas)"]] as const;

export default function StatusPage() {
  const q = useStatus();
  const m = useMeta().data;
  return (
    <>
      <h2>Status data</h2>
      {m && (
        <div className="kpis">
          <Kpi label="Sumber data" value={m.source === "database" ? "Database" : "Snapshot R2"} />
          <Kpi label="Harga terakhir" value={day(m.lastPriceDate)} tone={m.ageDays != null && m.ageDays > 4 ? "down" : ""} hint="Lebih dari 4 hari dianggap tertinggal." />
        </div>
      )}
      <Async q={q}>
        {(d) => (
          <div className="two">
            {SECTIONS.map(([k, title]) => {
              const s = d[k];
              if (!s) return null;
              return (
                <Section key={k} title={title}>
                  {"error" in s ? <p className="down" role="alert">Query gagal: {s.error}</p> : <DataGrid data={s} />}
                </Section>
              );
            })}
          </div>
        )}
      </Async>
    </>
  );
}