import { useEffect, useRef, useState } from "react";
import { settingsUrl, startOpenRouterLogin, useAiStatus, useDisconnectAi } from "../lib/ai";
import "../ai.css";

const POINTS = [
  "Opsional dan terpisah: Nirflo berfungsi penuh tanpa AI, dan AI tidak mengubah hitungan atau data.",
  "Key disimpan terenkripsi di server dan tidak pernah dikirim ke browser.",
  "Biaya dibebankan ke kredit OpenRouter-mu. Akses bisa dicabut kapan saja.",
];

/**
 * Halaman biasa: setelah tersambung, kartu diringkas jadi satu baris dengan menu ⋯ (Kelola / Putuskan tersembunyi).
 * flat: dipakai di modal akun Clerk, menampilkan pengaturan lengkap tanpa kotak.
 */
export function OpenRouterConnect({ flat = false }: { flat?: boolean }) {
  const status = useAiStatus();
  const disconnect = useDisconnectAi();
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revokeUrl, setRevokeUrl] = useState<string | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  const s = status.data;
  const connected = s?.connected === true;
  const manageUrl = settingsUrl(s?.keyHash ?? null);

  useEffect(() => {
    if (!menu) return;
    const outside = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) { setMenu(false); setConfirming(false); }
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setMenu(false); setConfirming(false); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [menu]);

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      await startOpenRouterLogin();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Gagal memulai sambungan.");
      setBusy(false);
    }
  };

  const drop = () => {
    const url = manageUrl;
    setError(null);
    disconnect.mutate(undefined, {
      onSuccess: () => { setRevokeUrl(url); setConfirming(false); setMenu(false); },
      onError: (e) => setError(e.message),
    });
  };

  if (connected && !flat) {
    return (
      <div className="ai-bar" ref={wrap}>
        <span className="ai-badge ok"><span className="ai-dot" aria-hidden />OpenRouter tersambung</span>
        <button
          type="button" className="ai-icon-btn" aria-label="Pengaturan koneksi" aria-haspopup="menu" aria-expanded={menu}
          onClick={() => { setMenu((v) => !v); setConfirming(false); }}
        >
          <span aria-hidden>⋯</span>
        </button>
        {menu && (
          <div className="ai-pop ai-menu" role="menu">
            {manageUrl && <a role="menuitem" href={manageUrl} target="_blank" rel="noreferrer">Kelola di OpenRouter</a>}
            {confirming ? (
              <div className="ai-confirm">
                <span>Putuskan sambungan?</span>
                <button type="button" className="danger" onClick={drop} disabled={disconnect.isPending}>
                  {disconnect.isPending ? "Memutus…" : "Ya"}
                </button>
                <button type="button" onClick={() => setConfirming(false)}>Batal</button>
              </div>
            ) : (
              <button type="button" role="menuitem" className="danger" onClick={() => setConfirming(true)}>Putuskan</button>
            )}
            {error && <p className="ai-menu-err" role="alert">{error}</p>}
          </div>
        )}
      </div>
    );
  }

  return (
    <section className={`ai-card${flat ? " flat" : ""}`} aria-label="Koneksi OpenRouter">
      <header className="ai-head">
        <div>
          <h2>{flat ? "Koneksi AI" : "OpenRouter"}</h2>
          <p className="ai-muted">Pakai model AI dari akun OpenRouter milikmu sendiri.</p>
        </div>
        {s?.configured && (
          <span className={`ai-badge${connected ? " ok" : ""}`}>
            <span className="ai-dot" aria-hidden />
            {connected ? "Tersambung" : "Belum tersambung"}
          </span>
        )}
      </header>

      {status.isPending && <p className="ai-muted">Memeriksa sambungan…</p>}

      {status.isError && (
        <div className="ai-notice err" role="alert">
          <span>Fitur AI belum bisa dipakai. {status.error.message}</span>
          <button type="button" className="ai-btn ghost" onClick={() => status.refetch()}>Coba lagi</button>
        </div>
      )}

      {s && !s.configured && (
        <div className="ai-notice warn" role="status">Fitur AI belum diaktifkan di server. Hubungi administrator Nirflo.</div>
      )}

      {s?.configured && !connected && (
        <>
          <ul className="ai-points">{POINTS.map((p) => <li key={p}>{p}</li>)}</ul>
          <div className="ai-actions">
            <button type="button" className="ai-btn primary" onClick={connect} disabled={busy}>
              {busy ? "Mengalihkan ke OpenRouter…" : "Sambungkan OpenRouter"}
            </button>
          </div>
        </>
      )}

      {connected && (
        <>
          <p className="ai-muted">Penggunaan model dibebankan ke kredit OpenRouter-mu.</p>
          {confirming ? (
            <div className="ai-actions" role="group" aria-label="Konfirmasi putus sambungan">
              <span className="ai-muted">Putuskan sambungan AI?</span>
              <button type="button" className="ai-btn danger" onClick={drop} disabled={disconnect.isPending}>
                {disconnect.isPending ? "Memutus…" : "Ya, putuskan"}
              </button>
              <button type="button" className="ai-btn ghost" onClick={() => setConfirming(false)}>Batal</button>
            </div>
          ) : (
            <div className="ai-actions">
              {manageUrl && <a className="ai-btn ghost" href={manageUrl} target="_blank" rel="noreferrer">Kelola di OpenRouter</a>}
              <button type="button" className="ai-btn ghost" onClick={() => setConfirming(true)}>Putuskan</button>
            </div>
          )}
        </>
      )}

      {revokeUrl && !connected && (
        <div className="ai-notice ok" role="status">
          Terputus dari Nirflo. Untuk mencabut akses sepenuhnya, hapus key-nya di{" "}
          <a href={revokeUrl} target="_blank" rel="noreferrer">pengaturan OpenRouter</a>.
        </div>
      )}

      {error && <div className="ai-notice err" role="alert">{error}</div>}
    </section>
  );
}