#!/usr/bin/env python3
"""ifdiag — ブロック間の接続図(インタフェース図)。

中心のブロックを真ん中に、左に接続元・右に接続先を置き、各枠の中に入出力端子を並べる。
  - 枠の上側: モジュール名(太字)とインスタンス名(灰色)
  - 端子: 枠の辺の内側に、入力 ▷(枠に入る向き)/ 出力 ▶(枠から出る向き)/ 双方向 ◇。バス(束)は塗りつぶし
  - 幅: 端子名の後ろに [N-1:0]、線の上に斜線と本数(/128)。束(AXI 等)は太線にプロトコル名
  - 線は端子の高さをそろえてまっすぐ(相手の枠を、つながる端子の高さに合わせて置く)

  python3 ifdiag.py render spec.yaml -o out/ [--png]
  python3 ifdiag.py rtl <rtl files/dirs> --top soc_top --center u_npu -o if.yaml   # RTL から作る
  python3 ifdiag.py lint spec.yaml

spec(YAML):
  interface:
    title: NPU の接続
    center: {module: npu_top, instance: u_npu, ports: [{name: clk, dir: in}, {name: s_axi, dir: in, bus: AXI4-Lite, width: 32}, ...]}
    left:  [{module: crg, instance: u_crg, ports: [...]}, ...]     # 接続元(省略可: nets から自動で振り分け)
    right: [{module: ..., instance: ..., ports: [...]}]           # 接続先
    nets:  [{from: u_crg.npu_clk, to: u_npu.clk}, ...]            # 出力 → 入力(束は master → slave)
"""
import sys, os, re, argparse, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import FONT, INK, GRAY, esc, f, wlen, text_overlaps, to_png

ROW = 24          # 端子1行の高さ
HEAD = 50         # 枠の上(モジュール名・インスタンス名)
PADB = 12         # 枠の下の余白
GAPG = 14         # 相手の枠どうしの間
CH = 170          # 中心と相手の間(線・幅の文字)
ICON = 9
PIN = 18          # 端子の札の長さ(枠の外)
BUS_COL = "#2563eb"
SIG_COL = "#334155"
WARN_COL = "#b45309"
IN_COL, OUT_COL, IO_COL = "#0f766e", "#7c3aed", "#b45309"


def rng(w):
    return f"[{int(w) - 1}:0]" if w and int(w) > 1 else ""


def port_text(p):
    """枠の中の端子の文字: 名前 + 幅(束はプロトコル)"""
    if p.get("bus"):
        return p["name"], f"{p['bus']}" + (f" {p['width']}b" if p.get("width") else "")
    return p["name"], rng(p.get("width"))


class IfDiag:
    def __init__(self, spec):
        s = spec.get("interface", spec)
        self.s = s
        self.warn = []
        self.center = s["center"]
        self.blocks = {self.center["instance"]: self.center}
        self.side_blocks = {}          # (inst, "L"/"R") → 枠(同じインスタンスを左右両方に描いてよい)
        for side in ("left", "right"):
            for b in s.get(side, []) or []:
                sd = "L" if side == "left" else "R"
                self.side_blocks[(b["instance"], sd)] = b
                if b["instance"] in self.blocks:      # 端子の定義は両側を合わせる
                    m = dict(self.blocks[b["instance"]])
                    m["ports"] = list(m.get("ports", [])) + [p for p in b.get("ports", [])
                                                             if p["name"] not in {q["name"] for q in m.get("ports", [])}]
                    self.blocks[b["instance"]] = m
                else:
                    self.blocks[b["instance"]] = b
        self.nets = s.get("nets", []) or []
        self.port = {}
        for inst, b in self.blocks.items():
            for p in b.get("ports", []):
                self.port[f"{inst}.{p['name']}"] = p
        self.check()
        self.assign_sides()
        self.layout()

    # ---------- 検査 ----------
    def check(self):
        drv = {}
        for n in self.nets:
            for k in ("from", "to"):
                if n[k] not in self.port:
                    self.warn.append(f"[端子] {n[k]} が定義にない")
            if n["from"] in self.port and n["to"] in self.port:
                a, b = self.port[n["from"]], self.port[n["to"]]
                da, db = a.get("dir", "in"), b.get("dir", "in")
                if da == "in" and db != "inout" or db == "out" and da != "inout":
                    self.warn.append(f"[向き] {n['from']}({da}) → {n['to']}({db}): 出力 → 入力 になっていない")
                wa, wb = a.get("width", 1), b.get("width", 1)
                if a.get("width") and b.get("width") and int(wa) != int(wb):
                    self.warn.append(f"[幅] {n['from']} {wa}b → {n['to']} {wb}b が違う")
                    n["_mismatch"] = True
                if a.get("bus") != b.get("bus") and (a.get("bus") or b.get("bus")):
                    self.warn.append(f"[束] {n['from']} と {n['to']} のプロトコルが違う({a.get('bus')} / {b.get('bus')})")
                drv.setdefault(n["to"], []).append(n["from"])
        for k, v in drv.items():
            if len(v) > 1 and self.port.get(k, {}).get("dir") != "inout":
                self.warn.append(f"[複数駆動] {k} を {', '.join(v)} が駆動")
        ci = self.center["instance"]
        for p in self.center.get("ports", []):
            k = f"{ci}.{p['name']}"
            if p.get("dir", "in") == "in" and k not in drv and "tie" not in p:
                self.warn.append(f"[未接続] 入力 {k} を駆動するものがない(固定値なら tie: 0 等)")

    # ---------- 振り分け ----------
    def peers_of(self, key):
        out = []
        for n in self.nets:
            if n["from"] == key:
                out.append((n["to"], n))
            elif n["to"] == key:
                out.append((n["from"], n))
        return out

    def assign_sides(self):
        """中心の端子を、相手のいる側(左=接続元・右=接続先)の辺に置く。相手の側の指定がなければ、
        中心の入力を駆動するもの → 左、中心の出力を受けるもの → 右(両方なら両側に同じ枠を描く)"""
        ci = self.center["instance"]
        def fixed_side(o):
            """相手の端子 o(inst.port)を描く側: その端子を持つ枠が置かれた側"""
            inst, pn = o.split(".", 1)
            for sd in ("L", "R"):
                b = self.side_blocks.get((inst, sd))
                if b and any(p["name"] == pn for p in b.get("ports", [])):
                    return sd
            return None
        self.cside = {}
        self.peer_side = {}          # (inst, side) → 描く
        for p in self.center.get("ports", []):
            k = f"{ci}.{p['name']}"
            pe = self.peers_of(k)
            d = p.get("dir", "in")
            side = None
            for o, n in pe:
                if fixed_side(o):
                    side = fixed_side(o)
                    break
            if side is None:
                side = "L" if d == "in" else "R"
            self.cside[p["name"]] = side
            for o, n in pe:
                inst = o.split(".")[0]
                self.peer_side.setdefault((inst, side), []).append((p["name"], o, n))

    # ---------- 配置 ----------
    def layout(self):
        ci = self.center["instance"]
        cps = self.center.get("ports", [])
        # 中心の各辺: 相手の枠ごとにまとまりを作り、まとまりの間を相手の枠の上下の余白ぶん空ける
        self.rows = {"L": [], "R": []}       # (port name or None(gap), peer inst)
        order = {"L": [], "R": []}
        for side, lst in (("L", self.s.get("left", []) or []), ("R", self.s.get("right", []) or [])):
            order[side] = [b["instance"] for b in lst]
        for (inst, side) in self.peer_side:
            if inst not in order[side]:
                order[side].append(inst)
        self.groups = {"L": [], "R": []}
        for side in ("L", "R"):
            done = set()
            for inst in order[side]:
                mine = [pn for pn, o, n in self.peer_side.get((inst, side), [])]
                names = []
                for p in cps:
                    if p["name"] not in mine:
                        continue
                    # 1つの端子が複数の相手へ(ファンアウト): 2つ目以降の相手は自分のまとまりに仮の行を取り、線を縦に振る
                    names.append(p["name"] if p["name"] not in done else f"~{inst}~{p['name']}")
                if not names:
                    continue
                done |= {n_ for n_ in names if not n_.startswith("~")}
                self.groups[side].append((inst, names))
            rest = [p["name"] for p in cps if self.cside[p["name"]] == side and p["name"] not in done]
            if rest:
                self.groups[side].append((None, rest))
        # y: 中心の枠の上からの位置。相手の枠の見出しが入るよう、まとまりの間に HEAD+PADB+GAPG を空ける
        self.cy = {}
        hmax = 0
        for side in ("L", "R"):
            y = HEAD
            for gi, (inst, names) in enumerate(self.groups[side]):
                if gi and inst is not None:
                    y += HEAD + PADB + GAPG          # 次の相手の枠の見出しと余白
                elif gi:
                    y += ROW / 2
                for nm in names:
                    self.cy[(side, nm)] = y + ROW / 2
                    y += ROW
            hmax = max(hmax, y)
        # 幅
        def tw(p):
            a, b = port_text(p)
            return wlen(a) * 7.4 + wlen(b) * 6.2 + 34 + PIN
        lw = max([tw(next(p for p in cps if p["name"] == nm)) for _, ns in self.groups["L"] for nm in ns if not nm.startswith("~")] + [0])
        rw = max([tw(next(p for p in cps if p["name"] == nm)) for _, ns in self.groups["R"] for nm in ns if not nm.startswith("~")] + [0])
        cw = max(lw + rw + 30, wlen(self.center.get("module", "")) * 10 + 40, wlen(ci) * 8 + 40, 220)
        self.cw, self.ch = cw, hmax + PADB
        # 相手の枠
        self.pboxes = []     # dict(inst, side, x, y, w, h, ports=[(pname, y)])
        for side in ("L", "R"):
            for inst, names in self.groups[side]:
                if inst is None:
                    continue
                b = self.side_blocks.get((inst, side)) or self.blocks.get(inst, {"instance": inst, "module": "?", "ports": []})
                rows, seen = [], set()
                for pn, o, n in self.peer_side[(inst, side)]:
                    if o in seen:
                        continue
                    seen.add(o)
                    yv = self.cy.get((side, f"~{inst}~{pn}"), self.cy[(side, pn)])
                    rows.append((o.split(".", 1)[1], yv))
                rows.sort(key=lambda t: t[1])
                ys = [r[1] for r in rows]
                top = ys[0] - ROW / 2 - HEAD         # 見出しの下に、つながる端子の高さで並ぶ
                bot = ys[-1] + ROW / 2 + PADB
                ports = [self.port.get(f"{inst}.{pn}", {"name": pn}) for pn, _ in rows]
                w = max([wlen(port_text(p)[0]) * 7.4 + 34 + PIN for p in ports] +
                        [wlen(b.get("module", "")) * 9.4 + 30, wlen(inst) * 7.6 + 30, 150])
                self.pboxes.append(dict(inst=inst, side=side, b=b, y=top, h=bot - top, w=w, rows=rows, ports=ports))
        lmax = max([p["w"] for p in self.pboxes if p["side"] == "L"] + [0])
        rmax = max([p["w"] for p in self.pboxes if p["side"] == "R"] + [0])
        self.x0 = 30
        self.cx = self.x0 + (lmax + CH if lmax else 0)
        for p in self.pboxes:
            p["x"] = self.cx - CH - p["w"] if p["side"] == "L" else self.cx + cw + CH
        self.W = self.cx + cw + (CH + rmax if rmax else 0) + 30
        self.top = 60
        ymin = min([p["y"] for p in self.pboxes] + [0])
        self.dy = self.top - ymin
        self.H = self.dy + max([self.ch] + [p["y"] + p["h"] for p in self.pboxes]) + 70

    # ---------- 描画 ----------
    def icon(self, x, y, d, side, bus):
        """端子の記号: 枠の辺の上に置く矢印形の札(回路図の端子記号)。
        入力 = 内へ向かう札、出力 = 外へ向かう札、双方向 = 両端がとがった札。束(バス)は塗りつぶし。
        (三角だけの記号は FF のクロック入力に見えるので使わない)"""
        i = 1 if side == "L" else -1                 # 辺から内側への向き
        col = IN_COL if d == "in" else OUT_COL if d == "out" else IO_COL
        fill = col if bus else "#fff"
        L_, H_, T_ = PIN, 6, 6                       # 長さ・半分の高さ・とがりの長さ
        x0, xi = x - i * L_ / 2, x + i * L_ / 2       # 枠の辺の上(半分外・半分内)
        if d == "in":        # 辺の側が四角、内側でとがる
            pts = [(x0, y - H_), (xi - i * T_, y - H_), (xi, y), (xi - i * T_, y + H_), (x0, y + H_)]
        elif d == "out":     # 内側が四角、辺の側でとがる
            pts = [(xi, y - H_), (x0 + i * T_, y - H_), (x0, y), (x0 + i * T_, y + H_), (xi, y + H_)]
        else:                # 両端がとがる
            pts = [(x0, y), (x0 + i * T_, y - H_), (xi - i * T_, y - H_), (xi, y), (xi - i * T_, y + H_), (x0 + i * T_, y + H_)]
        return (f'<polygon points="{" ".join(f"{f(a)},{f(b)}" for a, b in pts)}" fill="{fill}" '
                f'stroke="{col}" stroke-width="1.5" stroke-linejoin="round"/>')

    def pin_end(self, x, side):
        """線がつながる所(札の外の端)"""
        return x + (-PIN / 2 if side == "L" else PIN / 2)

    def box(self, x, y, w, h, module, inst, center=False, ext=False):
        sw = 2.6 if center else 1.8
        fill = "#f1f5f9" if not center else "#eef2ff"
        dash = ' stroke-dasharray="6 4"' if ext else ""
        return (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="8" fill="{fill}" '
                f'stroke="{INK}" stroke-width="{sw}"{dash}/>'
                f'<text x="{f(x + w / 2)}" y="{f(y + 22)}" font-size="{15 if center else 13.5}" font-weight="800" '
                f'fill="{INK}" text-anchor="middle">{esc(module)}</text>'
                f'<text x="{f(x + w / 2)}" y="{f(y + 39)}" font-size="11.5" fill="{GRAY}" text-anchor="middle">{esc(inst)}</text>'
                f'<line x1="{f(x + 8)}" y1="{f(y + HEAD - 4)}" x2="{f(x + w - 8)}" y2="{f(y + HEAD - 4)}" stroke="#cbd5e1"/>')

    def port_row(self, x, y, side, p, detail=True):
        """辺 x(side=L なら左辺)の内側に、記号と名前・幅(相手の枠は名前だけ: 幅は線の上に書く)"""
        d = p.get("dir", "in")
        a, b = port_text(p)
        if not detail:
            b = ""
        o = self.icon(x, y, d, side, bool(p.get("bus")))
        tx = x + (PIN / 2 + 8) if side == "L" else x - (PIN / 2 + 8)
        anc = "start" if side == "L" else "end"
        if side == "L":
            o += (f'<text x="{f(tx)}" y="{f(y + 4.5)}" font-size="12.5" font-weight="700" fill="{INK}">{esc(a)}'
                  f'<tspan dx="5" font-size="11" font-weight="400" fill="{GRAY}">{esc(b)}</tspan></text>')
        else:
            o += (f'<text x="{f(tx)}" y="{f(y + 4.5)}" font-size="12.5" font-weight="700" fill="{INK}" text-anchor="end">'
                  f'<tspan font-size="11" font-weight="400" fill="{GRAY}">{esc(b)}</tspan><tspan dx="5">{esc(a)}</tspan></text>'
                  if b else
                  f'<text x="{f(tx)}" y="{f(y + 4.5)}" font-size="12.5" font-weight="700" fill="{INK}" text-anchor="end">{esc(a)}</text>')
        return o

    def wire(self, x1, y1, x2, y2, n, a, b, lane):
        """出力 → 入力 の線。幅は斜線と本数、束は太線とプロトコル"""
        bus = a.get("bus") or b.get("bus")
        col = WARN_COL if n.get("_mismatch") else (BUS_COL if bus else SIG_COL)
        sw = 4.2 if bus else 1.8
        if abs(y1 - y2) < 0.5:
            d = f"M{f(x1)},{f(y1)} L{f(x2)},{f(y2)}"
            mx, my = (x1 + x2) / 2, y1
        else:   # 1つの出力から複数へ: 間で縦に振り分け
            xm = (x1 + x2) / 2 + lane * 10
            d = f"M{f(x1)},{f(y1)} L{f(xm)},{f(y1)} L{f(xm)},{f(y2)} L{f(x2)},{f(y2)}"
            mx, my = (xm + x2) / 2, y2
        dirn = 1 if x2 > x1 else -1
        o = f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{sw}"/>'
        if "inout" not in (a.get("dir"), b.get("dir")):   # 双方向は矢印なし(札の形で示す)
            o += f'<polygon points="{f(x2)},{f(y2)} {f(x2 - dirn * 9)},{f(y2 - 4.5)} {f(x2 - dirn * 9)},{f(y2 + 4.5)}" fill="{col}"/>'
        w = (b.get("width") or a.get("width"))
        if bus:
            lab = f"{bus}" + (f" {w}b" if w else "")
            o += (f'<text x="{f(mx)}" y="{f(my - 8)}" font-size="11" font-weight="800" fill="{col}" text-anchor="middle" '
                  f'paint-order="stroke" stroke="#fff" stroke-width="3">{esc(lab)}</text>')
        elif w and int(w) > 1:
            o += (f'<line x1="{f(mx - 5)}" y1="{f(my + 6)}" x2="{f(mx + 5)}" y2="{f(my - 6)}" stroke="{col}" stroke-width="1.6"/>'
                  f'<text x="{f(mx + 7)}" y="{f(my - 6)}" font-size="11" font-weight="800" fill="{col}" '
                  f'paint-order="stroke" stroke="#fff" stroke-width="3">{int(w)}</text>')
        if n.get("label"):
            o += (f'<text x="{f(mx)}" y="{f(my + 15)}" font-size="10.5" fill="{GRAY}" text-anchor="middle">{esc(n["label"])}</text>')
        return o

    def svg(self):
        ci = self.center["instance"]
        dy = self.dy
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(self.W)} {f(self.H)}" '
             f'width="{f(self.W)}" height="{f(self.H)}" font-family="{FONT}">',
             '<rect width="100%" height="100%" fill="#fff"/>',
             f'<text x="20" y="32" font-size="18" font-weight="800" fill="{INK}">{esc(self.s.get("title", ci + " の接続"))}</text>']
        if self.groups["L"] and any(i for i, _ in self.groups["L"]):
            o.append(f'<text x="{f(self.cx - CH / 2)}" y="{f(self.top - 18)}" font-size="12" font-weight="700" fill="{GRAY}" text-anchor="middle">接続元</text>')
        if self.groups["R"] and any(i for i, _ in self.groups["R"]):
            o.append(f'<text x="{f(self.cx + self.cw + CH / 2)}" y="{f(self.top - 18)}" font-size="12" font-weight="700" fill="{GRAY}" text-anchor="middle">接続先</text>')
        # 中心
        o.append(self.box(self.cx, dy, self.cw, self.ch, self.center.get("module", ""), ci, center=True))
        cps = {p["name"]: p for p in self.center.get("ports", [])}
        for side in ("L", "R"):
            ex = self.cx if side == "L" else self.cx + self.cw
            for inst, names in self.groups[side]:
                for nm in names:
                    if nm.startswith("~"):
                        continue
                    o.append(self.port_row(ex, dy + self.cy[(side, nm)], side, cps[nm]))
                    if "tie" in cps[nm]:     # 固定値: 辺の外に短い線と値
                        yy_ = dy + self.cy[(side, nm)]
                        x2_ = ex - 30 - PIN / 2 if side == "L" else ex + 30 + PIN / 2
                        o.append(f'<line x1="{f(x2_)}" y1="{f(yy_)}" x2="{f(self.pin_end(ex, side))}" y2="{f(yy_)}" stroke="{SIG_COL}" stroke-width="1.6"/>'
                                 f'<text x="{f(x2_ - 4 if side == "L" else x2_ + 4)}" y="{f(yy_ + 4)}" font-size="11.5" font-weight="800" '
                                 f'fill="{GRAY}" text-anchor="{"end" if side == "L" else "start"}">{esc(str(cps[nm]["tie"]))}</text>')
        # 相手
        pkey = {}
        for pb in self.pboxes:
            b = pb["b"]
            o.append(self.box(pb["x"], dy + pb["y"], pb["w"], pb["h"], b.get("module", "?"), pb["inst"], ext=b.get("external")))
            ex = pb["x"] + pb["w"] if pb["side"] == "L" else pb["x"]
            ps = "R" if pb["side"] == "L" else "L"     # 中心に向いた辺
            for (pn, y), p in zip(pb["rows"], pb["ports"]):
                o.append(self.port_row(ex, dy + y, ps, p, detail=False))
                pkey[(pb["inst"], pb["side"], pn)] = (self.pin_end(ex, ps), dy + y)
        # 線
        lanes = {}
        for side in ("L", "R"):
            for (inst, sd), lst in self.peer_side.items():
                if sd != side:
                    continue
                for pn, other, n in lst:
                    cx_ = self.pin_end(self.cx if side == "L" else self.cx + self.cw, side)
                    cyy = dy + self.cy[(side, pn)]
                    oi, opn = other.split(".", 1)
                    px, py = pkey[(oi, side, opn)]
                    a = self.port.get(n["from"], {})
                    b = self.port.get(n["to"], {})
                    k = (oi, opn)
                    lanes[k] = lanes.get(k, -1) + 1
                    lane = lanes[k]
                    if n["from"] == other:      # 相手 → 中心
                        o.append(self.wire(px, py, cx_, cyy, n, a, b, lane))
                    else:
                        o.append(self.wire(cx_, cyy, px, py, n, a, b, lane))
        # 凡例
        ly = self.H - 34
        lx = 24
        o.append(f'<text x="{lx}" y="{f(ly + 4)}" font-size="11" fill="{GRAY}">凡例:</text>')
        lx += 40
        for d, bus, t in (("in", False, "入力"), ("out", False, "出力"), ("inout", False, "双方向"), ("in", True, "バス(束)")):
            ex_ = lx + PIN / 2                       # 枠の左辺を短い縦線で示し、その上に札
            o.append(f'<line x1="{f(ex_)}" y1="{f(ly - 10)}" x2="{f(ex_)}" y2="{f(ly + 10)}" stroke="{INK}" stroke-width="2"/>')
            o.append(self.icon(ex_, ly, d, "R" if d == "out" else "L", bus))   # 出力は右辺の向き(外へ)
            o.append(f'<text x="{f(ex_ + PIN / 2 + 8)}" y="{f(ly + 4)}" font-size="11" fill="{INK}">{t}</text>')
            lx = ex_ + PIN / 2 + 8 + wlen(t) * 6.6 + 22
        o.append(f'<line x1="{lx}" y1="{f(ly + 6)}" x2="{lx + 10}" y2="{f(ly - 6)}" stroke="{SIG_COL}" stroke-width="1.6"/>'
                 f'<text x="{lx + 14}" y="{f(ly + 4)}" font-size="11" fill="{INK}">N = 本数(ビット幅)</text>')
        o.append("</svg>")
        return "".join(o)

    def drawio(self):
        """draw.io: 枠・端子の文字・線(位置は SVG と同じ)"""
        from xml.sax.saxutils import escape as xesc
        cells, nid = [], [2]
        def cell(v, st, x, y, w, h):
            nid[0] += 1
            cells.append(f'<mxCell id="c{nid[0]}" value="{xesc(v, {chr(34): "&quot;"})}" style="{st}" vertex="1" parent="1">'
                         f'<mxGeometry x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" as="geometry"/></mxCell>')
        def edge(pts, st):
            nid[0] += 1
            (x1, y1), (x2, y2) = pts[0], pts[-1]
            mid = "".join(f'<mxPoint x="{f(a)}" y="{f(b)}"/>' for a, b in pts[1:-1])
            cells.append(f'<mxCell id="c{nid[0]}" style="{st}" edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
                         f'<mxPoint x="{f(x1)}" y="{f(y1)}" as="sourcePoint"/><mxPoint x="{f(x2)}" y="{f(y2)}" as="targetPoint"/>'
                         + (f'<Array as="points">{mid}</Array>' if mid else "") + '</mxGeometry></mxCell>')
        dy = self.dy
        ci = self.center["instance"]
        def bx(x, y, w, h, m, i, center=False):
            cell(f"<b>{m}</b><br><font color='#64748b'>{i}</font>",
                 f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;verticalAlign=top;fillColor={'#eef2ff' if center else '#f1f5f9'};strokeWidth={2.6 if center else 1.8};",
                 x, y, w, h)
        bx(self.cx, dy, self.cw, self.ch, self.center.get("module", ""), ci, True)
        cps = {p["name"]: p for p in self.center.get("ports", [])}
        mark = {"in": "→", "out": "→", "inout": "↔"}
        for side in ("L", "R"):
            for inst, names in self.groups[side]:
                for nm in names:
                    if nm.startswith("~"):
                        continue
                    a, b = port_text(cps[nm])
                    y = dy + self.cy[(side, nm)] - 10
                    t = f"{mark.get(cps[nm].get('dir', 'in'))} {a} {b}" if side == "L" else f"{b} {a} {mark.get(cps[nm].get('dir', 'in'))}"
                    cell(t, f"text;html=1;align={'left' if side == 'L' else 'right'};fontSize=12;",
                         self.cx + 4 if side == "L" else self.cx + self.cw - 4 - self.cw / 2, y, self.cw / 2, 20)
        for pb in self.pboxes:
            bx(pb["x"], dy + pb["y"], pb["w"], pb["h"], pb["b"].get("module", "?"), pb["inst"])
            for (pn, y), p in zip(pb["rows"], pb["ports"]):
                a, b = port_text(p)
                left = pb["side"] == "R"
                t = f"{mark.get(p.get('dir', 'in'))} {a} {b}" if left else f"{b} {a} {mark.get(p.get('dir', 'in'))}"
                cell(t, f"text;html=1;align={'left' if left else 'right'};fontSize=12;",
                     pb["x"] + 4, dy + y - 10, pb["w"] - 8, 20)
        for (inst, sd), lst in self.peer_side.items():
            for pn, other, n in lst:
                cx_ = self.cx if sd == "L" else self.cx + self.cw
                cyy = dy + self.cy[(sd, pn)]
                pb = next(p for p in self.pboxes if p["inst"] == other.split(".")[0] and p["side"] == sd)
                py = dy + next(y for q, y in pb["rows"] if q == other.split(".", 1)[1])
                px = pb["x"] + pb["w"] if sd == "L" else pb["x"]
                a, b = self.port.get(n["from"], {}), self.port.get(n["to"], {})
                bus = a.get("bus") or b.get("bus")
                w = b.get("width") or a.get("width")
                st = (f"endArrow=block;html=1;rounded=0;strokeWidth={4 if bus else 1.8};strokeColor={BUS_COL if bus else SIG_COL};")
                pts = [(px, py), (cx_, cyy)] if n["from"] == other else [(cx_, cyy), (px, py)]
                if abs(py - cyy) > 0.5:
                    xm = (px + cx_) / 2
                    pts = [pts[0], (xm, pts[0][1]), (xm, pts[1][1]), pts[1]]
                edge(pts, st)
                lab = (f"{bus} {w}b" if bus else (f"/{w}" if w and int(w) > 1 else ""))
                if lab:
                    cell(lab, "text;html=1;align=center;fontSize=11;fontStyle=1;", (px + cx_) / 2 - 40, cyy - 22, 80, 16)
        return ('<mxfile><diagram name="接続図"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                + "".join(cells) + '</root></mxGraphModel></diagram></mxfile>')


# ---------- RTL から ----------
AXI_CH = re.compile(r"^(.*?)_?(aw|w|b|ar|r)(valid|ready|addr|data|id|len|size|burst|lock|cache|prot|qos|strb|last|resp|user|region)$", re.I)
APB_SIG = re.compile(r"^(.*?)_?p(sel|enable|write|addr|wdata|rdata|ready|slverr|prot|strb)$", re.I)


def bundle_ports(ports):
    """AXI / APB の信号をまとめて1つの端子(束)に。向きは valid の向きで master/slave を決める"""
    groups, rest = {}, []
    for p in ports:
        m = AXI_CH.match(p["name"])
        if m:
            groups.setdefault(("AXI", m.group(1).rstrip("_")), []).append((p, m.group(2).lower(), m.group(3).lower()))
            continue
        m = APB_SIG.match(p["name"])
        if m:
            groups.setdefault(("APB", m.group(1).rstrip("_")), []).append((p, "p", m.group(2).lower()))
            continue
        rest.append(p)
    out, members = [], {}
    for (proto, pre), sigs in groups.items():
        if len(sigs) < 4:
            rest += [s[0] for s in sigs]
            continue
        if proto == "AXI":
            av = next((p for p, ch, sg in sigs if ch in ("aw", "ar") and sg == "valid"), None)
            master = av is not None and av.get("dir") == "output"
            dw = next((p.get("width") for p, ch, sg in sigs if ch in ("w", "r") and sg == "data"), None)
            lite = not any(sg in ("len", "last") for p, ch, sg in sigs)
            name = pre or "axi"
            out.append({"name": name, "dir": "out" if master else "in", "bus": "AXI4-Lite" if lite else "AXI4",
                        "width": dw})
        else:
            sel = next((p for p, ch, sg in sigs if sg == "sel"), None)
            master = sel is not None and sel.get("dir") == "output"
            dw = next((p.get("width") for p, ch, sg in sigs if sg == "wdata"), None)
            name = pre or "apb"
            out.append({"name": name, "dir": "out" if master else "in", "bus": "APB", "width": dw})
        members[out[-1]["name"]] = [s[0]["name"] for s in sigs]
    for p in rest:
        d = {"input": "in", "output": "out", "inout": "inout"}.get(p.get("dir"), "in")
        out.append({k: v for k, v in {"name": p["name"], "dir": d, "width": p.get("width")}.items() if v not in (None, 1)}
                   | {"name": p["name"], "dir": d})
    return out, members


def from_rtl(paths, top, center, show_unconnected=True):
    from rtl_scan import scan
    db = scan(paths)
    mods = db["modules"]
    tm = mods[top]
    insts = {i["name"]: i for i in tm["instances"]}
    ci = insts[center]
    def ports_of(inst):
        return bundle_ports(mods[inst["module"]]["ports"]) if inst["module"] in mods else ([], {})
    cports, cmem = ports_of(ci)
    # ネット → (inst, 端子)。束は、束の先頭メンバのネットの「接頭辞」で相手を見つける
    def net_of(inst, sig):
        return (inst["conns"].get(sig) or "").strip()
    def base(net):
        return re.sub(r"\[.*?\]", "", net).strip()
    nets, peers = [], {}
    top_ports = {p["name"]: p for p in tm["ports"]}
    want = {"in": ("out", "inout"), "out": ("in", "inout"), "inout": ("in", "out", "inout")}
    for p in cports:
        sigs = cmem.get(p["name"], [p["name"]])
        nset = {base(net_of(ci, s)) for s in sigs if net_of(ci, s)}
        if not nset:
            continue
        found = False
        for oname, oi in insts.items():
            if oname == center or oi["module"] not in mods:
                continue
            ops, omem = ports_of(oi)
            for q in ops:
                if q["dir"] not in want[p["dir"]]:      # 出力 → 入力 の組だけ(同じクロックを受ける仲間は相手にしない)
                    continue
                qs = omem.get(q["name"], [q["name"]])
                qn = {base(net_of(oi, s)) for s in qs if net_of(oi, s)}
                if nset & qn:
                    a, b = (f"{oname}.{q['name']}", f"{center}.{p['name']}")
                    if p["dir"] == "out":
                        a, b = b, a
                    nets.append({"from": a, "to": b})
                    peers.setdefault(oname, {"module": oi["module"], "instance": oname, "ports": []})
                    if q not in peers[oname]["ports"]:
                        peers[oname]["ports"].append(q)
                    found = True
        tp = [n_ for n_ in nset if n_ in top_ports]
        if not found and tp:      # チップの外部端子(トップのポート)
            pin = "(外部端子)"
            peers.setdefault(pin, {"module": top, "instance": pin, "external": True, "ports": []})
            q = {"name": tp[0], "dir": "out" if p["dir"] == "in" else "in" if p["dir"] == "out" else "inout"}
            w = top_ports[tp[0]].get("width")
            if w and w > 1:
                q["width"] = w
            if q not in peers[pin]["ports"]:
                peers[pin]["ports"].append(q)
            a, b = (f"{pin}.{tp[0]}", f"{center}.{p['name']}")
            if p["dir"] == "out":
                a, b = b, a
            nets.append({"from": a, "to": b})
    spec = {"interface": {"title": f"{center}({ci['module']})の接続",
                          "center": {"module": ci["module"], "instance": center, "ports": cports},
                          "nets": nets,
                          "left": [], "right": []}}
    for oname, pb in peers.items():   # 中心へ駆動する端子は左(接続元)、中心から受ける端子は右(接続先)。両方あれば両側に描く
        drv = {n["from"].split(".", 1)[1] for n in nets if n["from"].startswith(oname + ".")}
        rcv = {n["to"].split(".", 1)[1] for n in nets if n["to"].startswith(oname + ".")}
        for side, names in (("left", drv), ("right", rcv)):
            if names:
                spec["interface"][side].append(dict(pb, ports=[q for q in pb["ports"] if q["name"] in names]))
    return spec


def main():
    ap = argparse.ArgumentParser(description="ブロック間の接続図")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render"); r.add_argument("spec"); r.add_argument("-o", "--out", default="out_if"); r.add_argument("--png", action="store_true")
    l = sub.add_parser("lint"); l.add_argument("spec")
    t = sub.add_parser("rtl"); t.add_argument("paths", nargs="+"); t.add_argument("--top", required=True)
    t.add_argument("--center", required=True); t.add_argument("-o", "--out", default="if.yaml")
    a = ap.parse_args()
    if a.cmd == "rtl":
        spec = from_rtl(a.paths, a.top, a.center)
        open(a.out, "w", encoding="utf-8").write(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=200))
        print(f"接続図YAML: {a.out}(左={len(spec['interface']['left'])} 右={len(spec['interface']['right'])} 接続={len(spec['interface']['nets'])})")
        return
    spec = yaml.safe_load(open(a.spec, encoding="utf-8"))
    d = IfDiag(spec)
    svg = d.svg()
    ov = text_overlaps(svg)
    for t_ in ov:
        d.warn.append(f"[重なり] 「{t_[0]}」と「{t_[1]}」")
    if a.cmd == "lint":
        print("lint:", "OK" if not d.warn else "")
        for w in d.warn:
            print("  " + w)
        return
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "interface.svg")
    open(p, "w", encoding="utf-8").write(svg)
    open(os.path.join(a.out, "interface.drawio"), "w", encoding="utf-8").write(d.drawio())
    if a.png:
        to_png(p)
    print(f"生成: {p}")
    print("lint:", "OK" if not d.warn else "")
    for w in d.warn:
        print("  " + w)


if __name__ == "__main__":
    main()
