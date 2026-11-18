import { store } from "./hooks";

export function loadRecent(key: string): string[] {
  try {
    const v = JSON.parse(store.get(key) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function addRecent(key: string, value: string, max = 5): string[] {
  const next = [value, ...loadRecent(key).filter((x) => x !== value)].slice(0, max);
  store.set(key, JSON.stringify(next));
  return next;
}

export function clearRecent(key: string) {
  store.set(key, "[]");
}