"""Draws the Mesh Testing charts as SVG pictures; publish.py sends them to Home Assistant as MQTT images.

- session(): one test (or the whole campaign) on one time axis: the test scenario, the upstream eero, the backhaul
  band, gateway latency, the running direct-to-gateway share and upstream changes per day, all aligned.
- comparison(): completed tests side by side (direct share, upstream changes per 24 h, gateway RTT, coverage), with
  the historical baseline drawn apart in grey because it isn't a controlled trial.
What the numbers mean is decided by eero-network's collector.meshtest; this only lays them out. Colors are fixed
(the gateway is always blue, each band always the same color) so every picture reads the same way. Two layouts:
"wide" for desktop and "tall" for phones. The pictures follow the viewer's light or dark setting.
"""

import datetime
from html import escape

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
STYLE = """
.bg{fill:#f9f9f7}.t1{fill:#0b0b0b}.t2{fill:#52514e}.t3{fill:#898781}.grid{stroke:#e1e0d9}.axis{stroke:#898781}
.nodata{fill:#e1e0d9}.stab{fill:#e1e0d9;opacity:.7}.line{fill:none;stroke:#0b0b0b;stroke-width:1.5}
.warn{fill:#b26a00}.base{fill:#b9b8b2}.panel{fill:#fcfcfb;stroke:rgba(11,11,11,.12)}
@media (prefers-color-scheme: dark){.bg{fill:#0d0d0d}.t1{fill:#fff}.t2{fill:#c3c2b7}.grid{stroke:#2c2c2a}
.nodata{fill:#2c2c2a}.stab{fill:#2c2c2a}.line{stroke:#fff}.warn{fill:#fab219}.base{fill:#5f5e5a}
.panel{fill:#1a1a19;stroke:rgba(255,255,255,.12)}}
"""
GATEWAY_COLOR = "#2a78d6"
NODE_COLORS = ["#e8833a", "#8e5bd0", "#1aa39a", "#c0504d", "#7a8b2a"]
BAND_COLORS = {"6": "#2f9e44", "5": "#e0a100", "2.4": "#d03b3b"}
SCENARIO_COLORS = {"A": "#6c8ebf", "B": "#c38d3a", "C": "#3f9c74", "D": "#8a6fb0", "E": "#c0504d"}
OTHER_SCENARIO = "#898781"


def node_colors(gateway, nodes):
    """Fixed colors: the gateway blue, the other eeros by name, wired grey."""
    out = {gateway: GATEWAY_COLOR, "wired": "#52514e"}
    for i, n in enumerate(sorted(x for x in nodes if x != gateway)):
        out[n] = NODE_COLORS[i % len(NODE_COLORS)]
    return out


class Svg:
    def __init__(self):
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, cls="t1", size=13, weight=400, anchor="start"):
        self.add(f'<text x="{x:g}" y="{y:g}" class="{cls}" font-size="{size}" font-weight="{weight}" '
                 f'text-anchor="{anchor}">{escape(str(s))}</text>')

    def rect(self, x, y, w, h, fill=None, cls=None, r=0):
        paint = f'fill="{fill}"' if fill else f'class="{cls}"'
        self.add(f'<rect x="{x:g}" y="{y:g}" width="{max(w, 0):g}" height="{h:g}" rx="{r}" {paint}/>')

    def line(self, x1, y1, x2, y2, cls="grid", extra=""):
        self.add(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" class="{cls}" {extra}/>')

    def render(self, w, h, title):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}" '
                f'font-family=\'{FONT}\' role="img"><title>{escape(title)}</title><style>{STYLE}</style>'
                f'<rect width="100%" height="100%" class="bg"/>' + "".join(self.parts) + "</svg>")


def _fmt(epoch, tz, f="%a %b %-d, %H:%M"):
    return datetime.datetime.fromtimestamp(epoch, tz).strftime(f) if epoch else "—"


def _ticks(lo, hi, tz, max_ticks):
    """(time, step seconds) on local hour boundaries, spaced so that at most max_ticks fit."""
    step_h = next((h for h in (1, 3, 6, 12, 24, 48, 168) if (hi - lo) / (h * 3600) <= max_ticks), 336)
    t = datetime.datetime.fromtimestamp(lo, tz).replace(minute=0, second=0, microsecond=0)
    out = []
    while t.timestamp() <= hi:
        on_step = t.hour % step_h == 0 if step_h < 24 else t.hour == 0 and t.toordinal() % (step_h // 24) == 0
        if t.timestamp() >= lo and on_step:
            out.append((t.timestamp(), step_h * 3600))
        t += datetime.timedelta(hours=1)
    return out


def _legend(svg, x, y, items, width, size=12):
    """Swatches with labels, wrapping at `width`. Returns the y below the legend."""
    cx = x
    for label, color in items:
        w = 22 + 7 * len(label)
        if cx + w > x + width:
            cx, y = x, y + 20
        svg.rect(cx, y - 10, 12, 12, fill=color, r=2)
        svg.text(cx + 17, y, label, cls="t2", size=size)
        cx += w + 12
    return y + 22


def session(data, tz, layout="wide"):
    """data: title, subtitle, flags, lo, hi, measure_from, gateway, nodes, sessions [{code, scenario, name, start,
    end}], timeline [{start, end, upstream, band}], rtt [{t, ms}], daily [{day, changes, valid_hours}]."""
    wide = layout == "wide"
    width = 1100 if wide else 440
    left, right = (150, 24) if wide else (16, 16)
    plot_w = width - left - right
    lo, hi = data["lo"], max(data["hi"], data["lo"] + 60)
    colors = node_colors(data["gateway"], data["nodes"])

    def x(t):
        return left + (min(max(t, lo), hi) - lo) / (hi - lo) * plot_w

    svg = Svg()
    y = 30
    svg.text(16, y, data["title"], size=18 if wide else 16, weight=600)
    y += 22
    svg.text(16, y, data["subtitle"], cls="t2", size=13 if wide else 12)
    if data.get("flags"):
        y += 20
        svg.text(16, y, " · ".join(data["flags"]), cls="warn", size=13, weight=600)
    y += 22

    def row_label(text, top, h):
        if wide:
            svg.text(16, top + h / 2 + 5, text, cls="t2", size=13)
        else:
            svg.text(16, top - 6, text, cls="t2", size=12)

    def bar_row(label, items, color_of, top, h=26):
        """items: [(start, end, key, text)]: a timeline row."""
        if not wide:
            top += 16
        row_label(label, top, h)
        svg.rect(left, top, plot_w, h, cls="nodata", r=3)
        merged = []  # adjacent samples in the same state draw as one block
        for a, b, key, text in items:
            if merged and merged[-1][2] == key and a - merged[-1][1] <= 1:
                merged[-1][1] = b
            else:
                merged.append([a, b, key, text])
        for a, b, key, text in merged:
            if key is None:
                continue
            x0, x1 = x(a), x(b)
            svg.rect(x0, top, max(x1 - x0, 0.6), h, fill=color_of(key))
            if text and x1 - x0 > 7 * len(text) + 8:
                svg.text((x0 + x1) / 2, top + h / 2 + 4, text, cls="t1", size=12, anchor="middle")
        return top + h + 14

    # scenario row: each test, its stabilization hatched
    sessions = [s for s in data["sessions"] if (s["end"] or hi) > lo and s["start"] < hi]
    top = y
    if not wide:
        top += 16
    row_label("Test", top, 26)
    svg.rect(left, top, plot_w, 26, cls="nodata", r=3)
    for s in sessions:
        x0, x1 = x(s["start"]), x(s["end"] or hi)
        svg.rect(x0, top, max(x1 - x0, 0.6), 26, fill=SCENARIO_COLORS.get(s.get("scenario"), OTHER_SCENARIO))
        if s.get("measure_from") and s["measure_from"] > s["start"]:
            svg.rect(x0, top, max(x(s["measure_from"]) - x0, 0), 26, cls="stab")
        label = s["code"]
        if x1 - x0 > 7 * len(label) + 8:
            svg.text((x0 + x1) / 2, top + 17, label, cls="t1", size=12, weight=600, anchor="middle")
    y = top + 26 + 14
    tl = data["timeline"]
    y = bar_row("Upstream eero", [(s["start"], s["end"], s["upstream"],
                                   "direct" if s["upstream"] == data["gateway"] else s["upstream"]) for s in tl],
                lambda k: colors.get(k, "#898781"), y)
    y = bar_row("Backhaul band", [(s["start"], s["end"], s["band"], f"{s['band']} GHz" if s["band"] else None)
                                  for s in tl], lambda k: BAND_COLORS.get(k, "#898781"), y)

    def line_chart(label, points, unit, top, h=110, y_min=0, y_max=None):
        if not wide:
            top += 16
        row_label(label, top, 28 if wide else h)  # wide: at the top, clear of the axis values below it
        vals = [v for _, v in points]
        hi_v = y_max if y_max is not None else max([v for v in vals] + [1]) * 1.15
        svg.line(left, top + h, left + plot_w, top + h, "axis")
        for frac in (0.5, 1):
            gy = top + h - frac * h
            svg.line(left, gy, left + plot_w, gy)
            tick = f"{y_min + frac * (hi_v - y_min):.0f} {unit}"
            if wide:  # in the gutter, clear of the line
                svg.text(left - 8, gy + 4, tick, cls="t3", size=11, anchor="end")
            else:
                svg.text(left + plot_w, gy - 4, tick, cls="t3", size=11, anchor="end")
        if not points:
            svg.text(left + plot_w / 2, top + h / 2, "no data", cls="t3", size=12, anchor="middle")
            return top + h + 14
        path, prev_t = [], None
        for t, v in points:
            px, py = x(t), top + h - (min(v, hi_v) - y_min) / (hi_v - y_min) * h
            path.append(("M" if prev_t is None or t - prev_t > 900 else "L") + f"{px:.1f},{py:.1f}")
            prev_t = t
        svg.add(f'<path d="{" ".join(path)}" class="line"/>')
        return top + h + 14

    rtt = [(p["t"], p["ms"]) for p in data["rtt"]]
    y = line_chart("Gateway RTT", rtt, "ms", y)
    # running share of valid time spent directly on the gateway, from the start of measurement
    cum, direct, total = [], 0, 0
    for s in tl:
        if s["upstream"] is None or s["end"] <= data["measure_from"]:
            continue
        d = s["end"] - max(s["start"], data["measure_from"])
        total += d
        direct += d if s["upstream"] == data["gateway"] else 0
        cum.append((s["end"], 100 * direct / total))
    y = line_chart(f"Direct to {data['gateway']}", cum, "%", y, h=80, y_max=100)
    # time axis
    for t, step in _ticks(lo, hi, tz, 10 if wide else 4):
        tx = x(t)
        svg.line(tx, y - 4, tx, y + 2, "axis")
        svg.text(tx, y + 16, _fmt(t, tz, "%a %-d" if step >= 86400 else "%a %H:%M"), cls="t3", size=11,
                 anchor="middle")
    y += 34
    # upstream changes per day
    daily = data.get("daily") or []
    if daily:
        h = 70
        top = y + (16 if not wide else 0)
        row_label("Upstream changes / day", top, h)
        top_v = max([d["changes"] for d in daily] + [1])
        for d in daily:  # each bar spans its local day on the shared time axis
            day = datetime.datetime.strptime(d["day"], "%Y-%m-%d").replace(tzinfo=tz)
            a, b = day.timestamp(), (day + datetime.timedelta(days=1)).timestamp()
            bx, bw = x(a) + 2, max(x(b) - x(a) - 4, 2)
            bh = d["changes"] / top_v * (h - 18)
            svg.rect(bx, top + h - bh, bw, bh, fill="#52514e", r=2)
            svg.text(bx + bw / 2, top + h - bh - 4, d["changes"], cls="t2", size=11, anchor="middle")
            part = "" if d["valid_hours"] >= 23.5 else f" ({d['valid_hours']:.0f} h)"
            if bw > 46:
                svg.text(bx + bw / 2, top + h + 14, day.strftime("%a %-d") + part, cls="t3", size=11, anchor="middle")
        y = top + h + 32
    # legend
    items = [("direct (" + data["gateway"] + ")", GATEWAY_COLOR)]
    items += [(n, colors[n]) for n in sorted(colors) if n not in (data["gateway"], "wired")]
    items += [(f"{b} GHz", c) for b, c in BAND_COLORS.items()] + [("no data", "#e1e0d9")]
    y = _legend(svg, 16, y + 4, items, width - 32)
    svg.text(16, y + 2, "Shaded start of a test = stabilization (not measured). RTT = Home Assistant's ping to the "
             "gateway, 5-minute medians." if wide else "Shaded = stabilization. RTT: HA ping, 5-min medians.",
             cls="t3", size=11)
    return svg.render(width, y + 18, data["title"])


def comparison(rows, baseline, gateway, layout="wide"):
    """rows: completed trials [{code, name, scenario, flags, direct_pct, changes_per_24h, rtt_median_ms, rtt_p95_ms,
    coverage_pct, valid_hours}]; baseline: the same keys or None (drawn grey, apart, labelled historical)."""
    wide = layout == "wide"
    width = 1100 if wide else 440
    panels = [("direct_pct", f"Direct to {gateway} (%)", 100, None),
              ("changes_per_24h", "Upstream changes per 24 h", None, None),
              ("rtt_median_ms", "Gateway RTT median, p95 (ms)", None, "rtt_p95_ms"),
              ("coverage_pct", "Valid data coverage (%)", 100, None)]
    entries = list(rows) + ([{**baseline, "code": "Baseline", "historical": True}] if baseline else [])
    svg = Svg()
    svg.text(16, 30, "Completed tests compared", size=18 if wide else 16, weight=600)
    svg.text(16, 52, "Controlled trials in color; the baseline (grey) is normal operation before testing, "
             "not a comparable trial." if wide else "Trials in color; grey baseline = history, not a trial.",
             cls="t2", size=12)
    if not entries:
        svg.text(16, 90, "No completed tests yet.", cls="t3", size=13)
        return svg.render(width, 120, "Mesh test comparison")
    cols = 2 if wide else 1
    pw = (width - 32 - (cols - 1) * 16) / cols
    row_h = 24
    ph = 44 + row_h * len(entries) + 10
    y0 = 72
    for i, (key, title, fixed_max, extra) in enumerate(panels):
        px, py = 16 + (i % cols) * (pw + 16), y0 + (i // cols) * (ph + 16)
        svg.rect(px, py, pw, ph, cls="panel", r=8)
        svg.text(px + 12, py + 24, title, size=13, weight=600)
        vals = [e.get(k) for e in entries for k in (key, extra) if k and e.get(k) is not None]
        top_v = fixed_max or (max(vals) * 1.15 if vals else 1) or 1
        label_w, value_w = 92, 92
        bar_x, bar_w = px + 12 + label_w, pw - 24 - label_w - value_w
        for j, e in enumerate(entries):
            ry = py + 40 + j * row_h
            if e.get("historical") and j:
                svg.line(px + 12, ry - 4, px + pw - 12, ry - 4)
            label = e["code"] + (" *" if "preliminary" in (e.get("flags") or []) else "")
            svg.text(px + 12, ry + 13, label, cls="t2" if e.get("historical") else "t1", size=12)
            v = e.get(key)
            if v is None:
                svg.text(bar_x, ry + 13, "not available", cls="t3", size=12)
                continue
            fill = None if e.get("historical") else SCENARIO_COLORS.get(e.get("scenario"), OTHER_SCENARIO)
            svg.rect(bar_x, ry + 2, bar_w * min(v, top_v) / top_v, 14, fill=fill, cls="base" if not fill else None,
                     r=3)
            text = f"{v:g}"
            if extra and e.get(extra) is not None:
                ex = bar_x + bar_w * min(e[extra], top_v) / top_v
                svg.line(ex, ry, ex, ry + 18, "axis", 'stroke-width="2"')
                text += f" · {e[extra]:g}"
            svg.text(px + pw - 12, ry + 13, text, cls="t1", size=12, anchor="end")
    rows_n = (len(panels) + cols - 1) // cols
    y = y0 + rows_n * (ph + 16) + 6
    svg.text(16, y, "* preliminary (fewer valid hours than the threshold set when it ran)", cls="t3", size=11)
    return svg.render(width, y + 16, "Mesh test comparison")
