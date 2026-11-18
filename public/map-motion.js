// Short camera transitions for explicit navigation; direct gestures stay immediate.
export class MapMotion {
  constructor(win) { this.win = win; this.frame = null; }
  cancel() {
    if (this.frame !== null) this.win.cancelAnimationFrame(this.frame);
    this.frame = null;
  }
  move(from, to, apply) {
    this.cancel();
    if (!this.win?.requestAnimationFrame || this.win.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      apply({ ...to }); return;
    }
    let start;
    const step = now => {
      start ??= now;
      const progress = Math.min(1, (now - start) / 190), ease = 1 - (1 - progress) ** 3;
      apply(progress === 1 ? { ...to }
        : Object.fromEntries(Object.keys(to).map(key => [key, from[key] + (to[key] - from[key]) * ease])));
      this.frame = progress < 1 ? this.win.requestAnimationFrame(step) : null;
    };
    this.frame = this.win.requestAnimationFrame(step);
  }
}
