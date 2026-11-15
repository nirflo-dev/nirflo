import { lazy, Suspense } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, useMeta } from "./lib/api";
import { day, stampLabel } from "./lib/format";
import { Empty, ErrorBoundary, ErrorState, Loading } from "./components/ui";

const EmitenPage = lazy(() => import("./pages/EmitenPage"));
const SignalsPage = lazy(() => import("./pages/SignalsPage"));
const MacroPage = lazy(() => import("./pages/MacroPage"));
const StatusPage = lazy(() => import("./pages/StatusPage"));

const NAV = [["/emiten", "Emiten"], ["/sinyal", "Sinyal"], ["/makro", "Makro"], ["/status", "Status data"]] as const;

export default function App() {
  const meta = useMeta();
  const qc = useQueryClient();
  const { pathname } = useLocation();
  const refresh = useMutation({
    mutationFn: () => api("/refresh", undefined, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries(),
  });
  const m = meta.data;
  const stale = m?.ageDays != null && m.ageDays > 4; // melewati akhir pekan dan libur

  return (
    <div className="shell">
      <a className="skip" href="#main">Lewati ke konten</a>
      <aside className="side">
        <h1>Mintigo</h1>
        <nav aria-label="Halaman utama">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>{label}</NavLink>
          ))}
        </nav>
        <div className="source" aria-live="polite">
          {m ? (
            <>
              <span>
                <span className={`dot ${m.source === "database" ? "ok" : "warn"}`} aria-hidden />
                {m.source === "database" ? "Database" : `Snapshot R2 ${m.snapshot ? stampLabel(m.snapshot) : ""}`}
              </span>
              <small className={stale ? "down" : ""}>Harga terakhir: {day(m.lastPriceDate)}{stale ? " (tertinggal)" : ""}</small>
            </>
          ) : meta.isPending ? "Memeriksa sumber data…" : null}
        </div>
        <button onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          {refresh.isPending ? "Memuat…" : "Muat ulang data"}
        </button>
      </aside>
      <main id="main">
        {meta.isError ? (
          <ErrorState error={meta.error} retry={() => meta.refetch()} />
        ) : (
          <>
            {m?.source === "snapshot" && (
              <div className="banner" role="status">
                Database tidak terjangkau. Tampilan memakai snapshot R2; Kinerja sinyal dan beberapa tabel mungkin tidak tersedia.
              </div>
            )}
            <ErrorBoundary key={pathname}>
              <Suspense fallback={<Loading />}>
                <Routes>
                  <Route path="/" element={<Navigate to="/emiten" replace />} />
                  <Route path="/emiten/:ticker?" element={<EmitenPage />} />
                  <Route path="/sinyal" element={<SignalsPage />} />
                  <Route path="/makro" element={<MacroPage />} />
                  <Route path="/status" element={<StatusPage />} />
                  <Route path="*" element={<Empty>Halaman tidak ditemukan.</Empty>} />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </>
        )}
      </main>
    </div>
  );
}