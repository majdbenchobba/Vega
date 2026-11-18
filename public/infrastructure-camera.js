// Camera math is independent of the DOM, so pan, pinch and fit share one model.
import { MapMotion } from './map-motion.js';
export const MIN_ZOOM = .05, MAX_ZOOM = 2;
export const clampZoom = value => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, value));
export function zoomAt(camera, scale, point, nextPoint = point) {
  const z = clampZoom(scale), ratio = z / camera.z;
  return { z, x: nextPoint.x - (point.x - camera.x) * ratio, y: nextPoint.y - (point.y - camera.y) * ratio };
}
export function fitCamera(size, viewport, all = true) {
  const z = clampZoom(Math.min(1, all === 'height' ? 1 : (viewport.width - 40) / size.width,
    all ? (viewport.height - 40) / size.height : 1));
  return { z, x: (viewport.width - size.width * z) / 2,
    y: all ? (viewport.height - size.height * z) / 2 : 20 };
}
export function mapLayout(nodes, orientation = 'horizontal') {
  const columns = ['project', 'runtime', 'service', 'storage'], positions = new Map(), sections = [];
  const horizontal = orientation === 'horizontal', cardWidth = 288, cardHeight = 128;
  const rowWidth = Math.max(1, ...columns.map(kind => nodes.filter(n => n.kind === kind).length)) * 392 - 104;
  columns.forEach((kind, column) => {
    const items = nodes.filter(n => n.kind === kind);
    const width = horizontal ? Math.max(1, items.length) * 392 - 104 : cardWidth;
    const x = horizontal ? (rowWidth - width) / 2 : column * 416, y = horizontal ? column * 304 : 0;
    sections.push({ kind, x, y, width,
      height: horizontal ? 204 : 56 + items.length * 176 });
    items.forEach((node, index) => positions.set(node.id, horizontal
      ? { x: x + index * 392, y: y + 56 }
      : { x, y: 56 + index * 176 }));
  });
  return { positions, sections, orientation, width: horizontal ? rowWidth : 1536,
    height: horizontal ? 1116 : 56 + Math.max(1, ...columns.map(kind =>
      nodes.filter(n => n.kind === kind).length)) * 176, cardWidth, cardHeight };
}

export function horizontalEdgePoints(a, b, width = 288, height = 128, lane = 0, positions = []) {
  const down = b.y >= a.y, direction = down ? 1 : -1, offset = 32 + lane % 3 * 7;
  const start = { x: a.x + width / 2, y: a.y + (down ? height : 0) };
  const end = { x: b.x + width / 2, y: b.y + (down && b.y !== a.y ? 0 : height) };
  const exitY = start.y + direction * offset;
  if (a.y === b.y) return [start, { x: start.x, y: exitY }, { x: end.x, y: exitY }, end];
  const entryY = end.y - direction * offset, cards = [...positions], margin = 22 + lane % 3 * 6;
  const rows = [...new Set(cards.map(p => p.y))].filter(y => y > Math.min(a.y, b.y) && y < Math.max(a.y, b.y))
    .sort((x, y) => direction * (x - y));
  const path = [start, { x: start.x, y: exitY }];
  let x = start.x, y = exitY;
  // Centering shifts odd/even rows by half a column. Pick a clear gap in each
  // crossed row instead of assuming one vertical gutter fits every row.
  for (const rowY of rows) {
    const row = cards.filter(p => p.y === rowY);
    const candidates = [end.x, ...row.flatMap(p => [p.x - margin, p.x + width + margin])];
    const clear = candidates.filter(candidate => row.every(p => candidate <= p.x - margin || candidate >= p.x + width + margin));
    const gap = clear.sort((left, right) => Math.abs(left - end.x) - Math.abs(right - end.x))[0];
    path.push({ x: gap, y });
    x = gap; y = rowY + (down ? height + offset : -offset);
    path.push({ x, y });
  }
  path.push({ x, y: entryY }, { x: end.x, y: entryY }, end);
  return path;
}

export class MapCamera {
  constructor(viewport, canvas, size, { saved, onChange, onFit } = {}) {
    this.viewport = viewport; this.canvas = canvas; this.size = size;
    this.onChange = onChange; this.onFit = onFit; this.pointers = new Map(); this.listeners = [];
    this.motion = new MapMotion(viewport.ownerDocument.defaultView);
    this.camera = saved || (size.orientation === 'horizontal'
      ? { x: (viewport.clientWidth - size.width * .85) / 2, y: 20, z: .85 }
      : fitCamera(size, this.dimensions(), false));
    // Start narrow screens with readable cards; Fit all remains an explicit overview.
    if (!saved && viewport.clientWidth < 600) this.camera = {
      x: (viewport.clientWidth - size.width * .85) / 2, y: 20, z: .85,
    };
    this.listen('wheel', e => {
      // Ordinary wheel scrolling belongs to the page. Ctrl/pinch zooms the map.
      if (!e.ctrlKey && !e.metaKey) {
        if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
          e.preventDefault(); this.motion.cancel();
          this.camera.x -= (e.shiftKey ? e.deltaY : e.deltaX) * (e.deltaMode === 1 ? 16 : 1); this.apply();
        }
        return;
      }
      e.preventDefault();
      this.zoom(this.camera.z * Math.exp(-e.deltaY * (e.deltaMode === 1 ? .04 : .002)), this.point(e));
    }, { passive: false });
    this.listen('pointerdown', e => this.start(e));
    this.listen('pointermove', e => this.move(e));
    this.listen('pointerup', e => this.end(e));
    this.listen('pointercancel', e => this.end(e));
    this.listen('lostpointercapture', e => this.end(e));
    this.listen('click', e => {
      if (this.suppressClick && e.detail !== 0) { e.preventDefault(); e.stopImmediatePropagation(); }
      this.suppressClick = false;
    }, true);
    this.listen('keydown', e => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (['+', '=', '-', '0', 'Home', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
        e.preventDefault();
        this.motion.cancel();
        if (e.key === 'Home' || e.key === '0') { this.fit(true); this.onFit?.(); }
        else if (['+', '=', '-'].includes(e.key)) this.zoom(this.camera.z * (e.key === '-' ? 1 / 1.2 : 1.2));
        else {
          const step = e.shiftKey ? 180 : 70;
          this.camera.x += e.key === 'ArrowLeft' ? step : e.key === 'ArrowRight' ? -step : 0;
          this.camera.y += e.key === 'ArrowUp' ? step : e.key === 'ArrowDown' ? -step : 0;
          this.apply();
        }
      }
    });
    this.listen('focusin', e => {
      const card = e.target.closest?.('.infra-node'); if (!card) return;
      this.motion.cancel();
      // Keyboard traversal must bring the focused card into the visible camera.
      this.reveal(card);
      viewport.scrollLeft = viewport.scrollTop = 0;
      this.apply();
    });
    this.lastSize = this.dimensions();
    const Resize = viewport.ownerDocument.defaultView.ResizeObserver;
    this.observer = new Resize(() => {
      const next = this.dimensions();
      if (next.width && next.height && this.lastSize.width && this.lastSize.height) {
        this.motion.cancel();
        this.camera.x += (next.width - this.lastSize.width) / 2;
        this.camera.y += (next.height - this.lastSize.height) / 2;
        const selected = canvas.querySelector('.infra-node.selected');
        if (selected) this.reveal(selected);
        this.apply();
      }
      if (next.width && next.height) this.lastSize = next;
    });
    this.observer.observe(viewport); this.apply();
  }
  dimensions() { return { width: this.viewport.clientWidth, height: this.viewport.clientHeight }; }
  point(e) { const b = this.viewport.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top }; }
  listen(type, fn, options) { this.viewport.addEventListener(type, fn, options); this.listeners.push([type, fn, options]); }
  snapshot() { return { ...this.camera }; }
  reveal(card) {
    const { width, height } = this.dimensions(), c = this.camera;
    const left = parseFloat(card.style.left) * c.z + c.x, top = parseFloat(card.style.top) * c.z + c.y;
    c.x += left < 16 ? 16 - left : Math.min(0, width - 16 - left - card.offsetWidth * c.z);
    c.y += top < 16 ? 16 - top : Math.min(0, height - 16 - top - card.offsetHeight * c.z);
  }
  apply() {
    const { x, y, z } = this.camera;
    this.canvas.style.transform = `translate(${x}px, ${y}px) scale(${z})`;
    this.viewport.style.backgroundPosition = `${x}px ${y}px`;
    const grid = Math.max(16, 24 * z);
    this.viewport.style.backgroundSize = `${grid}px ${grid}px`;
    this.onChange?.(this.snapshot());
  }
  zoom(scale, point) {
    this.motion.cancel();
    const size = this.dimensions();
    this.camera = zoomAt(this.camera, scale, point || { x: size.width / 2, y: size.height / 2 }); this.apply();
  }
  moveTo(next, animated = true) {
    if (animated) this.motion.move(this.snapshot(), next, frame => { this.camera = frame; this.apply(); });
    else { this.motion.cancel(); this.camera = next; this.apply(); }
  }
  zoomButton(scale) {
    const size = this.dimensions();
    this.moveTo(zoomAt(this.camera, scale, { x: size.width / 2, y: size.height / 2 }));
  }
  fit(all = true) { this.moveTo(fitCamera(this.size, this.dimensions(), all)); }
  focusSection(section) {
    const viewport = this.dimensions();
    const z = Math.max(.65, Math.min(1, (viewport.width - 40) / section.width, (viewport.height - 40) / section.height));
    this.moveTo({ z, x: (viewport.width - section.width * z) / 2 - section.x * z, y: 20 - section.y * z });
  }
  start(e) {
    if (e.button !== 0 && e.button !== 1) return;
    this.motion.cancel();
    this.suppressClick = false; this.pointers.set(e.pointerId, this.point(e));
    // Capture on the original target so a stationary click still opens its card.
    e.target.setPointerCapture?.(e.pointerId);
    this.rebase();
    if (!e.target.closest?.('button')) this.viewport.focus({ preventScroll: true });
  }
  rebase() {
    const points = [...this.pointers.values()];
    this.gesture = points.length ? { camera: this.snapshot(), points } : null;
  }
  move(e) {
    if (!this.pointers.has(e.pointerId)) return;
    this.pointers.set(e.pointerId, this.point(e));
    const now = [...this.pointers.values()], from = this.gesture.points;
    const center = pts => pts.length > 1 ? { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 } : pts[0];
    const a = center(from), b = center(now);
    if (now.length === 1 && !this.suppressClick && Math.hypot(b.x - a.x, b.y - a.y) < 5) return;
    this.suppressClick = true; this.viewport.classList.add('is-panning');
    if (now.length > 1 && from.length > 1) {
      const distance = pts => Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
      this.camera = zoomAt(this.gesture.camera, this.gesture.camera.z * distance(now) / Math.max(1, distance(from)), a, b);
    } else this.camera = { ...this.gesture.camera, x: this.gesture.camera.x + b.x - a.x, y: this.gesture.camera.y + b.y - a.y };
    this.apply();
  }
  end(e) {
    this.pointers.delete(e.pointerId); this.rebase();
    if (!this.pointers.size) this.viewport.classList.remove('is-panning');
  }
  destroy() {
    this.motion.cancel();
    this.observer.disconnect();
    for (const args of this.listeners) this.viewport.removeEventListener(...args);
    this.pointers.clear();
  }
}
