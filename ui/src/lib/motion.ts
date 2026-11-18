import { useEffect, useState } from "react";

const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

export function useMounted() {
  const [on, setOn] = useState(false);
  useEffect(() => {
    if (reduced()) {
      setOn(true);
      return;
    }
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setOn(true)));
    return () => cancelAnimationFrame(id);
  }, []);
  return on;
}

export function useCountUp(target: number | null | undefined, ms = 700) {
  const [v, setV] = useState(0);
  useEffect(() => {
    if (target == null || !Number.isFinite(target)) return;
    if (reduced()) {
      setV(target);
      return;
    }
    const t0 = performance.now();
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      setV(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return target == null || !Number.isFinite(target) ? null : v;
}