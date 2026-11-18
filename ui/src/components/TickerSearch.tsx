import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as KeyEvt } from "react";
import { useNavigate } from "react-router-dom";
import { useTickers } from "../lib/api";
import { store } from "../lib/hooks";
import type { Ticker } from "../lib/types";
import { Icon } from "./Icon";

const RECENT_KEY = "recent";

function readRecent(): string[] {
  try {
    const v = JSON.parse(store.get(RECENT_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function pushRecent(ticker: string) {
  store.set(RECENT_KEY, JSON.stringify([ticker, ...readRecent().filter((t) => t !== ticker)].slice(0, 5)));
}

function rank(list: Ticker[], query: string): Ticker[] {
  const q = query.trim();
  if (!q) return [];
  const up = q.toUpperCase();
  const low = q.toLowerCase();
  const starts: Ticker[] = [];
  const has: Ticker[] = [];
  const sector: Ticker[] = [];
  for (const t of list) {
    if (t.ticker.startsWith(up)) starts.push(t);
    else if (t.ticker.includes(up)) has.push(t);
    else if (t.sector.toLowerCase().includes(low)) sector.push(t);
  }
  return [...starts, ...has, ...sector].slice(0, 8);
}

export function TickerSearch({ label = "Cari emiten", placeholder = "Kode atau sektor, mis. BBCA", compact = false, globalKey = false, remember = true, onPick }: {
  label?: string; placeholder?: string; compact?: boolean; globalKey?: boolean; remember?: boolean; onPick?: (ticker: string) => void;
}) {
  const nav = useNavigate();
  const q = useTickers();
  const list = useMemo(() => q.data ?? [], [q.data]);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const uid = useId();

  const recents = useMemo(
    () => readRecent().map((t) => list.find((x) => x.ticker === t)).filter((x): x is Ticker => Boolean(x)),
    [list, open],
  );
  const typed = text.trim().length > 0;
  const items = useMemo(() => (typed ? rank(list, text) : recents), [typed, list, text, recents]);

  useEffect(() => setActive(0), [text]);

  useEffect(() => {
    const down = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", down);
    return () => document.removeEventListener("mousedown", down);
  }, []);

  useEffect(() => {
    if (!globalKey) return;
    const h = (e: KeyboardEvent) => {
      const t = e.target;
      if (e.key === "/" && !(t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement)) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
  }, [globalKey]);

  useEffect(() => {
    if (!open) return;
    const t = items[active];
    if (t) document.getElementById(`${uid}-${t.ticker}`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const pick = (t: Ticker) => {
    pushRecent(t.ticker);
    setText("");
    setOpen(false);
    inputRef.current?.blur();
    if (remember) pushRecent(t.ticker);
    else nav(`/emiten/${t.ticker}`);
  };

  const onKeyDown = (e: KeyEvt<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(Math.max(0, items.length - 1), i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      const hit = items[active];
      if (hit) {
        e.preventDefault();
        pick(hit);
      }
    } else if (e.key === "Escape") {
      setOpen(false);
      inputRef.current?.blur();
    }
  };

  return (
    <div className={`tsearch${compact ? " compact" : ""}`} ref={boxRef}>
      <label className="tsearch-field">
        {!compact && <span className="tsearch-label">{label}</span>}
        <span className="tsearch-input">
          <Icon name="search" size={16} />
          <input
            ref={inputRef} role="combobox" aria-expanded={open} aria-controls={`${uid}-list`} aria-autocomplete="list"
            aria-activedescendant={open && items[active] ? `${uid}-${items[active].ticker}` : undefined}
            value={text} placeholder={placeholder} autoComplete="off" spellCheck={false}
            onFocus={() => setOpen(true)} onChange={(e) => { setText(e.target.value); setOpen(true); }} onKeyDown={onKeyDown}
            aria-label={label}
          />
          {globalKey && !text && <kbd>/</kbd>}
        </span>
      </label>
      {open && (
        <ul className="tsearch-pop" role="listbox" id={`${uid}-list`}>
          {!typed && items.length > 0 && <li className="tsearch-head" role="presentation">Terakhir dibuka</li>}
          {items.map((t, i) => (
            <li
              key={t.ticker} id={`${uid}-${t.ticker}`} role="option" aria-selected={i === active} className={i === active ? "on" : ""}
              onMouseEnter={() => setActive(i)} onMouseDown={(e) => { e.preventDefault(); pick(t); }}
            >
              <strong>{t.ticker}</strong><span>{t.sector}</span>
            </li>
          ))}
          {items.length === 0 && (
            <li className="tsearch-empty" role="presentation">{typed ? `Tidak ada emiten untuk “${text.trim()}”.` : "Ketik kode atau sektor emiten."}</li>
          )}
        </ul>
      )}
    </div>
  );
}