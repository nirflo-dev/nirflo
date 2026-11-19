const INFINITY =
  "M60 64.5C68 54 76 44 88 44C99.5 44 110 53 110 64.5C110 76 99.5 85 88 85C76 85 68 75 60 64.5C52 54 44 44 32 44C20.5 44 10 53 10 64.5C10 76 20.5 85 32 85C44 85 52 75 60 64.5Z";

export function Loader({ label = "Memuat…", fullscreen = false, size = 72 }: {
  label?: string; fullscreen?: boolean; size?: number;
}) {
  return (
    <div className={`loader${fullscreen ? " full" : ""}`} role="status" aria-live="polite">
      <span className="loader-mark" style={{ "--size": `${size}px` } as React.CSSProperties} aria-hidden>
        <svg viewBox="0 26 120 77" focusable="false">
          <path className="loader-track" d={INFINITY} pathLength={1} />
          <path className="loader-run" d={INFINITY} pathLength={1} />
        </svg>
      </span>
      <span className="loader-label">{label}</span>
    </div>
  );
}