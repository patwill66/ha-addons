"""Draws the mesh as an SVG picture: internet, the gateway, then each eero under the eero it connects through,
with clients and channel utilization per band. publish.py sends it to Home Assistant as an MQTT image.

Input is eero-network's report.topology() (it decides the tree and what counts as busy); this only lays it out.
Two layouts: "wide" (a tree, for desktop) and "tall" (a stacked, indented list that stays readable on a phone).
The picture carries its own background and switches to dark colors with the viewer's system setting.
"""

import datetime
from html import escape

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
STYLE = """
.bg{fill:#f9f9f7}.card{fill:#fcfcfb;stroke:rgba(11,11,11,.14)}.off{fill:#f0efec}
.t1{fill:#0b0b0b}.t2{fill:#52514e}.t3{fill:#898781}
.track{fill:#e1e0d9}.meter{fill:#2a78d6}.busy{fill:#fab219}.congested{fill:#d03b3b}
.good{fill:#0ca30c}.bad{fill:#d03b3b}.unk{fill:#898781}
.rule{stroke:#e1e0d9}.wire{stroke:#52514e;fill:none}.pill{fill:#f9f9f7;stroke:rgba(11,11,11,.14)}
@media (prefers-color-scheme: dark){
.bg{fill:#0d0d0d}.card{fill:#1a1a19;stroke:rgba(255,255,255,.14)}.off{fill:#262624}
.t1{fill:#fff}.t2{fill:#c3c2b7}.track{fill:#2c2c2a}.meter{fill:#3987e5}
.rule{stroke:#2c2c2a}.wire{stroke:#c3c2b7}.pill{fill:#0d0d0d;stroke:rgba(255,255,255,.14)}}
"""
BAND_ORDER = {"2.4": 0, "5": 1, "6": 2}
LEVEL_TEXT = {"busy": "busy", "congested": "congested"}


class Svg:
    def __init__(self):
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, cls="t1", size=14, weight=400, anchor="start"):
        self.add(f'<text x="{x:g}" y="{y:g}" class="{cls}" font-size="{size}" font-weight="{weight}" '
                 f'text-anchor="{anchor}">{escape(str(s))}</text>')

    def rect(self, x, y, w, h, cls, r=0, extra=""):
        self.add(f'<rect x="{x:g}" y="{y:g}" width="{max(w, 0):g}" height="{h:g}" rx="{r}" class="{cls}" {extra}/>')

    def render(self, w, h, title):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}" '
                f'font-family=\'{FONT}\' role="img"><title>{escape(title)}</title><style>{STYLE}</style>'
                f'<rect width="100%" height="100%" class="bg"/>' + "".join(self.parts) + "</svg>")


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def link_text(n):
    if n["link"] == "wired":
        return "wired backhaul"
    if n["link"] == "wireless":
        bars = f" · mesh {n['mesh_bars']}/5" if n.get("mesh_bars") is not None else ""
        return f"{n['link_band']} GHz wireless{bars}" if n.get("link_band") else f"wireless{bars}"
    return "upstream unknown"


CARD_HEAD = 62
ROW = 44


def card_height(n):
    return CARD_HEAD + ROW * max(len(n["radios"]), 1) + 8


def draw_card(svg, n, x, y, w):
    """One eero: name, status, clients, then a row per band with channel, clients and a utilization meter."""
    h = card_height(n)
    online = n.get("online")
    svg.rect(x, y, w, h, "card" if online is not False else "card off", r=10)
    dot = "good" if online else "bad" if online is False else "unk"
    svg.add(f'<circle cx="{x + 20:g}" cy="{y + 24:g}" r="6" class="{dot}"/>')
    svg.text(x + 34, y + 30, n.get("location") or "eero", size=18, weight=600)
    role = "gateway" if n.get("is_gateway") else ("online" if online else "offline" if online is False else "")
    svg.text(x + w - 16, y + 29, role, cls="t2" if online is not False else "t1", size=13,
             weight=600 if online is False else 400, anchor="end")
    clients = n.get("clients")
    detail = plural(clients, "client") if clients is not None else "clients unknown"
    if n.get("clients_wired"):
        detail += f" ({n['clients_wired']} wired)"
    svg.text(x + 16, y + 52, detail, cls="t2", size=13)
    if n.get("upstream_changes_24h"):
        k = n["upstream_changes_24h"]
        svg.text(x + w - 16, y + 52, f"{k} switch{'' if k == 1 else 'es'} in 24 h", cls="t2", size=13, anchor="end")
    svg.add(f'<line x1="{x + 16:g}" y1="{y + CARD_HEAD:g}" x2="{x + w - 16:g}" y2="{y + CARD_HEAD:g}" class="rule"/>')
    if not n["radios"]:
        svg.text(x + 16, y + CARD_HEAD + 28, "no radio data", cls="t3", size=13)
    for i, r in enumerate(sorted(n["radios"], key=lambda r: BAND_ORDER.get(r["band"], 9))):
        top = y + CARD_HEAD + ROW * i
        svg.text(x + 16, top + 20, f"{r['band']} GHz", size=14, weight=600)
        ch = f"ch {r['channel']}" if r.get("channel") is not None else "ch —"
        if r.get("width_mhz"):
            ch += f" · {r['width_mhz']} MHz"
        svg.text(x + 82, top + 20, ch, cls="t2", size=13)
        rc = r.get("clients")
        svg.text(x + w - 16, top + 20, plural(rc, "client") if rc is not None else "—", size=13, anchor="end")
        util, level = r.get("utilization"), r.get("level")
        label = f"{util} %" if util is not None else "—"
        if level in LEVEL_TEXT:
            label += f" {LEVEL_TEXT[level]}"
        label_w = 92 if level in LEVEL_TEXT else 44
        mw = w - 32 - label_w
        svg.rect(x + 16, top + 29, mw, 8, "track", r=4)
        if util:
            svg.rect(x + 16, top + 29, mw * min(util, 100) / 100, 8, level if level in LEVEL_TEXT else "meter", r=4)
        svg.text(x + w - 16, top + 37, label, cls="t1" if level in LEVEL_TEXT else "t2", size=13,
                 weight=600 if level in LEVEL_TEXT else 400, anchor="end")
    return h


def draw_link(svg, path, n, lx, ly, anchor="middle"):
    dash = ' stroke-dasharray="6 5"' if n["link"] != "wired" else ""
    svg.add(f'<path d="{path}" class="wire" stroke-width="2"{dash}/>')
    text = link_text(n)
    width = 7.2 * len(text) + 16
    left = lx - width / 2 if anchor == "middle" else lx
    svg.rect(left, ly - 13, width, 22, "pill", r=11)
    svg.text(left + width / 2, ly + 3, text, cls="t2", size=12, anchor="middle")


def internet_width(internet):
    return 60 + 8 * len("Internet " + ("up" if internet else "down" if internet is False else "unknown"))


def draw_internet(svg, cx, y, internet):
    state = "up" if internet else "down" if internet is False else "unknown"
    w = internet_width(internet)
    svg.rect(cx - w / 2, y, w, 34, "card", r=17)
    svg.add(f'<circle cx="{cx - w / 2 + 20:g}" cy="{y + 17:g}" r="6" '
            f'class="{"good" if internet else "bad" if internet is False else "unk"}"/>')
    svg.text(cx - w / 2 + 34, y + 22, f"Internet {state}", size=14, weight=600)


def footer(svg, x, y, as_of, tz, anchor="start"):
    when = datetime.datetime.fromtimestamp(as_of, tz).strftime("%a %b %-d, %H:%M") if as_of else "—"
    svg.text(x, y, f"eero data as of {when} · solid line wired, dashed wireless · utilization is the share of "
                   f"airtime in use", cls="t3", size=12, anchor=anchor)


def wide(topo, internet, as_of, tz):
    """A tree: each eero centred over the eeros that connect through it."""
    card_w, gap, margin, level_gap, top = 320, 36, 24, 84, 96
    children = {}
    for n in topo:
        children.setdefault(n["parent_id"], []).append(n)
    slots, xs = [0], {}

    def place(n):  # leaves take the next slot; parents sit over their children
        kids = children.get(n["id"], [])
        if not kids:
            xs[n["id"]] = margin + slots[0] * (card_w + gap) + card_w / 2
            slots[0] += 1
        else:
            for k in kids:
                place(k)
            xs[n["id"]] = (xs[kids[0]["id"]] + xs[kids[-1]["id"]]) / 2

    roots = children.get(None, [])
    for r in roots:
        place(r)
    depth_h = {}
    for n in topo:
        depth_h[n["depth"]] = max(depth_h.get(n["depth"], 0), card_height(n))
    ys = {0: top}
    for d in range(1, max(depth_h, default=0) + 1):
        ys[d] = ys[d - 1] + depth_h[d - 1] + level_gap
    width = margin * 2 + max(slots[0], 1) * (card_w + gap) - gap
    if width < 760:  # room for the footer: centre a small mesh
        xs = {k: v + (760 - width) / 2 for k, v in xs.items()}
        width = 760
    height = ys[max(depth_h, default=0)] + depth_h.get(max(depth_h, default=0), 0) + 52
    svg = Svg()
    cx = xs[roots[0]["id"]] if roots else width / 2
    draw_internet(svg, cx, 20, internet)
    if roots:
        svg.add(f'<line x1="{cx:g}" y1="54" x2="{cx:g}" y2="{top:g}" class="wire" stroke-width="2"/>')
    bottoms = {}
    for n in topo:
        y = ys[n["depth"]]
        bottoms[n["id"]] = y + card_height(n)
        if n["parent_id"] is not None:
            px, py, cx = xs[n["parent_id"]], bottoms[n["parent_id"]], xs[n["id"]]
            mid = py + 26
            draw_link(svg, f"M{px:g},{py:g} V{mid:g} H{cx:g} V{y:g}", n, cx, y - 22)
        draw_card(svg, n, xs[n["id"]] - card_w / 2, y, card_w)
    footer(svg, width / 2, height - 18, as_of, tz, anchor="middle")
    return svg.render(width, height, "eero mesh map")


def tall(topo, internet, as_of, tz):
    """A stacked list, each eero indented under the eero it connects through: readable at phone width."""
    width, margin, indent, gap = 440, 16, 22, 48
    svg = Svg()
    cx = margin + internet_width(internet) / 2
    draw_internet(svg, cx, 14, internet)
    y = 14 + 34 + gap
    pos = {}
    first = True
    for n in topo:
        x = margin + indent * n["depth"]
        if n["parent_id"] is not None and n["parent_id"] in pos:
            px, ptop = pos[n["parent_id"]]
            lx = px + indent / 2
            draw_link(svg, f"M{lx:g},{ptop + card_height_by_id(topo, n['parent_id']):g} V{y + 30:g} H{x:g}",
                      n, lx + 8, y - gap / 2, anchor="start")
        elif first:
            svg.add(f'<line x1="{cx:g}" y1="48" x2="{cx:g}" y2="{y:g}" class="wire" stroke-width="2"/>')
        first = False
        pos[n["id"]] = (x, y)
        y += draw_card(svg, n, x, y, width - margin - x) + gap
    height = y - gap + 40
    when = datetime.datetime.fromtimestamp(as_of, tz).strftime("%a %b %-d, %H:%M") if as_of else "—"
    svg.text(margin, height - 16, f"eero data as of {when}", cls="t3", size=12)
    return svg.render(width, height, "eero mesh map")


def card_height_by_id(topo, nid):
    return card_height(next(n for n in topo if n["id"] == nid))


def render(topo, internet, as_of, tz, layout="wide"):
    return (wide if layout == "wide" else tall)(topo, internet, as_of, tz)
