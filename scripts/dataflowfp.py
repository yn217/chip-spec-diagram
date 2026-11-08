#!/usr/bin/env python3
"""dataflowfp — データフロー検討用のフロアプラン風の図

ダイのどの辺のどこからデータが入り、PHY → コントローラ → バス → どの SRAM・演算器に届くかを、
フロアプランに近い配置の上に太い矢印で描く。経路ごとに配線距離(マンハッタン距離の目安)・通過要素・帯域を表にする。

  python3 dataflowfp.py diagram.yaml -o out/ [--png]     # out/dataflow_fp.svg と out/dataflow.md

diagram.yaml(バス構成図と同じ)に dataflow_floorplan: を足す。何も書かなくても、バス構成図の配置をダイに写し、
端子(pads)の辺に PHY を自動で置いて描ける。
  dataflow_floorplan:
    title: データフロー検討(案A)
    die: [10, 7]                     # mm(概略)。省略時はバス構成図の縦横比から
    blocks:                          # 位置を変える・足す(mm、左下原点、[x, y, w, h])。省略時はバス構成図から
      npu: {at: [5.5, 1.0, 3.5, 3.0]}
      pll: {at: [0.3, 0.3, 0.6, 0.6], label: PLL}
    phy:                             # PHY の名前・大きさ(省略時は「<コントローラ> PHY」、辺に沿って置く)
      pcie: {label: PCIe PHY, length: 1.8}
    max_mm: 8                        # これより長い経路は [距離] で知らせる
    scenario: true                   # scenario.steps の flows(経路)も描く
    flows:
      - {label: 重み書き込み, from: host, to: dram, bw: 8GB/s, color: "#2563eb"}
      - {label: DMA 読み出し, from: dram, to: lbuf, bw: 25.6GB/s}
      - {label: 任意の経路, path: [host, pad_pcie, pcie, axi, sram]}   # 経路を直接書く(ブロック・端子・バス)
  from / to だけなら、結線とバスのつながりから経路を探す(途中に通れるのは端子・PHY 付きのコントローラ・
  2 本以上のバスにつながるブリッジ・親ブロックだけ)。

チェック(lint): [到達](経路が見つからない)[帯域](流す帯域が通る要素の帯域を超える)[距離](max_mm を超える)
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, load, esc, wlen, INK, to_png, hz, fmt_bw, bw_text

GRAY = "#64748b"
PAL = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#db2777", "#0891b2", "#ca8a04"]


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def bps(v):
    """'8GB/s' → bytes/s"""
    if v is None:
        return None
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?)B/s", str(v))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]


class DataflowFP:
    def __init__(self, c):
        self.c = c
        self.d = c.s.get("dataflow_floorplan", {}) or {}
        self.warn, self.info = [], []
        self.pads = {p["id"]: p for p in c.s.get("pads", [])}
        self.place()
        self.flows = self.resolve_flows()

    # ---------- 配置 ----------
    def place(self):
        c, d = self.c, self.d
        cx, cy, cw, ch = c.s["chip"]["rect"]
        W, H = d.get("die") or [round(cw / 100, 1), round(ch / 100, 1)]
        self.W, self.H = float(W), float(H)
        ring = min(self.W, self.H) * 0.07                 # PHY を置く外周の幅
        self.ring = ring
        gap = 0.25
        core = (ring + gap, ring + gap, self.W - 2 * (ring + gap), self.H - 2 * (ring + gap))
        self.R = {}
        for bid, b in c.blocks.items():
            if b.get("_ext") or b.get("virtual") or "rect" not in b or b.get("_hidden"):
                continue
            x, y, w, h = b["rect"]
            self.R[bid] = [core[0] + (x - cx) / cw * core[2], core[1] + (y - cy) / ch * core[3], w / cw * core[2], h / ch * core[3]]
        self.label = {bid: b.get("label", bid) for bid, b in c.blocks.items()}
        self.kind = {bid: b.get("kind") for bid, b in c.blocks.items()}
        # 上書き・追加(mm、左下原点)
        for bid, o in (d.get("blocks") or {}).items():
            if o.get("at"):
                x, y, w, h = map(float, o["at"])
                self.R[bid] = [x, self.H - y - h, w, h]
            if o.get("label"):
                self.label[bid] = o["label"]
            if o.get("kind"):
                self.kind[bid] = o["kind"]
        # バスの帯(バス構成図の帯をダイに写す。path で上書き)
        self.bus_y = {}
        for bid, bus in c.buses.items():
            x, y, w, h = bus["rect"]
            self.bus_y[bid] = core[1] + (y + h / 2 - cy) / ch * core[3]
            o = (d.get("buses") or {}).get(bid, {})
            if o.get("y") is not None:
                self.bus_y[bid] = self.H - float(o["y"])
        # バスの帯はブロックの上を通らないよう、つながるブロックの間の空いた高さに移す(バス構成図の高さに近い所)
        for bid, bus in c.buses.items():
            if (d.get("buses") or {}).get(bid, {}).get("y") is not None:
                continue
            att = {a.split(".")[0] for a in bus.get("attach", [])} & set(self.R)
            if not att:
                continue
            xs = [self.R[a][0] + self.R[a][2] / 2 for a in att]
            x0, x1 = min(xs), max(xs)
            tops = [r for b_, r in self.R.items() if not c.blocks.get(b_, {}).get("_parent")
                    and r[0] < x1 and r[0] + r[2] > x0]
            def clear(y):
                return all(not (r[1] - 0.15 < y < r[1] + r[3] + 0.15) for r in tops)
            cands = [self.bus_y[bid]] + [r[1] - 0.3 for r in tops] + [r[1] + r[3] + 0.3 for r in tops]
            ok = [y for y in cands if clear(y) and core[1] < y < core[1] + core[3]]
            if ok:
                self.bus_y[bid] = min(ok, key=lambda y: abs(y - self.bus_y[bid]))
        # PHY: 端子(pad)につながるブロックごとに、端子の辺に沿って置く
        self.phy = {}                                     # ctrl -> (rect, pad, side)
        self.padpos = {}
        for pid, p in self.pads.items():
            ctrl = self.pad_block(pid)
            side = p.get("side", "W")
            if ctrl is None or ctrl not in self.R:
                continue
            o = (d.get("phy") or {}).get(ctrl, {})
            L = float(o.get("length", min(2.0, max(1.2, (self.R[ctrl][3] if side in "WE" else self.R[ctrl][2])))))
            r = self.R[ctrl]
            if side in "WE":
                cyy = r[1] + r[3] / 2
                y0 = min(max(cyy - L / 2, 0.1), self.H - L - 0.1)
                rect = [0.06 if side == "W" else self.W - ring + 0.04, y0, ring - 0.1, L]
                self.padpos[pid] = (0 if side == "W" else self.W, y0 + L / 2)
            else:
                cxx = r[0] + r[2] / 2
                x0 = min(max(cxx - L / 2, 0.1), self.W - L - 0.1)
                rect = [x0, 0.06 if side == "N" else self.H - ring + 0.04, L, ring - 0.1]
                self.padpos[pid] = (x0 + L / 2, 0 if side == "N" else self.H)
            if o.get("at"):
                x, y, w, h = map(float, o["at"])
                rect = [x, self.H - y - h, w, h]
            self.phy[ctrl] = (rect, pid, side, o.get("label") or f"{self.label.get(ctrl, ctrl)} PHY")
            if not (rect[0] < 0.3 or rect[1] < 0.3 or rect[0] + rect[2] > self.W - 0.3 or rect[1] + rect[3] > self.H - 0.3):
                self.warn.append(f"[辺] {self.phy[ctrl][3]}: ダイの辺に接していない(PHY は端子のある辺に沿わせる)")
        # チップ外のブロック(Host・DRAM など): つながる端子の外側
        self.extpos = {}
        for ln in c.s.get("links", []):
            for a, b_ in ((ln["from"], ln["to"]), (ln["to"], ln["from"])):
                ea = a.split(".")[0]
                if c.blocks.get(ea, {}).get("_ext") and b_ in self.pads:
                    self.extpos[ea] = b_

    def pad_block(self, pid):
        for ln in self.c.s.get("links", []):
            for a, b in ((ln["from"], ln["to"]), (ln["to"], ln["from"])):
                if a == pid and b.split(".")[0] in self.c.blocks and not self.c.blocks[b.split(".")[0]].get("_ext"):
                    return b.split(".")[0]
        return None

    # ---------- 経路 ----------
    def graph(self):
        c = self.c
        G = collections.defaultdict(set)
        def node(a):
            if a in c.buses:
                return "u:" + a
            if a in self.pads:
                return "p:" + a
            return "b:" + a.split(".")[0]
        for ln in c.s.get("links", []):
            x, y = node(ln["from"]), node(ln["to"])
            if x != y:
                G[x].add(y); G[y].add(x)
        for bid, bus in c.buses.items():
            for a in bus.get("attach", []):
                G["u:" + bid].add("b:" + a.split(".")[0]); G["b:" + a.split(".")[0]].add("u:" + bid)
        for bid, b in c.blocks.items():                  # 親 ↔ 子
            if b.get("_parent"):
                G["b:" + bid].add("b:" + b["_parent"]); G["b:" + b["_parent"]].add("b:" + bid)
        return G

    def passable(self, n, src, dst):
        if n in (src, dst) or n[:2] in ("u:", "p:"):
            return True
        bid = n[2:]
        b = self.c.blocks.get(bid, {})
        if b.get("virtual") or bid in self.phy:
            return True
        nb = sum(1 for bus in self.c.buses.values() if any(a.split(".")[0] == bid for a in bus.get("attach", [])))
        if nb >= 2:
            return True
        # 行き先(元)の親
        for end in (src, dst):
            e = self.c.blocks.get(end[2:], {})
            if e.get("_parent") == bid:
                return True
        return False

    def find(self, frm, to):
        G = self.graph()
        def nid(x):
            if x in self.c.buses:
                return "u:" + x
            if x in self.pads:
                return "p:" + x
            return "b:" + x
        s, t = nid(frm), nid(to)
        prev, q = {s: None}, collections.deque([s])
        while q:
            u = q.popleft()
            if u == t:
                break
            for v in sorted(G[u]):
                if v in prev or not self.passable(v, s, t):
                    continue
                prev[v] = u
                q.append(v)
        if t not in prev:
            return None
        out, u = [], t
        while u:
            out.append(u)
            u = prev[u]
        return out[::-1]

    def resolve_flows(self):
        c, d = self.c, self.d
        flows = []
        def norm(path):
            out = []
            for a in path:
                n = "u:" + a if a in c.buses else ("p:" + a if a in self.pads else "b:" + str(a).split(".")[0])
                if not out or out[-1] != n:
                    out.append(n)
            return out
        for i, fl in enumerate(d.get("flows", []) or []):
            if fl.get("path"):
                p = norm(fl["path"])
            else:
                p = self.find(fl["from"], fl["to"])
                if p is None:
                    self.warn.append(f"[到達] {fl.get('label', '')}: {fl['from']} から {fl['to']} への経路が見つからない")
                    continue
            flows.append(dict(fl, nodes=p))
        if d.get("scenario"):
            for st in c.s.get("scenario", {}).get("steps", []):
                for fl in st.get("flows", []):
                    flows.append({"label": f'{st.get("title", "")}: {fl.get("label", "")}'.strip(": "),
                                  "color": fl.get("color"), "nodes": norm(fl["path"]), "bw": fl.get("bw")})
        for i, fl in enumerate(flows):
            fl.setdefault("color", PAL[i % len(PAL)])
            if not fl.get("color"):
                fl["color"] = PAL[i % len(PAL)]
        return flows

    # ---------- 座標 ----------
    def center(self, n):
        k, i = n[:2], n[2:]
        if k == "p:":
            return self.padpos.get(i)
        if k == "b:":
            if i in self.extpos:
                return self.ext_rect(i)[4]
            r = self.R.get(i)
            return (r[0] + r[2] / 2, r[1] + r[3] / 2) if r else None
        return None

    def ext_rect(self, bid):
        pid = self.extpos[bid]
        side = self.pads[pid].get("side", "W")
        px, py = self.padpos.get(pid, (0, 0))
        w, h = 1.7, 0.8
        g = 1.6 if side in "WE" else 0.9                 # 端子の名前を書く間
        if side == "W":
            r = (-g - w, py - h / 2, w, h)
        elif side == "E":
            r = (self.W + g, py - h / 2, w, h)
        elif side == "N":
            r = (px - w / 2, -g - h, w, h)
        else:
            r = (px - w / 2, self.H + g, w, h)
        # 矢印はチップ側の辺の真ん中で止める(名前は反対側に書く)
        edge = {"W": (r[0] + r[2], r[1] + r[3] / 2), "E": (r[0], r[1] + r[3] / 2),
                "N": (r[0] + r[2] / 2, r[1] + r[3]), "S": (r[0] + r[2] / 2, r[1])}[side]
        return (*r, edge)

    def points(self, fl):
        """経路の折れ線(mm)と通過要素の名前"""
        ns = fl["nodes"]
        pts, names = [], []
        def add(p):
            if p and (not pts or abs(pts[-1][0] - p[0]) + abs(pts[-1][1] - p[1]) > 1e-6):
                pts.append(p)
        def go(p):                                       # 直前の点から L 字で
            if not pts:
                add(p)
                return
            x0, y0 = pts[-1]
            if abs(x0 - p[0]) > 1e-6 and abs(y0 - p[1]) > 1e-6:
                add((p[0], y0))
            add(p)
        for j, n in enumerate(ns):
            k, i = n[:2], n[2:]
            if k == "u:":
                y = self.bus_y[i]
                prev = pts[-1] if pts else None
                nxt = self.center(ns[j + 1]) if j + 1 < len(ns) else None
                if prev:
                    add((prev[0], y)) if abs(prev[1] - y) > 1e-6 else None
                if nxt:
                    add((nxt[0], y))
                names.append(self.c.buses[i].get("label", i))
                continue
            if k == "b:" and i in self.phy:
                # PHY: 端子との間に挟む(端子 → PHY → コントローラ / コントローラ → PHY → 端子)
                rect, pid, side, plab = self.phy[i]
                pc = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
                before = ns[j - 1] if j > 0 else None
                after = ns[j + 1] if j + 1 < len(ns) else None
                if before == "p:" + pid:
                    go(pc); names.append(plab)
                    go(self.center(n)); names.append(self.label.get(i, i))
                    continue
                if after == "p:" + pid:
                    go(self.center(n)); names.append(self.label.get(i, i))
                    go(pc); names.append(plab)
                    continue
            c_ = self.center(n)
            if c_ is None:
                continue
            go(c_)
            if k == "p:":
                names.append(self.pads[i].get("label", i))
            else:
                names.append(self.label.get(i, i))
        return pts, names

    def capacity(self, fl):
        """通る要素の帯域の最小(端子の bw・バスの width×clock)"""
        caps = []
        for n in fl["nodes"]:
            k, i = n[:2], n[2:]
            if k == "p:" and bps(self.pads[i].get("bw")):
                caps.append((bps(self.pads[i]["bw"]), self.pads[i].get("label", i)))
            if k == "u:":
                b = bps(bw_text(self.c.buses[i]))
                if b:
                    caps.append((b, self.c.buses[i].get("label", i)))
        return min(caps) if caps else None

    # ---------- 図 ----------
    def svg(self):
        S = min(1000 / self.W, 700 / self.H)
        sides = {self.pads[p].get("side", "W") for p in self.extpos.values()}
        mg = {sd: (3.5 * S if sd in "WE" else 2.0 * S) if sd in sides else 50 for sd in "WENS"}
        def X(x):
            return 20 + mg["W"] + x * S
        def Y(y):
            return 70 + mg["N"] + y * S
        Wpx = X(self.W) + mg["E"] + 20
        M = mg["S"] / .6
        rows = []
        for i, fl in enumerate(self.flows):
            pts, names = self.points(fl)
            def inside(q):
                return -1e-6 <= q[0] <= self.W + 1e-6 and -1e-6 <= q[1] <= self.H + 1e-6
            # チップの中だけ数える(チップ外の Host・DRAM までの線は含めない)
            dist = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]) if inside(a) and inside(b))
            fl["_pts"], fl["_names"], fl["_mm"] = pts, names, dist
            cap = self.capacity(fl)
            fl["_cap"] = cap
            if fl.get("bw") and cap and bps(fl["bw"]) > cap[0] * 1.0001:
                self.warn.append(f"[帯域] {fl.get('label', '')}: {fl['bw']} を流すが、{cap[1]} は {fmt_bw(cap[0])}")
            if self.d.get("max_mm") and dist > float(self.d["max_mm"]):
                self.warn.append(f"[距離] {fl.get('label', '')}: 配線距離 ≈{dist:.1f}mm > {self.d['max_mm']}mm。"
                                 f"送り先を入口の近くに置くか、途中で受け直す")
        tab_y = Y(self.H) + mg["S"] + 40
        H = tab_y + 30 + len(self.flows) * 22 + 60
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(Wpx)} {f(H)}" font-family="\'Noto Sans JP\',sans-serif">'
             f'<style>.h{{paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}}</style>'
             f'<rect width="100%" height="100%" fill="#fff"/>'
             f'<text x="16" y="30" font-size="17" font-weight="800" fill="{INK}">'
             f'{esc(self.d.get("title", "データフロー検討(フロアプラン)"))}</text>'
             f'<text x="16" y="50" font-size="11" fill="{GRAY}">配置は概略(ダイ {f(self.W)} × {f(self.H)} mm)。'
             f'矢印 = データの流れ、太さ = 帯域、番号は下の表。距離はマンハッタン距離の目安</text>']
        # ダイ・外周
        o.append(f'<rect x="{f(X(0))}" y="{f(Y(0))}" width="{f(self.W * S)}" height="{f(self.H * S)}" rx="4" fill="#fcfcfd" '
                 f'stroke="{INK}" stroke-width="2.4"/><rect x="{f(X(self.ring))}" y="{f(Y(self.ring))}" '
                 f'width="{f((self.W - 2 * self.ring) * S)}" height="{f((self.H - 2 * self.ring) * S)}" fill="none" '
                 f'stroke="#cbd5e1" stroke-dasharray="4 4"/><text x="{f(X(0) + 8)}" y="{f(Y(0) - 8)}" font-size="12" '
                 f'font-weight="800" fill="{INK}">{esc(self.c.s.get("chip", {}).get("label", "chip"))}</text>')
        # 辺の名前
        for side, (tx, ty) in {"W": (X(0) - 10, Y(self.H / 2)), "E": (X(self.W) + 10, Y(self.H / 2)),
                               "N": (X(self.W / 2), Y(0) - 22), "S": (X(self.W / 2), Y(self.H) + 30)}.items():
            o.append(f'<text x="{f(tx)}" y="{f(ty)}" font-size="10" fill="#94a3b8" '
                     f'text-anchor="{"end" if side == "W" else ("start" if side == "E" else "middle")}">{side} 辺</text>')
        # バスの帯
        for bid, bus in self.c.buses.items():
            xs = [self.R[a.split(".")[0]][0] + self.R[a.split(".")[0]][2] / 2 for a in bus.get("attach", []) if a.split(".")[0] in self.R]
            if not xs:
                continue
            y = self.bus_y[bid]
            col = self.c.bus_color[bid]
            o.append(f'<rect x="{f(X(min(xs)) - 10)}" y="{f(Y(y) - 9)}" width="{f((max(xs) - min(xs)) * S + 20)}" height="18" rx="9" '
                     f'fill="{col}" fill-opacity=".14" stroke="{col}" stroke-width="1.2"/>'
                     f'<text class="h" x="{f(X(min(xs)) - 6)}" y="{f(Y(y) + 26)}" font-size="10.5" font-weight="800" fill="{col}">'
                     f'{esc(bus.get("label", bid))}</text>')
            for a in bus.get("attach", []):              # ブロックから帯への接続
                bb = a.split(".")[0]
                if bb not in self.R:
                    continue
                r = self.R[bb]
                cxx = r[0] + r[2] / 2
                yb = r[1] + r[3] if r[1] + r[3] < y else r[1]
                o.append(f'<line x1="{f(X(cxx))}" y1="{f(Y(yb))}" x2="{f(X(cxx))}" y2="{f(Y(y))}" stroke="{col}" stroke-width="2" opacity=".5"/>')
        top_txt = []                                     # 流れの上に書く文字
        # ブロック(親 → 子の順)
        order = sorted(self.R, key=lambda b: self.c.blocks.get(b, {}).get("_depth", 1))
        for bid in order:
            r = self.R[bid]
            mem = self.kind.get(bid) == "memory"
            child = bool(self.c.blocks.get(bid, {}).get("_parent"))
            fill, st = ("#fff7ed", "#c2410c") if mem else (("#f1f5f9", "#475569") if child else ("#ffffff", INK))
            o.append(f'<rect x="{f(X(r[0]))}" y="{f(Y(r[1]))}" width="{f(r[2] * S)}" height="{f(r[3] * S)}" rx="4" fill="{fill}" '
                     f'stroke="{st}" stroke-width="{1.2 if child else 1.6}"/>')
            top_txt.append(f'<text class="h" x="{f(X(r[0]) + 6)}" y="{f(Y(r[1]) + 15)}" font-size="{10.5 if child else 12}" font-weight="700" '
                           f'fill="{INK}">{esc(self.label.get(bid, bid))}</text>')
        # PHY・端子・チップ外
        for ctrl, (rect, pid, side, plab) in self.phy.items():
            o.append(f'<rect x="{f(X(rect[0]))}" y="{f(Y(rect[1]))}" width="{f(rect[2] * S)}" height="{f(rect[3] * S)}" rx="3" '
                     f'fill="#ecfdf5" stroke="#047857" stroke-width="1.6"/>')
            vert = side in "WE"
            tx, ty = X(rect[0] + rect[2] / 2), Y(rect[1] + rect[3] / 2)
            rot = f' transform="rotate(-90 {f(tx)} {f(ty)})"' if vert else ""
            top_txt.append(f'<text class="h" x="{f(tx)}" y="{f(ty + 4)}" font-size="10.5" font-weight="800" fill="#047857" text-anchor="middle"{rot}>{esc(plab)}</text>')
        for pid, (px, py) in self.padpos.items():
            p = self.pads[pid]
            o.append(f'<rect x="{f(X(px) - 7)}" y="{f(Y(py) - 7)}" width="14" height="14" fill="#cbd5e1" stroke="{INK}" stroke-width="1.5"/>')
            side = p.get("side", "W")
            g = 1.6 * S
            if side in "WE":
                lx, ly, anc = X(px) + (-g / 2 if side == "W" else g / 2), Y(py) - 22, "middle"
            else:
                lx, ly, anc = X(px) + 14, Y(py) + (-12 if side == "N" else 26), "start"
            lines = [p.get("label", pid)] + ([p["bw"]] if p.get("bw") else [])
            for j, t in enumerate(lines):
                top_txt.append(f'<text class="h" x="{f(lx)}" y="{f(ly + j * 13 - (13 if side in "WE" and len(lines) > 1 else 0))}" '
                               f'font-size="10.5" font-weight="{700 if j == 0 else 400}" fill="{INK}" text-anchor="{anc}">{esc(t)}</text>')
        for bid in self.extpos:
            x, y, w, h, _ = self.ext_rect(bid)
            sd = self.pads[self.extpos[bid]].get("side", "W")
            ty_ = Y(y + h) - 10 if sd == "S" else (Y(y) + 20 if sd == "N" else Y(y + h / 2) + 22)
            o.append(f'<rect x="{f(X(x))}" y="{f(Y(y))}" width="{f(w * S)}" height="{f(h * S)}" rx="4" fill="#f8fafc" stroke="{GRAY}" '
                     f'stroke-width="1.4" stroke-dasharray="5 4"/><text x="{f(X(x + w / 2))}" y="{f(ty_)}" font-size="12" '
                     f'font-weight="700" fill="{INK}" text-anchor="middle">{esc(self.label.get(bid, bid))}</text>')
        # 流れ(重なる区間は少しずらす)
        mx_bw = max([bps(fl.get("bw")) or 0 for fl in self.flows] + [1])
        n = len(self.flows)
        for k, fl in enumerate(self.flows):
            off = (k - (n - 1) / 2) * 7
            pts = [(X(x) + off, Y(y) + off) for x, y in fl["_pts"]]
            if len(pts) < 2:
                continue
            b = bps(fl.get("bw"))
            sw = 3 + (5 * b / mx_bw if b else 2)
            col = fl["color"]
            d_ = "M" + " L".join(f"{f(x)},{f(y)}" for x, y in pts)
            o.append(f'<defs><marker id="dfa{k}" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="3.2" markerHeight="3.2" '
                     f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{col}"/></marker></defs>'
                     f'<path d="{d_}" fill="none" stroke="#fff" stroke-width="{f(sw + 4)}" stroke-linejoin="round" opacity=".9"/>'
                     f'<path d="{d_}" fill="none" stroke="{col}" stroke-width="{f(sw)}" stroke-linejoin="round" '
                     f'stroke-linecap="round" marker-end="url(#dfa{k})" opacity=".92"/>'
                     f'<circle cx="{f(pts[0][0])}" cy="{f(pts[0][1])}" r="10" fill="{col}" stroke="#fff" stroke-width="2"/>'
                     f'<text x="{f(pts[0][0])}" y="{f(pts[0][1] + 4)}" font-size="11" font-weight="800" fill="#fff" '
                     f'text-anchor="middle">{k + 1}</text>')
        o += top_txt
        # 表
        o.append(f'<text x="16" y="{f(tab_y)}" font-size="13" font-weight="800" fill="{INK}">経路</text>')
        for k, fl in enumerate(self.flows):
            y = tab_y + 24 + k * 22
            cap = fl["_cap"]
            over = fl.get("bw") and cap and bps(fl["bw"]) > cap[0] * 1.0001
            far = self.d.get("max_mm") and fl["_mm"] > float(self.d["max_mm"])
            o.append(f'<circle cx="26" cy="{f(y - 4)}" r="8" fill="{fl["color"]}"/><text x="26" y="{f(y)}" font-size="10" '
                     f'font-weight="800" fill="#fff" text-anchor="middle">{k + 1}</text>'
                     f'<text x="42" y="{f(y)}" font-size="11.5" fill="{INK}"><tspan font-weight="800">{esc(fl.get("label", ""))}</tspan>'
                     f'  {esc(" → ".join(fl["_names"]))}  '
                     f'<tspan fill="{"#dc2626" if far else "#0f766e"}" font-weight="700">≈{fl["_mm"]:.1f}mm</tspan>'
                     + (f'  <tspan fill="{"#dc2626" if over else INK}" font-weight="700">{esc(fl["bw"])}</tspan>' if fl.get("bw") else "")
                     + (f'<tspan fill="{GRAY}">(上限 {esc(fmt_bw(cap[0]))}: {esc(cap[1])})</tspan>' if cap else "")
                     + '</text>')
        o.append("</svg>")
        return "".join(o)

    def md(self):
        L = ["# データフロー(フロアプラン)", "", f"ダイ {self.W} × {self.H} mm(概略)", "",
             "| # | 流れ | 経路 | 距離の目安 | 帯域 | 上限(ボトルネック) |", "|---|---|---|---|---|---|"]
        for k, fl in enumerate(self.flows):
            cap = fl.get("_cap")
            L.append(f'| {k + 1} | {fl.get("label", "")} | {" → ".join(fl.get("_names", []))} | {fl.get("_mm", 0):.1f} mm | '
                     f'{fl.get("bw", "")} | {fmt_bw(cap[0]) + " (" + cap[1] + ")" if cap else ""} |')
        return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    df = DataflowFP(c)
    svg = df.svg()
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "dataflow_fp.svg")
    open(p, "w", encoding="utf-8").write(svg)
    open(os.path.join(a.out, "dataflow.md"), "w", encoding="utf-8").write(df.md())
    if a.png:
        to_png(p)
    print(f"生成: {p} / dataflow.md")
    for fl in df.flows:
        print(f'  {fl.get("label", "")}: {" → ".join(fl["_names"])}  ≈{fl["_mm"]:.1f}mm')
    for w in df.info:
        print("  " + w)
    print("lint:", "OK" if not df.warn else "")
    for w in df.warn:
        print("  " + w)


if __name__ == "__main__":
    main()
