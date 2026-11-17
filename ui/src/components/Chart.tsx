import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { BarChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import { AxisPointerComponent, DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { num } from "../lib/format";
import type { Palette } from "../lib/hooks";

echarts.use([BarChart, CustomChart, LineChart, ScatterChart, AxisPointerComponent, DataZoomComponent, GridComponent,
  LegendComponent, MarkAreaComponent, MarkLineComponent, TooltipComponent, CanvasRenderer]);

export function Chart({ option, height = 380, label }: { option: EChartsCoreOption; height?: number; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    const el = ref.current!;
    const c = echarts.init(el);
    chart.current = c;
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el);
    return () => { ro.disconnect(); c.dispose(); };
  }, []);
  useEffect(() => { chart.current?.setOption(option, { notMerge: true, lazyUpdate: true }); }, [option]);
  return <div ref={ref} role="img" aria-label={label} style={{ height }} />;
}

export const baseOption = (c: Palette, extra: EChartsCoreOption = {}): EChartsCoreOption => ({
  animation: false,
  color: c.series,
  textStyle: { color: c.muted },
  legend: { top: 0, icon: "roundRect", textStyle: { color: c.muted } },
  tooltip: {
    trigger: "axis", confine: true, backgroundColor: c.surface, borderColor: c.border, textStyle: { color: c.text },
    valueFormatter: (v: unknown) => (typeof v === "number" ? num(v, 2) : String(v ?? "-")),
  },
  ...extra,
});

export const X = (c: Palette) => ({ type: "time" as const, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.muted }, splitLine: { show: false } });
export const Y = (c: Palette, e: object = {}) => ({ type: "value" as const, scale: true, axisLabel: { color: c.muted }, splitLine: { lineStyle: { color: c.border } }, ...e });
export const yName = (name: string, gap = 46) => ({ name, nameLocation: "middle" as const, nameGap: gap });

export const pts = (x: string[], y: (number | null)[]) => x.map((d, i) => [d, y[i]]);
export const line = (name: string, x: string[], y: (number | null)[], color: string, extra: object = {}) => ({
  type: "line", name, showSymbol: false, data: pts(x, y), lineStyle: { width: 1.6, color }, itemStyle: { color }, ...extra,
});

export function stack(c: Palette, H: number, weights: number[], names: string[]) {
  const sum = weights.reduce((a, b) => a + b, 0);
  const usable = H - 34 - 26 - 14 * (weights.length - 1);
  let top = 34;
  const grid = weights.map((w) => { const h = (usable * w) / sum; const g = { left: 64, right: 16, top, height: h }; top += h + 14; return g; });
  return {
    grid,
    xAxis: weights.map((_, i) => ({ ...X(c), gridIndex: i, axisLabel: { show: i === weights.length - 1, color: c.muted } })),
    yAxis: weights.map((_, i): Record<string, unknown> => ({ ...Y(c), ...yName(names[i]), gridIndex: i, nameTextStyle: { color: c.muted } })),
    dataZoom: [{ type: "inside", xAxisIndex: weights.map((_, i) => i) }],
    axisPointer: { link: [{ xAxisIndex: "all" }] },
  };
}