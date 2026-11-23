export type TimelineWindow = { start: number; end: number; paddingTop: number; paddingBottom: number };

export function timelineWindow(total: number, scrollTop: number, rowHeight: number,
                               viewportHeight: number, overscan = 8): TimelineWindow {
  if (![total, scrollTop, rowHeight, viewportHeight, overscan].every(Number.isFinite)
      || !Number.isInteger(total) || total < 0 || scrollTop < 0 || rowHeight <= 0
      || viewportHeight < 0 || !Number.isInteger(overscan) || overscan < 0) {
    throw new Error("Invalid timeline viewport");
  }
  const visible = Math.ceil(viewportHeight / rowHeight);
  const start = Math.max(0, Math.min(total, Math.floor(scrollTop / rowHeight)) - overscan);
  const end = Math.min(total, start + visible + 2 * overscan);
  return { start, end, paddingTop: start * rowHeight, paddingBottom: (total - end) * rowHeight };
}
