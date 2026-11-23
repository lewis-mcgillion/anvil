export type TimelineWindow = { start: number; end: number; paddingTop: number; paddingBottom: number };

export function timelineWindow(total: number, scrollTop: number, rowHeight: number,
                               viewportHeight: number, overscan = 8): TimelineWindow {
  if (![total, scrollTop, rowHeight, viewportHeight, overscan].every(Number.isFinite)
      || !Number.isInteger(total) || total < 0 || scrollTop < 0 || rowHeight <= 0
      || viewportHeight < 0 || !Number.isInteger(overscan) || overscan < 0) {
    throw new Error("Invalid timeline viewport");
  }
  const visible = Math.ceil(viewportHeight / rowHeight);
  const size = Math.min(total, visible + 2 * overscan);
  const start = Math.max(0, Math.min(total - size, Math.floor(scrollTop / rowHeight) - overscan));
  const end = start + size;
  return { start, end, paddingTop: start * rowHeight, paddingBottom: (total - end) * rowHeight };
}
