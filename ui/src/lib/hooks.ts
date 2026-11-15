import { useCallback, useEffect, useMemo, useReducer } from "react";
import { useSearchParams } from "react-router-dom";

/** State di URL: bisa di-bookmark, dibagikan, dan tahan refresh. */
export function useParam(key: string, fallback: string) {
  const [sp, setSp] = useSearchParams();
  const set = useCallback(
    (v: string) => setSp((p) => { const n = new URLSearchParams(p); if (v === fallback) n.delete(key); else n.set(key, v); return n; }, { replace: true }),
    [fallback, key, setSp],
  );
  return [sp.get(key) ?? fallback, set] as const;
}

export const store = {
  get: (k: string) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* diabaikan */ } },
};

/** Warna chart dibaca dari CSS variable, otomatis ikut mode terang/gelap. */
export function useColors() {
  const [tick, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    const m = matchMedia("(prefers-color-scheme: dark)");
    m.addEventListener("change", bump);
    return () => m.removeEventListener("change", bump);
  }, []);
  return useMemo(() => {
    const s = getComputedStyle(document.documentElement);
    const v = (n: string) => s.getPropertyValue(n).trim();
    return {
      text: v("--text"), muted: v("--muted"), border: v("--border"), surface: v("--surface"),
      accent: v("--accent"), accent2: v("--accent-2"), up: v("--up"), down: v("--down"), warn: v("--warn"), gate: v("--gate"),
      series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--s${i}`)),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick]);
}
export type Palette = ReturnType<typeof useColors>;