#!/usr/bin/env python3
"""clocktree — クロック系統図(クロック源 → PLL → 分周/ゲート → ドメイン → CDC)

  python3 clocktree.py diagram.yaml -o out/clock_tree.svg [--png]

diagram.yaml の clocks: を使う
  clocks:
    sources: [{id: xtal, label: XTAL, freq: 25MHz, kind: osc|pad}]     osc=チップ外発振器, pad=外部REFCLK入力
    plls:    [{id: pll_sys, label: SYS PLL, in: xtal, freq: 2.0GHz}]
    domains: [{id: core_clk, label: core, freq: 1.0GHz, from: pll_sys, div: 2, gate: true, color: "#2563eb"}]
  block/bus に clock_domain: core_clk を書くと、ドメインの所属ブロックとCDC箇所が自動で決まる。
CDC: アーキテクチャ図の接続(バス接続・結線)のうち、両端のドメインが異なるものを集計して右側に描く。
"""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, load, esc, wlen, INK, to_png

PAL = ["#2563eb", "#9333ea", "#db2777", "#0891b2", "#ea580c", "#16a34a", "#ca8a04"]


def build(c, title="クロック系統図"):
    ck = c.s.get("clocks", {})
    srcs, plls, doms = ck.get("sources", []), ck.get("plls", []), ck.get("domains", [])
    if not doms:
        return None, ["clocks.domains がない(クロック系統図は描けない)"]
    warn = []
    # ドメイン所属
    members = {d["id"]: [] for d in doms}
    for o in list(c.blocks.values()) + list(c.buses.values()):
        d = o.get("clock_domain")
        if d in members and not o.get("_ext"):
            members[d].append(o.get("label", o["id"]))
        elif d and d not in members:
            warn.append(f"{o['id']}: 未定義のドメイン {d}")
    # CDC(ドメインの組)
    def dom_of(o):
        return o.get("clock_domain") if o else None
    pairs = {}
    for bus in c.buses.values():
        for a in bus.get("attach", []):
            opt = c.attach_opt.get((bus["id"], a), {})
            da, db = dom_of(c.owner_clock(a)), dom_of(bus)
            if opt.get("cdc") is False or not da or not db or da == db:
                continue
            k = tuple(sorted((da, db)))
            pairs.setdefault(k, []).append(f"{a.split('.')[0]}↔{bus.get('label', bus['id'])}")
    for ln in c.s.get("links", []):
        if ln["from"] not in c.anch or ln["to"] not in c.anch or ln.get("cdc") is False:
            continue
        A, B = c.anch[ln["from"]], c.anch[ln["to"]]
        if A[3] is None or B[3] is None:
            continue
        da, db = dom_of(c.owner_clock(ln["from"])), dom_of(c.owner_clock(ln["to"]))
        if da and db and da != db:
            pairs.setdefault(tuple(sorted((da, db))), []).append(f"{A[3]}→{B[3]}")

    # 配置
    X_SRC, X_PLL, X_DOM, W_SRC, W_PLL, W_DOM = 30, 270, 490, 160, 150, 340
    y = 90
    dpos = {}
    def flow(names, width):
        rows, x, cur = [], 0, []
        for m in names:
            w = min(wlen(m) * 6.6 + 20, width)
            if cur and x + w > width:
                rows.append(cur); cur, x = [], 0
            cur.append((m, x, w)); x += w + 8
        if cur:
            rows.append(cur)
        return rows
    chips = {d["id"]: flow(members[d["id"]], 316) for d in doms}
    for d in doms:
        h = 50 + max(1, len(chips[d["id"]])) * 26
        dpos[d["id"]] = (X_DOM, y, W_DOM, h)
        y += h + 22
    bottom = y
    def cy(r):
        return r[1] + r[3] / 2
    ppos = {}
    for p in plls:
        ys = [cy(dpos[d["id"]]) for d in doms if d.get("from") == p["id"]] or [100]
        ppos[p["id"]] = [X_PLL, sum(ys) / len(ys) - 28, W_PLL, 56]
    def spread(pos):
        items = sorted(pos.items(), key=lambda kv: kv[1][1])
        last = 60
        for _, r in items:
            r[1] = max(r[1], last)
            last = r[1] + r[3] + 16
    spread(ppos)
    spos = {}
    for s_ in srcs:
        ys = [ppos[p["id"]][1] + 28 for p in plls if p.get("in") == s_["id"]]
        ys += [cy(dpos[d["id"]]) for d in doms if d.get("from") == s_["id"]]
        ys = ys or [100]
        spos[s_["id"]] = [X_SRC, sum(ys) / len(ys) - 26, W_SRC, 52]
    spread(spos)
    bottom = max([bottom] + [r[1] + r[3] + 20 for r in list(ppos.values()) + list(spos.values())])
    lanes = len(pairs)
    W = X_DOM + W_DOM + 40 + lanes * 34 + 150
    H = bottom + 64
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" font-family="\'Noto Sans JP\',sans-serif">'
         f'<rect width="100%" height="100%" fill="#fff"/>'
         f'<defs><marker id="cka" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
         f'markerUnits="userSpaceOnUse" orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="{INK}"/></marker></defs>',
         f'<text x="12" y="28" font-size="17" font-weight="800" fill="{INK}">{esc(title)}</text>']
    chip_x = X_SRC + W_SRC + 22
    o.append(f'<rect x="{chip_x}" y="50" width="{X_DOM + W_DOM + 20 + lanes * 34 + 30 - chip_x}" height="{H - 94}" rx="6" '
             f'fill="#fcfcfd" stroke="{INK}" stroke-width="2.2"/>'
             f'<text x="{chip_x + 10}" y="70" font-size="13" font-weight="800" fill="{INK}">{esc(c.s.get("chip", {}).get("label", "chip"))}</text>')

    def edge(x1, y1, x2, y2, label=None, mx=None):
        mx = (x1 + x2) / 2 if mx is None else mx
        o.append(f'<path d="M{x1:.1f},{y1:.1f} L{mx:.1f},{y1:.1f} L{mx:.1f},{y2:.1f} L{x2:.1f},{y2:.1f}" fill="none" '
                 f'stroke="{INK}" stroke-width="1.6" marker-end="url(#cka)"/>')
        if label:
            o.append(f'<text x="{x2 - 8:.1f}" y="{y2 - 6:.1f}" font-size="11" font-weight="700" fill="#0369a1" text-anchor="end">{esc(label)}</text>')

    for s_ in srcs:
        x, y_, w, h = spos[s_["id"]]
        dash = ' stroke-dasharray="5 4"' if s_.get("kind", "osc") == "osc" else ""
        o.append(f'<rect x="{x}" y="{y_:.1f}" width="{w}" height="{h}" rx="4" fill="#f8fafc" stroke="#64748b" stroke-width="1.5"{dash}/>'
                 f'<text x="{x + w/2}" y="{y_ + 22:.1f}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">{esc(s_.get("label", s_["id"]))}</text>'
                 f'<text x="{x + w/2}" y="{y_ + 40:.1f}" font-size="11.5" font-weight="700" fill="#0369a1" text-anchor="middle">{esc(s_.get("freq", ""))}</text>')
        if True:   # チップ外の源はパッド経由で入る
            o.append(f'<rect x="{chip_x - 8}" y="{y_ + h/2 - 8:.1f}" width="16" height="16" fill="#cbd5e1" stroke="{INK}" stroke-width="1.5"/>')
    for p in plls:
        x, y_, w, h = ppos[p["id"]]
        o.append(f'<rect x="{x}" y="{y_:.1f}" width="{w}" height="{h}" rx="4" fill="#fff" stroke="{INK}" stroke-width="1.6"/>'
                 f'<text x="{x + w/2}" y="{y_ + 23:.1f}" font-size="13" font-weight="700" fill="{INK}" text-anchor="middle">{esc(p.get("label", p["id"]))}</text>'
                 f'<text x="{x + w/2}" y="{y_ + 41:.1f}" font-size="11.5" font-weight="700" fill="#0369a1" text-anchor="middle">{esc(p.get("freq", ""))}</text>')
        if p.get("in") in spos:
            sx, sy, sw, sh = spos[p["in"]]
            k = [q["id"] for q in srcs].index(p["in"])
            edge(sx + sw, sy + sh / 2, x, y_ + h / 2, mx=X_PLL - 14 - k * 10)
    for i, d in enumerate(doms):
        x, y_, w, h = dpos[d["id"]]
        col = d.get("color", PAL[i % len(PAL)])
        o.append(f'<rect x="{x}" y="{y_}" width="{w}" height="{h}" rx="6" fill="{col}" fill-opacity=".10" stroke="{col}" stroke-width="2"/>'
                 f'<text x="{x + 12}" y="{y_ + 22}" font-size="13.5" font-weight="800" fill="{col}">{esc(d.get("label", d["id"]))}</text>'
                 f'<text x="{x + w - 12}" y="{y_ + 22}" font-size="13" font-weight="800" fill="#0369a1" text-anchor="end">{esc(d.get("freq", ""))}{"(仮)" if d.get("assumed") else ""}</text>')
        for r, row in enumerate(chips[d["id"]]):
            for m, ox, w in row:
                cx_, cy_ = x + 12 + ox, y_ + 34 + r * 26
                o.append(f'<rect x="{cx_:.1f}" y="{cy_}" width="{w:.1f}" height="20" rx="10" fill="#fff" stroke="{col}" stroke-width="1"/>'
                         f'<text x="{cx_ + w/2:.1f}" y="{cy_ + 14}" font-size="11" fill="{INK}" text-anchor="middle">{esc(m)}</text>')
        if not members[d["id"]]:
            o.append(f'<text x="{x + 12}" y="{y_ + 46}" font-size="11" fill="#94a3b8">所属ブロックなし</text>')
        src = d.get("from")
        lab = []
        if d.get("div"):
            lab.append(f"÷{d['div']}")
        if d.get("gate"):
            lab.append("CG")
        if src in ppos:
            px, py, pw, ph = ppos[src]
            edge(px + pw, py + ph / 2, x, y_ + h / 2, " ".join(lab) or None)
        elif src in spos:
            sx, sy, sw, sh = spos[src]
            k = [q["id"] for q in srcs].index(src)
            edge(sx + sw, sy + sh / 2, x, y_ + h / 2, " ".join(lab) or None, mx=X_PLL - 14 - k * 10)
        elif src:
            warn.append(f"{d['id']}: from={src} が未定義")
    # CDC マップ
    xr = X_DOM + W_DOM
    for k, ((da, db), via) in enumerate(sorted(pairs.items(), key=lambda kv: abs(cy(dpos[kv[0][0]]) - cy(dpos[kv[0][1]])))):
        lx = xr + 30 + k * 34
        y1, y2 = cy(dpos[da]), cy(dpos[db])
        o.append(f'<path d="M{xr},{y1:.1f} L{lx},{y1:.1f} L{lx},{y2:.1f} L{xr},{y2:.1f}" fill="none" stroke="#dc2626" '
                 f'stroke-width="1.6" stroke-dasharray="5 3"/>')
        tag = f"CDC×{len(via)}"
        tw = wlen(tag) * 6.4 + 10
        o.append(f'<rect x="{lx - tw/2:.1f}" y="{(y1 + y2)/2 - 9:.1f}" width="{tw:.1f}" height="18" rx="2" fill="#fff" stroke="#dc2626" stroke-width="1.4"/>'
                 f'<text x="{lx:.1f}" y="{(y1 + y2)/2 + 4:.1f}" font-size="10" font-weight="800" fill="#dc2626" text-anchor="middle">{tag}</text>')
    o.append(f'<text x="{chip_x}" y="{H - 16}" font-size="11" fill="#64748b">÷n = 分周 / CG = クロックゲート / '
             f'赤破線 = ドメイン間の非同期乗り換え(CDC)。破線の源 = チップ外</text></svg>')
    table = [(da, db, via) for (da, db), via in sorted(pairs.items())]
    return "".join(o), warn, table


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="clock_tree.svg")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    res = build(c)
    if res[0] is None:
        sys.exit(res[1][0])
    svg, warn, table = res
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(svg)
    if a.png:
        to_png(a.out)
    print("クロック系統図:", a.out)
    print("CDC(ドメインの組 / 経路):")
    for da, db, via in table:
        print(f"  {da} ↔ {db}: {', '.join(via)}")
    if warn:
        print("警告:\n  " + "\n  ".join(warn))
