import type { CSSProperties } from "react";

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <span className="logo" style={{ "--logo": `${size}px` } as CSSProperties} aria-hidden>
      <img src={`${import.meta.env.BASE_URL}logo.svg`} alt="" draggable={false} />
    </span>
  );
}