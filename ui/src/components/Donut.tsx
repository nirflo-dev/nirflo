import { useMounted } from "../lib/motion";

export type Slice = { key: string; value: number; color: string };

const R = 46;
const C = 2 * Math.PI * R;

export function Donut({ slices, hover, onHover, label }: {
  slices: Slice[]; hover: string | null; onHover: (key: string | null) => void; label: string;
}) {
  const on = useMounted();
  const total = slices.reduce((s, x) => s + Math.max(0, x.value), 0) || 1;
  let acc = 0;
  return (
    <svg className="donut" viewBox="0 0 120 120" role="img" aria-label={label}>
      <circle className="donut-track" cx="60" cy="60" r={R} />
      {slices.map((s) => {
        const frac = Math.max(0, s.value) / total;
        const len = Math.max(0, frac * C - (slices.length > 1 ? 1.5 : 0));
        const off = acc * C;
        acc += frac;
        if (frac <= 0) return null;
        return (
          <circle
            key={s.key} cx="60" cy="60" r={R}
            className={`donut-seg${hover === s.key ? " hot" : hover ? " dim" : ""}`}
            style={{ stroke: s.color, strokeDasharray: on ? `${len} ${C - len}` : `0 ${C}`, strokeDashoffset: -off }}
            onMouseEnter={() => onHover(s.key)} onMouseLeave={() => onHover(null)}
          />
        );
      })}
    </svg>
  );
}