import type { MacroRow } from "./types";

/**
 * Nama tampilan indikator makro. Urutan kunci juga menjadi urutan tampil di dalam kelompok.
 * Indikator yang tidak terdaftar memakai nama dari macro_dictionary.
 */
export const NAMES: Record<string, string> = {
  IHSG: "IHSG",
  USDIDR: "USD/IDR",
  DXY: "Indeks Dolar AS",
  VIX: "Indeks Volatilitas (VIX)",
  BRENT_OIL: "Minyak Brent",
  COAL: "Batubara Newcastle",
  COAL_GLOBAL: "Batubara Termal Australia",
  COPPER: "Tembaga",
  GOLD: "Emas",
  NICKEL: "Nikel",
  BAUXITE: "Bauksit (indeks harga impor)",
  ID_RATE: "Suku Bunga Antarbank 3 Bulan",
  ID_CPI_YOY_BPS: "Inflasi Tahunan (BPS)",
  ID_CPI: "Indeks Harga Konsumen",
  ID_GDP: "Pertumbuhan PDB Riil",
  ID_CURRENT_ACCOUNT: "Transaksi Berjalan",
  ID_TRADE_BALANCE: "Neraca Perdagangan Barang",
  ID_RESERVES: "Cadangan Devisa (di luar emas)",
  ID_FOREIGN_DEBT: "Kepemilikan Asing pada SBN",
  US_10Y: "Imbal Hasil Treasury AS 10 Tahun",
  US_CPI: "Indeks Harga Konsumen AS",
};

export const PRIORITY = Object.keys(NAMES);

type Named = Pick<MacroRow, "key" | "name">;

/** Nama dari database biasanya berbentuk "KUNCI - Deskripsi"; buang awalan kuncinya. */
export function fallbackName(r: Named): string {
  const raw = r.name?.trim();
  if (!raw) return r.key;
  const prefix = `${r.key} - `;
  return raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
}

export const macroLabel = (r: Named): string => NAMES[r.key] ?? fallbackName(r);