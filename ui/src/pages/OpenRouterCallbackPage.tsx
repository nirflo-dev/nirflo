import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { AI_PATH, AI_STATUS_KEY, completeOpenRouterLogin } from "../lib/ai";
import { Loading } from "../components/ui";
import "../ai.css";

export default function OpenRouterCallbackPage() {
  const nav = useNavigate();
  const qc = useQueryClient();

  useEffect(() => {
    let live = true;
    completeOpenRouterLogin().then((result) => {
      if (!live) return; // efek pertama milik StrictMode: hanya efek terakhir yang berpindah halaman
      qc.invalidateQueries({ queryKey: AI_STATUS_KEY });
      nav(AI_PATH, { replace: true, state: { flash: result.ok ? { kind: "ok" } : { kind: "err", message: result.message } } });
    });
    return () => { live = false; };
  }, [nav, qc]);

  return (
    <div className="ai-page">
      <Loading />
      <p className="ai-muted" role="status">Menyelesaikan sambungan ke OpenRouter…</p>
    </div>
  );
}