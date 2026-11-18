import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { KeyboardEvent as KeyEvt, PointerEvent as PtrEvt } from "react";
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

export function useSplit(key: string, fallback = 0.5, min = 0.3, max = 0.7) {
  const ref = useRef<HTMLDivElement>(null);
  const [p, setP] = useState(() => {
    const n = Number(store.get(key));
    return n >= min && n <= max ? n : fallback;
  });
  const latest = useRef(p);
  const commit = (v: number, persist: boolean) => {
    const c = Math.min(max, Math.max(min, v));
    latest.current = c;
    setP(c);
    if (persist) store.set(key, String(c));
  };
  const handle = {
    role: "separator" as const,
    "aria-orientation": "vertical" as const,
    "aria-valuenow": Math.round(p * 100),
    "aria-label": "Ubah lebar panel",
    tabIndex: 0,
    onPointerDown: (e: PtrEvt<HTMLDivElement>) => {
      e.preventDefault();
      const el = e.currentTarget;
      el.setPointerCapture(e.pointerId);
      const move = (ev: PointerEvent) => {
        const r = ref.current?.getBoundingClientRect();
        if (r) commit((ev.clientX - r.left) / r.width, false);
      };
      const up = () => {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        el.removeEventListener("pointercancel", up);
        commit(latest.current, true);
      };
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    },
    onKeyDown: (e: KeyEvt<HTMLDivElement>) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      commit(latest.current + (e.key === "ArrowLeft" ? -0.02 : 0.02), true);
    },
    onDoubleClick: () => commit(fallback, true),
  };
  return { p, ref, handle };
}