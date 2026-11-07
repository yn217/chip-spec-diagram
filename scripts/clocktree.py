#!/usr/bin/env python3
"""clocktree — クロック系統図(クロック源 → PLL → 切替(MUX)→ 分周 → ゲート → クロックドメイン)と乗り換え(CDC)

  python3 clocktree.py diagram.yaml -o out/ [--png]          # out/clock_tree.svg と out/clock.md
  python3 clocktree.py diagram.yaml -o out/clock_tree.svg     # ファイル名でも可

diagram.yaml の clocks: を使う
  clocks:
    sources:                          # osc = 水晶/発振器(チップ外・端子経由) / pad = 外部 REFCLK 入力 / rc = チップ内 RC 発振
      - {id: xtal, label: XTAL, freq: 24MHz, kind: osc}
    plls:
      - {id: pll_sys, label: SYS PLL, in: xtal, freq: 2.0GHz, mult: 250, refdiv: 3}   # mult/refdiv は任意(あれば周波数を検算)
    muxes:                            # クロック切替(グリッチレス)。default の入力が通常動作
      - {id: cpu_sel, label: CPU clk sel, in: [pll_arm, xtal], default: pll_arm, sel: SW}
    domains:
      - {id: core_clk, label: core, freq: 1.0GHz, from: pll_sys, div: 2, gate: sw, color: "#2563eb"}
      - {id: ipg, label: IPG, freq: 133MHz, from: noc, div: 6}       # 別ドメインから分周してもよい
      - {id: a53, label: CPU, freq: 1.8GHz, from: cpu_sel, dvfs: [1.8GHz, 1.2GHz, 600MHz]}
  gate: true | sw(ソフトウェア制御)| auto(アイドルで自動)。block/bus に clock_domain: を書くと所属と CDC が決まる。

乗り換えの種類: 同じ源(PLL)から整数比で作ったドメインどうし = 比率同期(同期の乗り換え、同期化 FF 不要)、
それ以外 = 非同期(CDC、同期化 FF/非同期 FIFO が要る)。
チェック(lint): [周波数](源から計算した周波数と書いた周波数の不一致) [未定義] [クロック未割当] [循環] [MUX]
"""
import os, sys, re, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, load, esc, wlen, INK, to_png

PAL = ["#2563eb", "#9333ea", "#db2777", "#0891b2", "#ea580c", "#16a34a", "#ca8a04"]
ASYNC_C, SYNC_C, GRAY = "#dc2626", "#2563eb", "#64748b"
GATE = {True: "CG", "sw": "CG(SW)", "auto": "CG(自動)", "hw": "CG(自動)"}


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def hz(s):
    """'1.8GHz' → 1.8e9。範囲や不明は None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKMG]?)Hz\s*", str(s))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def fmt(v):
    if v is None:
        return "?"
    for u, k in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if v >= k:
            x = v / k
            return (f"{x:.3f}".rstrip("0").rstrip(".")) + u
    return f"{v:g}Hz"


class ClockTree:
    def __init__(self, c):
        self.c = c
        ck = c.s.get("clocks", {}) or {}
        self.srcs, self.plls = ck.get("sources", []), ck.get("plls", [])
        self.muxes, self.doms = ck.get("muxes", []), ck.get("domains", [])
        self.node = {}
        for kind, lst in (("src", self.srcs), ("pll", self.plls), ("mux", self.muxes), ("dom", self.doms)):
            for n in lst:
                n["_k"] = kind
                self.node[n["id"]] = n
        for i, d in enumerate(self.doms):
            d.setdefault("_col", d.get("color") or PAL[i % len(PAL)])
        self.warn, self.info = [], []

    def lab(self, i):
        n = self.node.get(i)
        return n.get("label", i) if n else i

    # ---------- 周波数・源 ----------
    def parent(self, n):
        if n["_k"] == "pll":
            return n.get("in")
        if n["_k"] == "mux":
            return n.get("default") or (n.get("in") or [None])[0]
        if n["_k"] == "dom":
            return n.get("from")
        return None

    def calc(self):
        self.fq, self.root, self.ratio = {}, {}, {}
        busy = set()
        def go(i):
            if i in self.fq:
                return self.fq[i]
            n = self.node.get(i)
            if n is None:
                return None
            if i in busy:
                self.warn.append(f"[循環] クロックの接続が循環している: {i}")
                return None
            busy.add(i)
            p = self.parent(n)
            pf = go(p) if p else None
            if n["_k"] == "src":
                v, r = hz(n.get("freq")), i
            elif n["_k"] == "pll":
                v, r = hz(n.get("freq")), i
                if n.get("mult") and pf:
                    calc = pf * float(n["mult"]) / float(n.get("refdiv", 1)) / float(n.get("postdiv", 1))
                    if v and abs(calc - v) / v > 0.01:
                        self.warn.append(f"[周波数] {self.lab(i)}: 入力 {fmt(pf)} ×{n['mult']}"
                                         f"{'÷' + str(n['refdiv']) if n.get('refdiv') else ''} = {fmt(calc)} だが freq は {fmt(v)}")
                    v = v or calc
            else:
                div = float(n.get("div", 1) or 1)
                v = pf / div if pf else None
                r = self.root.get(p) if p else None
                if n["_k"] == "dom":
                    dv = hz(n.get("freq"))
                    if v and dv and abs(v - dv) / dv > 0.01:
                        self.warn.append(f"[周波数] {self.lab(i)}: {self.lab(p)} {fmt(pf)}"
                                         f"{' ÷' + str(n['div']) if n.get('div') else ''} = {fmt(v)} だが freq は {fmt(dv)}")
                    v = v or dv          # 比の判定は計算値で(表示は書いた値)
            busy.discard(i)
            self.fq[i], self.root[i] = v, r
            return v
        for i in self.node:
            go(i)

    def check(self):
        for n in self.plls:
            if n.get("in") and n["in"] not in self.node:
                self.warn.append(f"[未定義] {self.lab(n['id'])}: in {n['in']} がない")
        for n in self.muxes:
            ins = n.get("in") or []
            for x in ins:
                if x not in self.node:
                    self.warn.append(f"[未定義] {self.lab(n['id'])}: 入力 {x} がない")
            if len(ins) < 2:
                self.warn.append(f"[MUX] {self.lab(n['id'])}: 入力が {len(ins)} 本(切替には 2 本以上)")
            if n.get("default") and n["default"] not in ins:
                self.warn.append(f"[MUX] {self.lab(n['id'])}: default {n['default']} が入力にない")
        for d in self.doms:
            if d.get("from") and d["from"] not in self.node:
                self.warn.append(f"[未定義] {self.lab(d['id'])}: from {d['from']} がない")
            if not d.get("from"):
                self.info.append(f"[info] {self.lab(d['id'])}: 源(from)が未決")
        self.calc()
        imp = {}
        for d in self.doms:
            p, dv = d.get("from"), hz(d.get("freq"))
            if p in self.node and dv and not hz(self.node[p].get("freq")) and self.node[p]["_k"] != "dom":
                imp.setdefault(p, []).append((dv * float(d.get("div", 1) or 1), d))
        for p, lst in imp.items():
            vs = {round(v / 1e3) for v, _ in lst}
            if len(vs) > 1:
                self.warn.append(f"[周波数] {self.lab(p)} の出力が決まらない: " + ", ".join(
                    f'{self.lab(d["id"])} {d["freq"]}{" ÷" + str(d["div"]) if d.get("div") else ""} → {fmt(v)}' for v, d in lst)
                    + "。PLL を分けるか div を書く")

    # ---------- 所属と乗り換え ----------
    def members(self):
        mem = {d["id"]: [] for d in self.doms}
        for b in self.c.blocks.values():
            if b.get("_ext") or b.get("virtual") or b.get("_hidden"):
                continue
            d = b.get("clock_domain")
            par = b.get("_att_of") or b.get("_parent")
            if par and (not d or d == self.c.blocks[par].get("clock_domain")):
                continue
            lab = b.get("label", b["id"])
            if par:
                lab = f'{self.c.blocks[par].get("label", par)} / {lab}'
            if d in mem:
                mem[d].append(lab)
            elif d:
                self.warn.append(f"[未定義] {lab}: clock_domain {d} が clocks.domains にない")
            elif not b.get("clock") and not par and not any(p.get("clock_domain") or p.get("clock") for p in b.get("ports", [])):
                self.info.append(f"[info] {lab}: clock_domain がない")
        for bus in self.c.buses.values():
            d = bus.get("clock_domain")
            if d in mem:
                mem[d].append("⇄ " + bus.get("label", bus["id"]))
            elif d:
                self.warn.append(f"[未定義] バス {bus.get('label', bus['id'])}: clock_domain {d} がない")
        return mem

    def chain(self, i):
        """ドメインから上へ: [(ノード, ここまでの分周の積)]。MUX・PLL で止まる(その先は別の源になりうる)"""
        out, n, k, seen = [], self.node.get(i), 1.0, set()
        while n is not None and n["id"] not in seen:
            seen.add(n["id"])
            out.append((n["id"], k))
            if n["_k"] != "dom":
                break
            k *= float(n.get("div", 1) or 1)
            n = self.node.get(n.get("from"))
        return out

    def kind(self, a, b):
        """共通の祖先から分周だけで作られていて、比が整数 → sync(比率同期)。それ以外 → async"""
        if a not in self.node or b not in self.node:
            return "async", None
        ca, cb = dict(self.chain(a)), dict(self.chain(b))
        com = [x for x, _ in self.chain(a) if x in cb]
        if not com:
            return "async", None
        x = com[0]
        ka, kb = ca[x], cb[x]
        fa, fb = hz(self.node[a].get("freq")), hz(self.node[b].get("freq"))
        if fa and fb and abs((fa / fb) - (kb / ka)) / (kb / ka) > 0.01:
            return "async", None                       # 書いた周波数が分周比と合わない: 同期とは言えない
        r = max(ka, kb) / min(ka, kb)
        if abs(r - round(r)) < 1e-6:
            return "sync", f"1:{round(r)}"
        return "async", None

    def crossings(self):
        c, pairs = self.c, {}
        def dom(o):
            return o.get("clock_domain") if o else None
        for bus in c.buses.values():
            for a in bus.get("attach", []):
                opt = c.attach_opt.get((bus["id"], a), {})
                da, db = dom(c.owner_clock(a)), dom(bus)
                if opt.get("cdc") is False or not da or not db or da == db:
                    continue
                bid = c.anch[a][3] if a in c.anch else a.split('.')[0]
                bl = c.blocks.get(bid, {}).get("label", bid)
                pairs.setdefault(tuple(sorted((da, db))), []).append(f"{bl}↔{bus.get('label', bus['id'])}")
        for ln in c.s.get("links", []):
            if ln["from"] not in c.anch or ln["to"] not in c.anch or ln.get("cdc") is False:
                continue
            A, B = c.anch[ln["from"]], c.anch[ln["to"]]
            if A[3] is None or B[3] is None:
                continue
            da, db = dom(c.owner_clock(ln["from"])), dom(c.owner_clock(ln["to"]))
            if da and db and da != db:
                pairs.setdefault(tuple(sorted((da, db))), []).append(
                    f'{c.blocks.get(A[3], {}).get("label", A[3])}→{c.blocks.get(B[3], {}).get("label", B[3])}')
        out = []
        for (a, b), via in sorted(pairs.items()):
            via = list(dict.fromkeys(via))
            k, r = self.kind(a, b)
            out.append({"a": a, "b": b, "via": via, "kind": k, "ratio": r})
        return out

    # ---------- 描画 ----------
    def svg(self, title="クロック系統図"):
        mem = self.members()
        self.cross = cr = self.crossings()
        doms, plls, muxes, srcs = self.doms, self.plls, self.muxes, self.srcs
        # 描く順: 源から木をたどる順(分周で作るドメインは親のすぐ下)
        order, seen = [], set()
        def dfs(i):
            for d in self.doms:
                if d.get("from") == i and d["id"] not in seen:
                    seen.add(d["id"]); order.append(d); dfs(d["id"])
            for m in muxes:
                if (m.get("default") or (m.get("in") or [None])[0]) == i:
                    dfs(m["id"])
            for p in plls:
                if p.get("in") == i:
                    dfs(p["id"])
        for x in srcs:
            dfs(x["id"])
        for d in self.doms:
            if d["id"] not in seen:
                order.append(d); seen.add(d["id"]); dfs(d["id"])
        doms = order
        o, ow = [], []
        SUBK = {"osc": "水晶/発振器", "pad": "外部クロック入力", "rc": "内部 RC 発振"}
        W_SRC = max([140] + [max(wlen(s.get("label", s["id"])) * 7.2, wlen(f'{s.get("freq", "")} {SUBK.get(s.get("kind", "osc"), "")}') * 6.2) + 24
                             for s in srcs])
        X_EXT = 24
        chip_x = X_EXT + W_SRC + 34
        X_INT = chip_x + 18
        X_SL = X_INT + W_SRC + 16                      # 源の幹線
        ns = len(srcs)
        X_PLL = X_SL + ns * 12 + 16
        W_PLL = max([150] + [wlen(p.get("label", p["id"])) * 7.2 + 24 for p in plls]) if plls else 0
        X_PT = X_PLL + W_PLL + 16                      # PLL の幹線
        np_ = len(plls)
        X_MUX = X_PT + np_ * 12 + 22 if muxes else X_PT + np_ * 12
        W_MUX = 44 if muxes else 0
        X_DIV = X_MUX + W_MUX + (58 if muxes else 10)
        W_DIV = 46
        X_DT = X_DIV + W_DIV + 14                      # ドメインからの分岐の列
        nd_par = len({d["from"] for d in doms if self.node.get(d.get("from"), {}).get("_k") == "dom"})
        X_CG = X_DT + nd_par * 10 + 14
        W_CG = 62
        X_DOM = X_CG + W_CG + 92
        W_DOM = 380

        def chips(names, width):
            rows, x, cur = [], 0, []
            for m in names:
                w = min(wlen(m) * 6.6 + 20, width)
                if cur and x + w > width:
                    rows.append(cur); cur, x = [], 0
                cur.append((m, x, w)); x += w + 6
            if cur:
                rows.append(cur)
            return rows
        y = 96
        pos, crow = {}, {}
        for d in doms:
            crow[d["id"]] = chips(mem[d["id"]], W_DOM - 24)
            h = max(46 + max(1, len(crow[d["id"]])) * 24 + (14 if d.get("dvfs") else 0), 64)
            pos[d["id"]] = [y, h]
            y += h + 12
        bottom = y
        def cy(i):
            if i in pos:
                return pos[i][0] + pos[i][1] / 2
            if i in npos:
                return npos[i][0] + npos[i][1] / 2
            return None
        # MUX, PLL, 源の縦位置: 下流の平均に寄せて詰める
        npos = {}
        def place(items, H, kids):
            want = {}
            for n in items:
                ys = [cy(k) for k in kids(n["id"]) if cy(k) is not None] or [bottom]
                want[n["id"]] = sum(ys) / len(ys) - H(n) / 2
            last = 70
            for n in sorted(items, key=lambda n: want[n["id"]]):
                yy = max(want[n["id"]], last)
                npos[n["id"]] = [yy, H(n)]
                last = yy + H(n) + 12
        def users(i):
            return [d["id"] for d in doms if d.get("from") == i] + \
                   [m["id"] for m in muxes if i in (m.get("in") or [])] + [p["id"] for p in plls if p.get("in") == i]
        place(muxes, lambda m: max(36, len(m.get("in", [])) * 16 + 12), users)
        place(plls, lambda p: 56, users)
        place(srcs, lambda s: 50, users)
        bottom = max([bottom] + [v[0] + v[1] + 14 for v in npos.values()])
        lanes = len(cr)
        xr = X_DOM + W_DOM
        chip_r = xr + 26 + lanes * 44 + 20
        W, H = chip_r + 24, bottom + 64
        o.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" font-family="\'Noto Sans JP\',sans-serif">'
                 f'<rect width="100%" height="100%" fill="#fff"/><defs><marker id="cka" viewBox="0 0 10 10" refX="9" refY="5" '
                 f'markerWidth="7" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="{INK}"/>'
                 f'</marker></defs><text x="12" y="28" font-size="17" font-weight="800" fill="{INK}">{esc(title)}</text>'
                 f'<rect x="{f(chip_x)}" y="50" width="{f(chip_r - chip_x)}" height="{f(bottom - 50)}" rx="6" fill="#fcfcfd" '
                 f'stroke="{INK}" stroke-width="2.2"/><text x="{f(chip_x + 10)}" y="68" font-size="13" font-weight="800" fill="{INK}">'
                 f'{esc(self.c.s.get("chip", {}).get("label", "chip"))}</text><!--LINES-->')
        # 入口の座標(各ノードの左側の入力点)
        def entry(i, k=0, n_in=1):
            n = self.node[i]
            if n["_k"] == "dom":
                return X_DIV, cy(i)
            if n["_k"] == "mux":
                yy, hh = npos[i]
                return X_MUX + 6, yy + hh / 2 + (k - (n_in - 1) / 2) * 16
            return X_PLL, cy(i)
        def wire(x1, y1, x2, y2, tx, dot=True, dash=False, col=INK):
            d_ = ' stroke-dasharray="5 3"' if dash else ""
            ow.append(f'<path d="M{f(tx)},{f(y2)} L{f(x2)},{f(y2)}" stroke="{col}" stroke-width="1.5" fill="none"{d_}/>')
            if dot:
                ow.append(f'<circle cx="{f(tx)}" cy="{f(y2)}" r="2.6" fill="{col}"/>')
        # 源・PLL の出力(幹線)
        def trunk(i, x_out, y_out, tx):
            tg = []
            for d in doms:
                if d.get("from") == i:
                    tg.append((d["id"], 0, 1, False))
            for m in muxes:
                ins = m.get("in") or []
                if i in ins:
                    tg.append((m["id"], ins.index(i), len(ins), i != (m.get("default") or ins[0])))
            for p in plls:
                if p.get("in") == i:
                    tg.append((p["id"], 0, 1, False))
            ys = [y_out]
            for t, k, n_in, dash in tg:
                ex, ey = entry(t, k, n_in)
                ys.append(ey)
                wire(tx, y_out, ex, ey, tx, dot=len(tg) > 1, dash=dash)
                ow.append(f'<path d="M{f(ex - 9)},{f(ey - 4.5)} L{f(ex)},{f(ey)} L{f(ex - 9)},{f(ey + 4.5)}" fill="{INK}"/>')
            if tg:
                ow.append(f'<path d="M{f(x_out)},{f(y_out)} L{f(tx)},{f(y_out)} M{f(tx)},{f(min(ys))} L{f(tx)},{f(max(ys))}" '
                          f'stroke="{INK}" stroke-width="1.5" fill="none"/>')
        for k, s in enumerate(srcs):
            yy, hh = npos[s["id"]]
            ext = s.get("kind", "osc") in ("osc", "pad")
            x = X_EXT if ext else X_INT
            dash = ' stroke-dasharray="5 4"' if ext else ""
            sub = {"osc": "水晶/発振器", "pad": "外部クロック入力", "rc": "内部 RC 発振"}.get(s.get("kind", "osc"), "")
            o.append(f'<rect x="{f(x)}" y="{f(yy)}" width="{f(W_SRC)}" height="{hh}" rx="4" fill="#f8fafc" stroke="#64748b" '
                     f'stroke-width="1.5"{dash}/><text x="{f(x + W_SRC / 2)}" y="{f(yy + 19)}" font-size="12.5" font-weight="700" '
                     f'fill="{INK}" text-anchor="middle">{esc(s.get("label", s["id"]))}</text><text x="{f(x + W_SRC / 2)}" '
                     f'y="{f(yy + 36)}" font-size="11" fill="{GRAY}" text-anchor="middle"><tspan font-weight="700" fill="#0369a1">'
                     f'{esc(s.get("freq", ""))}</tspan> {esc(sub)}</text>')
            if ext:
                o.append(f'<rect x="{f(chip_x - 8)}" y="{f(yy + hh / 2 - 8)}" width="16" height="16" fill="#cbd5e1" stroke="{INK}" stroke-width="1.5"/>')
            trunk(s["id"], x + W_SRC, yy + hh / 2, X_SL + k * 12)
        for k, p in enumerate(plls):
            yy, hh = npos[p["id"]]
            sub = f'{fmt(self.fq.get(p["id"]))}' if self.fq.get(p["id"]) else esc(p.get("freq", ""))
            mul = f' ×{p["mult"]}' + (f'÷{p["refdiv"]}' if p.get("refdiv") else "") if p.get("mult") else ""
            o.append(f'<rect x="{f(X_PLL)}" y="{f(yy)}" width="{f(W_PLL)}" height="{hh}" rx="4" fill="#fff" stroke="{INK}" '
                     f'stroke-width="1.6"/><text x="{f(X_PLL + W_PLL / 2)}" y="{f(yy + 22)}" font-size="12.5" font-weight="700" '
                     f'fill="{INK}" text-anchor="middle">{esc(p.get("label", p["id"]))}</text><text x="{f(X_PLL + W_PLL / 2)}" '
                     f'y="{f(yy + 40)}" font-size="11.5" font-weight="700" fill="#0369a1" text-anchor="middle">{esc(p.get("freq", sub))}'
                     f'{esc(mul)}{"(仮)" if p.get("assumed") else ""}</text>')
            trunk(p["id"], X_PLL + W_PLL, yy + hh / 2, X_PT + k * 12)
        for m in muxes:
            yy, hh = npos[m["id"]]
            o.append(f'<path d="M{f(X_MUX)},{f(yy)} L{f(X_MUX + W_MUX)},{f(yy + 10)} L{f(X_MUX + W_MUX)},{f(yy + hh - 10)} '
                     f'L{f(X_MUX)},{f(yy + hh)} Z" fill="#fff" stroke="{INK}" stroke-width="1.6"/>'
                     f'<text x="{f(X_MUX + W_MUX / 2)}" y="{f(yy - 16)}" font-size="11" font-weight="700" fill="{INK}" '
                     f'text-anchor="middle">{esc(m.get("label", m["id"]))}</text>'
                     f'<text x="{f(X_MUX + W_MUX / 2)}" y="{f(yy - 4)}" font-size="10" fill="{GRAY}" text-anchor="middle">'
                     f'選択: {esc(str(m.get("sel", "未決")))}</text>')
            trunk(m["id"], X_MUX + W_MUX, yy + hh / 2, X_MUX + W_MUX + 16)
        # ドメインから分周するドメイン
        dpar = sorted({d["from"] for d in doms if self.node.get(d.get("from"), {}).get("_k") == "dom"},
                      key=lambda i: cy(i))
        for k, pid in enumerate(dpar):
            tx = X_DT + k * 10
            ys = [cy(pid)]
            for d in doms:
                if d.get("from") == pid:
                    ys.append(cy(d["id"]))
                    ow.append(f'<path d="M{f(tx)},{f(cy(d["id"]))} L{f(X_DIV)},{f(cy(d["id"]))}" stroke="{INK}" stroke-width="1.5" fill="none"/>'
                              f'<path d="M{f(X_DIV - 9)},{f(cy(d["id"]) - 4.5)} L{f(X_DIV)},{f(cy(d["id"]))} L{f(X_DIV - 9)},{f(cy(d["id"]) + 4.5)}" fill="{INK}"/>')
            ow.append(f'<path d="M{f(tx)},{f(min(ys))} L{f(tx)},{f(max(ys))}" stroke="{INK}" stroke-width="1.5" fill="none"/>'
                      f'<circle cx="{f(tx)}" cy="{f(cy(pid))}" r="2.8" fill="{INK}"/>')
        # ドメインの行: [÷n] → [CG] → ドメイン
        for d in doms:
            i = d["id"]
            yy, hh = pos[i]
            c_ = cy(i)
            col = d["_col"]
            if not d.get("from"):
                o.append(f'<text x="{f(X_DIV - 8)}" y="{f(c_ + 4)}" font-size="10.5" font-weight="700" fill="#b45309" '
                         f'text-anchor="end">源 未決</text>')
            x = X_DIV
            if d.get("div") and float(d["div"]) != 1:
                o.append(f'<rect x="{f(X_DIV)}" y="{f(c_ - 13)}" width="{W_DIV}" height="26" rx="3" fill="#fff" stroke="{INK}" stroke-width="1.4"/>'
                         f'<text x="{f(X_DIV + W_DIV / 2)}" y="{f(c_ + 4.5)}" font-size="12" font-weight="800" fill="{INK}" '
                         f'text-anchor="middle">÷{d["div"]}</text>')
                x = X_DIV + W_DIV
            g = d.get("gate")
            if g:
                gl = GATE.get(g, f"CG({g})")
                ow.append(f'<path d="M{f(x)},{f(c_)} L{f(X_CG)},{f(c_)}" stroke="{col}" stroke-width="1.8" fill="none"/>')
                o.append(f'<rect x="{f(X_CG)}" y="{f(c_ - 13)}" width="{W_CG}" height="26" rx="3" fill="#fff" stroke="{col}" stroke-width="1.6"/>'
                         f'<text x="{f(X_CG + W_CG / 2)}" y="{f(c_ + 4.5)}" font-size="11" font-weight="800" fill="{col}" '
                         f'text-anchor="middle">{esc(gl)}</text>')
                x = X_CG + W_CG
            ow.append(f'<path d="M{f(x)},{f(c_)} L{f(X_DOM)},{f(c_)}" stroke="{col}" stroke-width="2" fill="none" marker-end="url(#cka)"/>')
            fq = self.fq.get(i)
            o.append(f'<text x="{f(X_DOM - 8)}" y="{f(c_ - 6)}" font-size="11.5" font-weight="800" fill="#0369a1" text-anchor="end">'
                     f'{esc(d.get("freq") or fmt(fq))}{"(仮)" if d.get("assumed") else ""}</text>')
            o.append(f'<rect x="{f(X_DOM)}" y="{f(yy)}" width="{W_DOM}" height="{f(hh)}" rx="6" fill="{col}" fill-opacity=".09" '
                     f'stroke="{col}" stroke-width="2"/><text x="{f(X_DOM + 12)}" y="{f(yy + 20)}" font-size="13" font-weight="800" '
                     f'fill="{col}">{esc(d.get("label", i))}</text><text x="{f(X_DOM + W_DOM - 10)}" y="{f(yy + 20)}" font-size="10.5" '
                     f'fill="{GRAY}" text-anchor="end">{esc(self.path(i))}</text>')
            for r, row in enumerate(crow[i]):
                for m, ox, w in row:
                    cx_, cy_ = X_DOM + 12 + ox, yy + 30 + r * 24
                    o.append(f'<rect x="{f(cx_)}" y="{f(cy_)}" width="{f(w)}" height="19" rx="9.5" fill="#fff" stroke="{col}" stroke-width="1"/>'
                             f'<text x="{f(cx_ + w / 2)}" y="{f(cy_ + 13.5)}" font-size="10.5" fill="{INK}" text-anchor="middle">{esc(m)}</text>')
            if not mem[i]:
                o.append(f'<text x="{f(X_DOM + 12)}" y="{f(yy + 44)}" font-size="11" fill="#94a3b8">所属ブロックなし</text>')
            if d.get("dvfs"):
                o.append(f'<text x="{f(X_DOM + 12)}" y="{f(yy + hh - 8)}" font-size="10.5" font-weight="700" fill="#7c3aed">'
                         f'DVFS: {esc(" / ".join(str(v) for v in d["dvfs"]))}</text>')
        # 乗り換え
        cr.sort(key=lambda r: abs(cy(r["a"]) - cy(r["b"])))
        for k, r in enumerate(cr):
            lx = xr + 26 + k * 44
            ya, yb = cy(r["a"]) + 12, cy(r["b"]) + 12
            colr = ASYNC_C if r["kind"] == "async" else SYNC_C
            ow.append(f'<path d="M{f(xr)},{f(ya)} L{f(lx)},{f(ya)} L{f(lx)},{f(yb)} L{f(xr)},{f(yb)}" fill="none" stroke="{colr}" '
                      f'stroke-width="1.6" stroke-dasharray="5 3"/>')
            tag = f'CDC×{len(r["via"])}' if r["kind"] == "async" else f'{r["ratio"]}×{len(r["via"])}'
            tw = wlen(tag) * 6.4 + 10
            my = (ya + yb) / 2
            o.append(f'<rect x="{f(lx - tw / 2)}" y="{f(my - 9)}" width="{f(tw)}" height="18" rx="2" fill="#fff" stroke="{colr}" stroke-width="1.4"/>'
                     f'<text x="{f(lx)}" y="{f(my + 4)}" font-size="10" font-weight="800" fill="{colr}" text-anchor="middle">{tag}</text>')
        o.append(f'<text x="12" y="{f(H - 16)}" font-size="11" fill="{GRAY}">÷n = 分周 / CG = クロックゲート(SW = ソフトウェア制御・自動 = アイドルで停止) / '
                 f'台形 = クロック切替(実線 = 通常の選択、破線 = 切替先) / 赤破線 CDC = 非同期の乗り換え(同期化が要る) / '
                 f'青破線 1:n = 同じ PLL から整数比の同期の乗り換え / 破線の源 = チップ外</text></svg>')
        return "".join(o).replace("<!--LINES-->", "".join(ow), 1)

    def path(self, i):
        """源までの道筋(例: XTAL → SYS PLL → ÷2)"""
        seq, n, seen = [], self.node.get(i), set()
        while n is not None and n["id"] not in seen:
            seen.add(n["id"])
            p = self.parent(n)
            if p is None:
                break
            n = self.node.get(p)
            if n is not None:
                seq.append(n.get("label", p))
        return " ← ".join(seq) if seq else ""

    def md(self):
        L = ["# クロック系統", "", "## クロックドメイン", "",
             "| ドメイン | 周波数 | 源までの道筋 | 分周 | ゲート | DVFS | 所属 |", "|---|---|---|---|---|---|---|"]
        mem = self.members()
        for d in self.doms:
            L.append(f'| {d.get("label", d["id"])} | {d.get("freq") or fmt(self.fq.get(d["id"]))}{"(仮)" if d.get("assumed") else ""} '
                     f'| {self.path(d["id"]) or "未決"} | {"÷" + str(d["div"]) if d.get("div") else ""} '
                     f'| {GATE.get(d.get("gate"), d.get("gate") or "")} | {" / ".join(map(str, d.get("dvfs", [])))} '
                     f'| {", ".join(mem[d["id"]])} |')
        L += ["", "## 乗り換え", "", "| ドメイン | ドメイン | 種類 | 経路 |", "|---|---|---|---|"]
        for r in self.cross:
            k = "非同期(CDC: 同期化/非同期 FIFO)" if r["kind"] == "async" else f'比率同期 {r["ratio"]}(同期の乗り換え)'
            L.append(f'| {self.lab(r["a"])} | {self.lab(r["b"])} | {k} | {", ".join(r["via"])} |')
        return "\n".join(L) + "\n"


def build(c, title="クロック系統図"):
    """互換: (svg, warn, table)。table = [(a, b, via)]。ct は build.last に入る"""
    if not (c.s.get("clocks", {}) or {}).get("domains"):
        return None, ["clocks.domains がない(クロック系統図は描けない)"], []
    ct = ClockTree(c)
    ct.check()
    svg = ct.svg(title)
    build.last = ct
    return svg, ct.warn, [(r["a"], r["b"], r["via"]) for r in ct.cross]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    svg, warn, _ = build(c)
    if svg is None:
        sys.exit(warn[0])
    ct = build.last
    if a.out.endswith(".svg"):
        p = a.out
        d = os.path.dirname(os.path.abspath(p))
    else:
        d = a.out
        p = os.path.join(d, "clock_tree.svg")
    os.makedirs(d, exist_ok=True)
    open(p, "w", encoding="utf-8").write(svg)
    open(os.path.join(d, "clock.md"), "w", encoding="utf-8").write(ct.md())
    if a.png:
        to_png(p)
    print(f"生成: {p} / clock.md")
    for r in ct.cross:
        k = "CDC" if r["kind"] == "async" else f'同期 {r["ratio"]}'
        print(f'  {ct.lab(r["a"])} ↔ {ct.lab(r["b"])}: {k} ({", ".join(r["via"])})')
    for w in ct.info:
        print("  " + w)
    print("lint:", "OK" if not ct.warn else "")
    for w in ct.warn:
        print("  " + w)


if __name__ == "__main__":
    main()
