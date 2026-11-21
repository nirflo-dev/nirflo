import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../lib/api";
import { AI_STATUS_KEY, askAi, useAiModels, useAiStatus, type ChatMessage } from "../lib/ai";
import { store } from "../lib/hooks";
import { OpenRouterConnect } from "../components/OpenRouterConnect";
import "../ai.css";

type Flash = { kind: "ok" } | { kind: "err"; message: string };

const MODEL_KEY = "aiModel";
const HISTORY_LIMIT = 12;
const STARTERS = [
  "Jelaskan Sharpe ratio dengan bahasa sederhana.",
  "Apa arti z-score pada sinyal pertumbuhan laba?",
  "Bagaimana cara membaca kontribusi risiko di sebuah portofolio?",
];

export default function AiPage() {
  const nav = useNavigate();
  const { state, pathname } = useLocation();
  const [flash, setFlash] = useState<Flash | null>(() => (state as { flash?: Flash } | null)?.flash ?? null);
  const status = useAiStatus();

  // Hasil dari halaman callback dibaca sekali, lalu dibersihkan supaya tidak muncul lagi setelah refresh.
  useEffect(() => {
    if (state) nav(pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="ai-page">
      <header>
        <h2>Asisten AI</h2>
        <p className="ai-muted">Opsional. Hubungkan akun OpenRouter-mu untuk berdiskusi dengan model AI pilihanmu.</p>
      </header>

      {flash?.kind === "ok" && (
        <div className="ai-notice ok" role="status">
          OpenRouter berhasil tersambung.
          <button type="button" className="ai-btn ghost" onClick={() => setFlash(null)}>Tutup</button>
        </div>
      )}
      {flash?.kind === "err" && (
        <div className="ai-notice err" role="alert">
          {flash.message}
          <button type="button" className="ai-btn ghost" onClick={() => setFlash(null)}>Tutup</button>
        </div>
      )}

      <OpenRouterConnect />
      {status.data?.connected && <AiChat />}
    </div>
  );
}

function AiChat() {
  const qc = useQueryClient();
  const models = useAiModels();
  const [model, setModel] = useState(() => store.get(MODEL_KEY) ?? "");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [messages, busy]);

  const pickModel = (value: string) => {
    setModel(value);
    store.set(MODEL_KEY, value.trim());
  };

  const send = async (text = draft) => {
    const content = text.trim();
    if (!content || !model.trim() || busy) return;
    const next: ChatMessage[] = [...messages, { role: "user", content }];
    setMessages(next);
    setDraft("");
    setError(null);
    setBusy(true);
    const ac = new AbortController();
    abort.current = ac;
    try {
      const res = await askAi({ model: model.trim(), messages: next.slice(-HISTORY_LIMIT), signal: ac.signal });
      setMessages([...next, { role: "assistant", content: res.content || "(Model tidak mengembalikan jawaban.)" }]);
    } catch (e) {
      if (ac.signal.aborted) return;
      setError(e instanceof Error ? e.message : "Permintaan gagal.");
      if (e instanceof ApiError && e.status === 409) qc.invalidateQueries({ queryKey: AI_STATUS_KEY });
    } finally {
      if (!ac.signal.aborted) setBusy(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const list = models.data ?? [];
  const ready = model.trim() !== "";

  return (
    <section className="ai-card" aria-label="Obrolan AI">
      <header className="ai-head">
        <div>
          <h2>Obrolan</h2>
          <p className="ai-muted">AI tidak punya akses ke data atau hitungan di Nirflo.</p>
        </div>
        {messages.length > 0 && (
          <button type="button" className="ai-btn ghost" onClick={() => { setMessages([]); setError(null); }}>
            Obrolan baru
          </button>
        )}
      </header>

      <label className="ai-field">
        <span>Model</span>
        <input
          list="ai-models" value={model} onChange={(e) => pickModel(e.target.value)} spellCheck={false}
          placeholder={models.isPending ? "Memuat daftar model…" : "Pilih atau ketik, mis. openai/gpt-4o-mini"}
        />
        <datalist id="ai-models">
          {list.map((m) => <option key={m.id} value={m.id}>{m.name}{m.free ? " · gratis" : ""}</option>)}
        </datalist>
        {models.isError && <small className="ai-muted">Daftar model tidak dapat dimuat. Kamu tetap bisa mengetik id model.</small>}
      </label>

      <div className="ai-log" role="log" aria-live="polite">
        {messages.length === 0 && (
          <div className="ai-empty">
            <p className="ai-muted">Mulai dengan salah satu pertanyaan ini, atau tulis sendiri.</p>
            <div className="ai-actions">
              {STARTERS.map((q) => (
                <button key={q} type="button" className="ai-chip" disabled={!ready || busy} onClick={() => void send(q)}>{q}</button>
              ))}
            </div>
            {!ready && <p className="ai-muted">Pilih model dulu untuk mulai.</p>}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`ai-msg ${m.role}`}>{m.content}</div>
        ))}
        {busy && <div className="ai-msg assistant pending">Sedang menjawab…</div>}
        <div ref={end} />
      </div>

      {error && <div className="ai-notice err" role="alert">{error}</div>}

      <div className="ai-compose">
        <textarea
          value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown}
          rows={2} maxLength={4000} placeholder="Tulis pertanyaan… (Enter kirim, Shift+Enter baris baru)"
          aria-label="Pesan"
        />
        <button type="button" className="ai-btn primary" onClick={() => void send()} disabled={busy || !draft.trim() || !ready}>
          Kirim
        </button>
      </div>
      <p className="ai-muted ai-foot">Jawaban AI bisa keliru dan bukan nasihat investasi.</p>
    </section>
  );
}