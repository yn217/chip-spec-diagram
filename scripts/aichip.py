#!/usr/bin/env python3
"""aichip — AI 向けチップ(SoC ではない、演算タイル + メモリ + 高速 I/O、積層あり)のアーキテクチャ図

  python3 aichip.py render model.yaml -o out/ [--png]     # 全体図・層ごとの図・動作説明・切り出し・別図・arch.md を全部出す
  python3 aichip.py lint   model.yaml

1 つのモデル(model.yaml)から、同じ配置のまま次を作る。図を描き分けない(読む人が混乱しないように):
  arch_overview.svg        全体図(データの流れ重視)。積層は 1 枚に重ねる(上の層 = 色つきの枠 + 斜線 + 「L2」の札、層をまたぐ接続 = ⊠)
  arch_layer_<層>.svg      層ごとの図(物理配置を見る時)。ほかの層は薄い枠だけ
  arch_layer_<層>_design.svg  その層を回路面から見た向き(設計データの向き)。全体図と左右が反転する層にだけ出す
  arch_scene_<id>.svg      動作説明。全体図と同じ配置で、登場しない所を薄くし、登場する線に番号を振る。下に動作の表
  arch_scene_<id>_frames.svg  動作説明のコマ割り(frames: true の時)。同じ図を 1 ステップずつ並べる
  arch_crop_<id>.svg       切り出し。全体図の一部を拡大(どこを切り出したかの小さい地図つき)
  arch_focus_<種類>.svg    別図。全体図では描かない種類の線(test = BIST の接続とセレクタ、clk = クロック)を、同じ配置の上に描く
  arch_detail_<id>.svg     別図。ブロックの中の回路(階層の 1 段下)。動作説明・コマ割りも同じように出る(arch_detail_<id>_scene_… )
  arch.md                  ブロック・接続(ビット幅 × クロック = 帯域)・クロック・積層と向きの表

描き方の決まり(R55〜R60。くわしくは diagrams/ai-chip-architecture/GUIDE.md):
  ・マス目に敷き詰める。ブロックの間は配線用のすき間(32px)だけ
  ・データの流れに関係の薄いブロック(クロック・電源・温度・BIST)は名前だけの小さい箱。それだけの行は低くする
  ・線の太さ = ビット幅。ラベル = ビット幅×クロック、向き(→ 片方向 / ⇄各 両方向 / ⇄交互 半二重)と帯域、プロトコル
  ・SRAM は橙、FIFO は小さいアイコン。回路の図ではセレクタ = 台形、レジスタ = 三角の印、演算器 = 丸の中に記号
  ・文字どうしは重ねない(枠・線の上に乗るのは可)
  ・積層の座標は「上面から見た向き」。回路面が下の層は設計データと左右が反転して見える → 原点の印と F の字で示す

model.yaml の書き方は examples/aichip/infer_accel.yaml。
"""
import os, sys, re, math, fnmatch, argparse, collections, heapq
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import esc, wlen, INK, to_png, hz, load, FONT, text_box, fifo_cells, FIFO_COL, FIFO_FILL

GRAY = "#64748b"
CW, CH, CHS, G, PAD = 118, 78, 24, 32, 6     # マスの幅・高さ、小さい箱だけの行の高さ、すき間、外の余白
CHB, BUSH = 38, 32                           # バスの帯だけの行の高さ、帯の太さ
TABC = "#fbbf24"                             # ポートの札
BEND = 46
KFILL = {"compute": "#dbeafe", "mem": "#ffedd5", "io": "#dcfce7", "clk": "#fef9c3", "ctrl": "#ede9fe", "pwr": "#fee2e2",
         "reg": "#ccfbf1", "test": "#f1f5f9", "misc": "#f1f5f9"}
KHATCH = {"compute": "#3b82f6", "mem": "#f97316", "io": "#22c55e", "clk": "#ca8a04", "ctrl": "#8b5cf6", "pwr": "#ef4444",
          "reg": "#14b8a6", "test": "#94a3b8", "misc": "#94a3b8"}
KNAME = {"compute": "演算", "mem": "メモリ(SRAM)", "reg": "レジスタ・セレクタ", "io": "I/O", "ctrl": "制御", "clk": "クロック", "pwr": "電源", "test": "テスト"}
DIE_STYLE = [("#334155", "濃い灰"), ("#7c3aed", "紫"), ("#0f766e", "緑"), ("#be185d", "赤紫")]
LCOL = {"data": "#1e3a8a", "ctrl": "#64748b", "clk": "#b45309", "test": "#0e7490", "pwr": "#b91c1c"}
VIAC = "#7c2d12"
FOCUS_TITLE = {"test": "別図 — テスト・BIST の接続とセレクタ", "clk": "別図 — クロックの配り方", "pwr": "別図 — 電源の配り方"}
CLKPAL = ["#0369a1", "#b45309", "#15803d", "#be185d", "#6d28d9", "#0f766e", "#a16207"]
MEMC = "#ea580c"
ACC = "#d9480f"                           # 動作説明で登場する線・枠
DIMS, DIMF, DIMT = "#d9dfe7", "#fbfcfd", "#c4ccd6"
FS_NAME, FS_INFO, FS_LAB = 10.5, 7.5, 6.8
FLIPSIDE = {"E": "W", "W": "E", "N": "S", "S": "N"}
SYM = {"reg": (30, 44), "mux": (24, 40), "op": (34, 34), "fifo": (48, 24)}      # 回路の図の記号の大きさ(幅, 高さ)。reg・mux は縦に伸びる
PINDEF = {"reg": {"w": ["d"], "e": ["q"]}, "fifo": {"w": ["in"], "e": ["out"]}, "op": {"e": ["y"]}}


def diverge(P, Q):
    """同じ端子から出る 2 本の線が分かれる点(分岐の ● を打つ所)"""
    if abs(P[0][0] - Q[0][0]) + abs(P[0][1] - Q[0][1]) > 0.6:
        return None
    i = 0
    while i + 1 < len(P) and i + 1 < len(Q):
        S = P[i]
        dp, dq = (P[i + 1][0] - S[0], P[i + 1][1] - S[1]), (Q[i + 1][0] - S[0], Q[i + 1][1] - S[1])
        lp, lq = abs(dp[0]) + abs(dp[1]), abs(dq[0]) + abs(dq[1])
        if lp < 0.3 or lq < 0.3:
            return None
        if not (dp[0] * dq[0] + dp[1] * dq[1] > 0 and abs(dp[0] * dq[1] - dp[1] * dq[0]) < 0.5):
            return S if i > 0 else None
        if abs(lp - lq) < 0.6:
            i += 1
            continue
        t = min(lp, lq)
        return (S[0] + dp[0] / lp * t, S[1] + dp[1] / lp * t)
    return None


def f(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def bps(v):
    m = re.match(r"\s*([\d.]+)\s*([kKmMgGtT]?)b(?:ps|/s)", str(v))
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[m.group(2).lower()] if m else None


def bytes_s(v):
    m = re.match(r"\s*([\d.]+)\s*([kKmMgGtT]?)B/s", str(v))
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[m.group(2).lower()] if m else None


def fmt_bytes(b):
    gb = b / 1e9
    if gb >= 100:
        return f"{gb:.0f}GB/s"
    if gb >= 1:
        return f"{gb:.3g}GB/s"
    if gb >= 1e-3:
        return f"{gb * 1e3:.3g}MB/s"
    return f"{gb * 1e6:.3g}kB/s"


def short_hz(v):
    return f"{v / 1e9:g}GHz" if v >= 1e9 else f"{v / 1e6:g}MHz" if v >= 1e6 else f"{v / 1e3:g}kHz"


def wrap(t, maxu):
    out, cur = [], ""
    for ch in str(t):
        if wlen(cur + ch) > maxu and cur:
            out.append(cur); cur = ""
        cur += ch
    return out + [cur] if cur else out


def tw(t, fs, bold=False):
    return wlen(t) * fs * (0.6 if bold else 0.56)


def inter(a, b):
    ix, iy = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    return ix * iy if ix > 0.8 and iy > 0.8 else 0


def along(pts, fr):
    """折れ線の、全長の fr の所の点"""
    tot = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
    d = tot * fr
    for a, b in zip(pts, pts[1:]):
        ln = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if d <= ln and ln > 0:
            return (a[0] + (b[0] - a[0]) * d / ln, a[1] + (b[1] - a[1]) * d / ln)
        d -= ln
    return pts[-1]


# ======================= モデル =======================
class Arch:
    def __init__(self, spec):
        self.s, self.lint = spec, []
        self.C, self.R = spec["grid"]
        self.schem = bool(spec.get("schematic"))                       # 回路のつながりの図(物理配置ではない)
        self.focus = list(spec.get("focus", ["data", "ctrl"]))        # 全体図に描く線の種類
        self.small_kinds = set(spec.get("small_kinds", ["clk", "pwr", "test"]))
        self.dies = []
        for k, d in enumerate(spec["stack"]):
            d = dict(d, idx=k)
            d.setdefault("face", "up"); d.setdefault("flip", "y"); d.setdefault("at", [0, 0, self.C, self.R])
            self.dies.append(d)
        self.die = {d["id"]: d for d in self.dies}
        self.clk = collections.OrderedDict((c["id"], dict(c, hz=hz(c.get("freq")), col=CLKPAL[k % len(CLKPAL)]))
                                           for k, c in enumerate(spec.get("clocks", [])))
        self.blocks = collections.OrderedDict()
        for b in spec["blocks"]:
            nx, ny = b.get("array", [1, 1])
            c, r, w, h = b["at"]
            px, py = b.get("pitch", [w, h])
            for iy in range(ny):
                for ix in range(nx):
                    i = iy * nx + ix
                    sub = lambda v: v.replace("{i}", str(i)) if isinstance(v, str) else v
                    bb = {k: sub(v) for k, v in b.items()}
                    bb["at"] = [c + ix * px, r + iy * py, w, h]
                    bb["info"] = [sub(x) for x in b.get("info", [])]
                    bb.setdefault("kind", "misc"); bb.setdefault("name", bb["id"])
                    bb["pins"] = self.pins_of(bb)
                    if bb["id"] in self.blocks:
                        self.lint.append(f"[配置] ブロック {bb['id']} が 2 回ある")
                    self.blocks[bb["id"]] = bb
        self.links = collections.OrderedDict()
        for l in spec.get("links", []):
            rng = l.get("for")
            rng = [None] if rng is None else (list(range(rng)) if isinstance(rng, int) else list(rng))
            for i in rng:
                sub = lambda v: v.replace("{i}", str(i)) if isinstance(v, str) and i is not None else v
                ll = {k: sub(v) for k, v in l.items() if k != "for"}
                ll.setdefault("id", f"{ll['from']}>{ll['to']}"); ll.setdefault("kind", "data")
                for key, pk in (("from", "fpin"), ("to", "tpin")):       # ブロック.端子
                    v = str(ll[key])
                    if v not in self.blocks and "." in v and v.rsplit(".", 1)[0] in self.blocks:
                        ll[key], ll[pk] = v.rsplit(".", 1)
                if ll.get("fpin") and not ll.get("net"):                  # 同じ出力端子から出る線は 1 本の配線(分岐する)
                    ll["net"] = f"{ll['from']}.{ll['fpin']}"
                self.links[ll["id"]] = ll
        self.detail_of = {}
        for dt in spec.get("details", []):
            for pat in dt.get("of", []):
                for bid in self.blocks:
                    if fnmatch.fnmatchcase(bid, pat):
                        self.detail_of[bid] = dt["id"]
        self.check()

    def small(self, b):
        """名前だけの小さい箱にするか(データの流れに関係の薄いブロック)"""
        return not self.schem and b.get("small", b["kind"] in self.small_kinds)

    def pins_of(self, b):
        """端子: {辺: [名前, …]}。回路の図では、形ごとの既定の端子を足す(reg: d→q、fifo: in→out、op・mux: 出力 y)"""
        p = {str(k).lower(): [str(x) for x in v] for k, v in (b.get("pins") or {}).items()}
        if self.schem:
            dflt = dict(PINDEF.get(b.get("shape"), {}))
            if b.get("shape") == "mux":
                dflt = {str(b.get("out", "e")).lower(): ["y"]}
            names = {n for v in p.values() for n in v}
            for sd, v in dflt.items():
                for n in v:
                    if n not in names:
                        p.setdefault(sd, []).append(n)
        return p

    def bname(self, k):
        return self.blocks[k]["name"] if k in self.blocks else str(k)

    def bw(self, l):
        """→ (帯域 バイト/秒 or None, 1 行目 = 幅×クロック, 2 行目 = 向き + 帯域 + プロトコル)"""
        b, l1 = None, ""
        if l.get("lanes"):
            r = bps(l.get("rate"))
            if r:
                b = l["lanes"] * r / 8
                l1 = f"{l['lanes']}L×{str(l['rate']).replace('bps', 'b/s')}"
        elif l.get("width"):
            fq = self.clk[l["clock"]]["hz"] if l.get("clock") in self.clk else hz(l.get("rate"))
            if fq:
                e = l.get("eff", 1)
                b = l["width"] * fq / 8 * e
                l1 = f"{l['width']}b×{short_hz(fq)}" + (f"×{e:g}" if e != 1 else "")
            else:
                l1 = f"{l['width']}b"
        if l.get("label"):
            l1 = (l1 + " " if l1 else "") + str(l["label"])
        d = {"both": "⇄各", "half": "⇄交互"}.get(l.get("dir"), "→")       # 片方向 / 両方向(それぞれの向きにこの帯域)/ 半二重
        l2 = " ".join(x for x in [(d + fmt_bytes(b)) if b else "", str(l.get("proto", ""))] if x)
        return b, l1, l2

    def lw(self, l):
        """線の太さ = ビット幅(直列はレーン数 × 32 ビット相当)"""
        if l["kind"] in ("clk", "pwr"):
            return 0.9
        if self.schem:                                    # 回路の図: 細めの線。太さの差は控えめに
            return min(3.6, max(0.9, 0.8 + 0.35 * math.log2(max(l.get("width") or 1, 8) / 8)))
        w = l.get("width") or (l.get("lanes", 0) * 32) or 32
        return min(10, max(1.2, 1.2 + 1.1 * math.log2(max(w, 32) / 32)))

    def check(self):
        L, C, R = self.lint, self.C, self.R
        own = collections.defaultdict(dict)
        for b in self.blocks.values():
            d = self.die.get(b.get("die"))
            if not d:
                L.append(f"[配置] {b['id']}: 層 {b.get('die')} が stack にない"); continue
            c, r, w, h = b["at"]
            dc, dr, dw, dh = d["at"]
            if c < dc or r < dr or c + w > dc + dw or r + h > dr + dh:
                L.append(f"[配置] {b['id']} が層 {d['id']} の範囲 {d['at']} からはみ出している")
            for x in range(c, c + w):
                for y in range(r, r + h):
                    if (x, y) in own[d["id"]]:
                        L.append(f"[配置] {b['id']} と {own[d['id']][(x, y)]} が同じ層の同じマス ({x},{y}) にある")
                    own[d["id"]][(x, y)] = b["id"]
            if b.get("clock") is None and not self.small(b) and not self.schem:
                L.append(f"[情報] {b['id']}: クロックがない(クロックで動かないブロックは clock: none と書く)")
            elif b.get("clock") not in (None, "none") and b["clock"] not in self.clk:
                L.append(f"[情報] {b['id']}: クロック {b['clock']} が clocks にない")
        self.own = own
        for d in self.dies:
            dc, dr, dw, dh = d["at"]
            free = {(x, y) for c, r, w, h in d.get("free", []) for x in range(c, c + w) for y in range(r, r + h)}
            emp = [(x, y) for y in range(dr, dr + dh) for x in range(dc, dc + dw) if (x, y) not in own[d["id"]] and (x, y) not in free]
            if emp and not self.schem:
                L.append(f"[空き] 層 {d['id']}: {dw * dh} マス中 {len(emp)} マスが空いている "
                         f"({', '.join(f'[{x},{y}]' for x, y in emp[:6])}{' …' if len(emp) > 6 else ''})。詰めるか、何があるかを書く")
        for l in self.links.values():
            a, b = self.blocks.get(l["from"]), self.blocks.get(l["to"])
            if not a or not b:
                L.append(f"[接続] {l['id']}: {l['from'] if not a else l['to']} というブロックがない"); l["bad"] = True; continue
            cross = a["die"] != b["die"]
            if cross and not l.get("via"):
                L.append(f"[接続] {l['id']}: 層をまたぐ接続に via(HB・TSV・µバンプ など)がない"); l["via"] = "?"
            if l.get("via") and not cross:
                L.append(f"[接続] {l['id']}: 同じ層の中の接続に via がある")
            if cross and not l.get("far"):
                (ac, ar, aw, ah), (bc, br, bw_, bh) = a["at"], b["at"]
                if min(ac + aw, bc + bw_) <= max(ac, bc) or min(ar + ah, br + bh) <= max(ar, br):
                    L.append(f"[接続] {l['id']}: {a['id']} と {b['id']} は上下に重なっていない(真上・真下でつなげない)。"
                             f"途中を引き回すなら far: true(印を両方に置く)か far: line(線で結ぶ)と書く")
                    l["far"] = True
            for pk, bb in (("fpin", a), ("tpin", b)):
                if l.get(pk) and not any(l[pk] in v for v in bb["pins"].values()):
                    L.append(f"[接続] {l['id']}: {bb['id']} に端子 {l[pk]} がない(pins に書く)"); l.pop(pk)
            by, l1, l2 = self.bw(l)
            if self.schem and l["kind"] not in ("clk", "pwr") and not l.get("width"):
                L.append(f"[情報] {l['id']}({l['from']} → {l['to']}): ビット幅がない")
            if l["kind"] not in ("clk", "pwr") and not by and not self.schem:
                L.append(f"[情報] {l['id']}({l['from']} → {l['to']}): ビット幅とクロック(または レーン数と速度)がなく、帯域がわからない")
            if l.get("bw") and by and abs(bytes_s(l["bw"]) - by) / by > 0.02:
                L.append(f"[帯域] {l['id']}: 書いてある帯域 {l['bw']} と、幅 × クロックからの計算 {fmt_bytes(by)} が合わない")
        ids = set(self.links)
        for sc in self.s.get("scenes", []):
            for st in sc.get("steps", []):
                for x in st.get("links", []):
                    if x not in ids:
                        L.append(f"[動作] {sc['id']}: 接続 {x} がない")
                    elif self.links[x]["kind"] not in self.focus:
                        L.append(f"[動作] {sc['id']}: 接続 {x}(種類 {self.links[x]['kind']})は全体図に描かない種類。別図(focus)で説明する")
                for x in (st.get("state") or {}):
                    if x not in self.blocks:
                        L.append(f"[動作] {sc['id']}: state の {x} というブロックがない")
        for cr in self.s.get("crops", []):
            for x in cr.get("blocks", []):
                if x not in self.blocks:
                    L.append(f"[切り出し] {cr['id']}: ブロック {x} がない")
        self.groups = []                                  # サブシステムの枠: (名前, 色, [ブロック])
        for g in self.s.get("groups", []):
            ms = [bid for bid in self.blocks for pat in g.get("blocks", []) if fnmatch.fnmatchcase(bid, pat)]
            if not ms:
                L.append(f"[配置] グループ {g.get('name')}: 当てはまるブロックがない")
            else:
                self.groups.append((g.get("name", ""), g.get("color", "#64748b"), ms))

    def isbus(self, b):
        return b.get("shape") == "bus" and not self.schem

    def state(self, d, fx=False, fy=False):
        """層 d が、モデルの座標(上面から見た向き)を (fx, fy) だけ反転した図でどう見えるか → (左右反転, 上下反転)"""
        mx, my = False, False
        if d["face"] == "down":
            mx, my = (True, False) if d["flip"] == "y" else (False, True)
        return mx ^ fx, my ^ fy

    def base(self):
        """全体図の反転: 下面から見る図は、上面から見た向きを左右に反転する"""
        return (self.s.get("view", "top") == "bottom" and not self.schem, False)


# ======================= 配置・配線(図の種類によらず 1 回だけ決める)=======================
class Layout:
    def __init__(self, A, fx=False, fy=False):
        self.A, self.fx, self.fy = A, fx, fy
        C, R = A.C, A.R
        self.cells = {b["id"]: self.flip(b["at"]) for b in A.blocks.values()}
        rowsmall, has, rowbus = [True] * R, [False] * R, [False] * R   # 小さい箱だけ・バスの帯だけの行は低くする
        for b in A.blocks.values():
            c, r, w, h = self.cells[b["id"]]
            for y in range(r, r + h):
                has[y] = True
                if not (A.small(b) or (A.isbus(b) and w >= h)) or h > 1:
                    rowsmall[y] = False
                rowbus[y] = rowbus[y] or A.isbus(b)
        self.rh = [(CHB if rowbus[j] else CHS) if (rowsmall[j] and has[j]) else CH for j in range(R)]
        self.cw = [CW] * C
        if A.schem:                                       # 回路の図: 列の幅・行の高さは、そこに置く記号と文字で決める
            self.cw, self.rh = [40] * C, [40] * R
            sz = {b["id"]: self.sym_size(b) for b in A.blocks.values()}
            for b in A.blocks.values():
                c, r, w, h = self.cells[b["id"]]
                if w == 1:
                    self.cw[c] = max(self.cw[c], sz[b["id"]][0])
                if h == 1:
                    self.rh[r] = max(self.rh[r], sz[b["id"]][1])
            for b in A.blocks.values():
                c, r, w, h = self.cells[b["id"]]
                need = sz[b["id"]][0] - sum(self.cw[c:c + w]) - G * (w - 1)
                for i in range(c, c + w):
                    self.cw[i] += max(need, 0) / w
                need = sz[b["id"]][1] - sum(self.rh[r:r + h]) - G * (h - 1)
                for j in range(r, r + h):
                    self.rh[j] += max(need, 0) / h
        self.X0 = self.Y0 = PAD + G
        self.cx = [self.X0 + sum(self.cw[:i]) + i * G for i in range(C)]
        self.ry = [self.Y0 + sum(self.rh[:j]) + j * G for j in range(R)]
        self.W = self.cx[-1] + self.cw[-1] + G + PAD
        self.H = self.ry[-1] + self.rh[-1] + G + PAD
        self.xg = [self.cx[i] - G / 2 for i in range(C)] + [self.cx[-1] + self.cw[-1] + G / 2]
        self.yg = [self.ry[j] - G / 2 for j in range(R)] + [self.ry[-1] + self.rh[-1] + G / 2]
        self.rect, self.drect, self.own = {}, {}, collections.defaultdict(dict)
        for b in A.blocks.values():
            c, r, w, h = self.cells[b["id"]]
            self.rect[b["id"]] = self.px(c, r, w, h)
            for x in range(c, c + w):
                for y in range(r, r + h):
                    self.own[b["die"]][(x, y)] = b["id"]
                    self.own[None][(x, y)] = b["id"]
        self.ext = {d["id"]: self.flip(d["at"]) for d in A.dies}
        self.ext[None] = [0, 0, C, R]
        self.texts, self.boxes, self.over = [], [], []
        self.route, self.via, self.sq, self.inner, self.lines, self.tabs, self.icons = {}, {}, [], [], [], [], []
        self.pinlab, self.dots, self.grect = [], [], []
        for nm, col, ms in A.groups:                      # サブシステムの枠(題つき)
            rs = [self.rect[m] for m in ms]
            x0, y0 = min(r[0] for r in rs) - 11, min(r[1] for r in rs) - 11
            x1, y1 = max(r[0] + r[2] for r in rs) + 11, max(r[1] + r[3] for r in rs) + 11
            self.grect.append((nm, col, x0, y0, x1, y1))
            self.boxes.append(((x0 + 5, y0 + 1, x0 + 9 + tw(nm, 8, True), y0 + 10.5), ("g", nm), "□", True))
        if A.schem:
            self.place_symbols()
        else:
            self.place_blocks()
        self.do_routes()
        for lid, r in self.route.items():                 # 線の通り道(ラベルを線に沿って重ねないため)
            l = A.links[lid]
            e = 1.2 + A.lw(l) / 2
            for a, b in zip(r["pts"], r["pts"][1:]):
                self.lines.append((min(a[0], b[0]) - e, min(a[1], b[1]) - e, max(a[0], b[0]) + e, max(a[1], b[1]) + e))
            if l.get("via"):                              # 線で結ぶ層またぎ: 線の途中に ⊠
                p, ms = along(r["pts"], 0.5), 5 + A.lw(l) * 0.6
                r["viapt"] = (p[0], p[1], ms)
                self.boxes.append(((p[0] - ms - 1, p[1] - ms - 1, p[0] + ms + 1, p[1] + ms + 1), ("l", lid), "⊠", True))
        self.place_vias()
        self.place_labels()

    def flip(self, at):
        c, r, w, h = at
        return [self.A.C - c - w if self.fx else c, self.A.R - r - h if self.fy else r, w, h]

    def px(self, c, r, w, h):
        return (self.cx[c], self.ry[r], sum(self.cw[c:c + w]) + (w - 1) * G, sum(self.rh[r:r + h]) + (h - 1) * G)

    # ---- ブロックの形(セレクタ = 台形)----
    def mux_out(self, bid):
        b = self.A.blocks[bid]
        if b.get("shape") != "mux" or self.A.small(b):
            return None
        o = str(b.get("out", "e")).upper()
        if (self.fx and o in "EW") or (self.fy and o in "NS"):
            o = FLIPSIDE[o]
        return o

    def slant(self, bid):
        x, y, w, h = self.drect[bid]
        return min(14, 0.22 * (h if self.mux_out(bid) in ("E", "W") else w))

    def span(self, bid, side):
        """辺 side の上で、線をつなげる範囲(台形の狭い辺は狭い)"""
        x, y, w, h = self.drect[bid]
        lo, hi = (y, y + h) if side in "EW" else (x, x + w)
        if self.mux_out(bid) == side:
            s = self.slant(bid)
            lo, hi = lo + s, hi - s
        return lo, hi

    def edge_at(self, bid, side, pos):
        """辺 side の、位置 pos での座標(台形の斜めの辺は位置で変わる)"""
        x, y, w, h = self.drect[bid]
        base = {"W": x, "E": x + w, "N": y, "S": y + h}[side]
        if self.A.schem and self.A.blocks[bid].get("shape") == "op":      # 丸: 円周まで
            d = pos - ((y + h / 2) if side in "EW" else (x + w / 2))
            return base + (w / 2 - math.sqrt(max(w * w / 4 - d * d, 0))) * (1 if side in "WN" else -1)
        o = self.mux_out(bid)
        if not o:
            return base
        s = self.slant(bid)
        if o in "EW" and side in "NS":
            t = min(max((pos - x) / w, 0), 1)
            t = 1 - t if o == "W" else t
            return base + s * t * (1 if side == "N" else -1)
        if o in "NS" and side in "EW":
            t = min(max((pos - y) / h, 0), 1)
            t = 1 - t if o == "N" else t
            return base + s * t * (1 if side == "W" else -1)
        return base

    def mux_poly(self, bid):
        x, y, w, h = self.drect[bid]
        s = self.slant(bid)
        return {"E": [(x, y), (x + w, y + s), (x + w, y + h - s), (x, y + h)],
                "W": [(x, y + s), (x + w, y), (x + w, y + h), (x, y + h - s)],
                "S": [(x, y), (x + w, y), (x + w - s, y + h), (x + s, y + h)],
                "N": [(x + s, y), (x + w - s, y), (x + w, y + h), (x, y + h)]}[self.mux_out(bid)]

    # ---- 文字 ----
    def text(self, x, y, t, fs, anc="start", rot=False, role="info", owner=None, bold=False, tight=False):
        bx = text_box(x, y, fs * (1.07 if bold else 1), t, anc, rot)
        self.texts.append(dict(x=x, y=y, t=t, fs=fs, anc=anc, rot=rot, role=role, owner=owner, bold=bold))
        self.boxes.append((bx, owner, t, tight))
        return bx

    def hit(self, bx):
        return sum(inter(bx, b[0]) for b in self.boxes)

    def bounds(self, die):
        c, r, w, h = self.ext[die]
        if (w, h) == (self.A.C, self.A.R) or self.A.schem:
            return (0, 0, self.W, self.H)
        x, y, pw, ph = self.px(c, r, w, h)
        return (x - 8, y - 8, x + pw + 8, y + ph + 8)

    def soft(self, bx, bd=None, blocks=False):
        """文字ではないものとの重なり(線に沿って乗る・その層の外に出る・ブロックの上に乗る)。lint にはしないが、避ける"""
        b0, b1, b2, b3 = bd or (0, 0, self.W, self.H)
        out = max(0, b0 - bx[0]) + max(0, b1 - bx[1]) + max(0, bx[2] - b2) + max(0, bx[3] - b3)
        blk = sum(inter(bx, (x, y, x + w, y + h)) for x, y, w, h in self.drect.values()) * 0.6 if blocks else 0
        return sum(inter(bx, ln) for ln in self.lines) * 0.5 + out * 40 + blk

    def sym_size(self, b):
        """回路の図で、このブロックに要るマスの大きさ (幅, 高さ)。記号(reg・mux・op・fifo)は名前を外に、箱は名前を中に書く"""
        info, pins = b.get("info", []), b["pins"]
        twm = max([tw(b["name"], 9.5, True)] + [tw(t, 7.2) for t in info])
        band = 13 + 8.6 * len(info)
        sh = b.get("shape")
        if sh in SYM:
            sw_, sh_ = SYM[sh]
            if sh == "mux":
                sh_ = max(40, 16 * max(len(v) for v in pins.values()) + 12)
            return max(sw_, twm) + 8, sh_ + 2 * band + 2
        pw = lambda sd: max([tw(t, 6.4) + 7 for t in pins.get(sd, [])] + [0])
        nw = max(twm + 16 + pw("w") + pw("e"), max(sum(tw(t, 6.4) + 12 for t in pins.get(sd, [])) for sd in "ns") + 8)
        nh = max(band + 10 + (9 if pins.get("n") else 0) + (9 if pins.get("s") else 0),
                 13 * max(len(pins.get("w", [])), len(pins.get("e", [])), 1) + 8)
        return nw, nh

    def place_symbols(self):
        """回路の図: 記号を置き、名前・端子の名前を書く"""
        A = self.A
        for b in A.blocks.values():
            x, y, w, h = self.rect[b["id"]]
            sh, pins, info, ow = b.get("shape"), b["pins"], b.get("info", []), ("b", b["id"])
            band = 13 + 8.6 * len(info)
            if sh in SYM:                                 # 記号: 名前は記号の上(上から線が入る記号は下)
                bot = bool(pins.get("n")) and not pins.get("s")
                ay, ah = y + band, h - 2 * band              # 名前の帯を上下に同じだけ取り、記号を行の中央に置く(線がまっすぐつながる)
                sw_, sh_ = SYM[sh]
                self.drect[b["id"]] = (x + w / 2 - sw_ / 2, ay, sw_, ah) if sh in ("reg", "mux") else (x + w / 2 - sw_ / 2, ay + ah / 2 - sh_ / 2, sw_, sh_)
                ty = y + h - band + 10.5 if bot else y + 9
                self.text(x + w / 2, ty, b["name"], 9.5, "middle", role="name", owner=ow, bold=True)
                for k, t in enumerate(info):
                    self.text(x + w / 2, ty + 9.4 + k * 8.6, t, 7.2, "middle", role="info", owner=ow)
            else:                                         # 箱(SRAM・制御など): 名前は中
                self.drect[b["id"]] = (x, y, w, h)
                pw = lambda sd: max([tw(t, 6.4) + 7 for t in pins.get(sd, [])] + [0])
                lx = x + w / 2 + (pw("w") - pw("e")) / 2
                ty = y + h / 2 - (11 + 8.6 * len(info)) / 2 + 9.5 + (4 if pins.get("n") else 0) - (4 if pins.get("s") else 0)
                self.text(lx, ty, b["name"], 9.5, "middle", role="name", owner=ow, bold=True)
                for k, t in enumerate(info):
                    self.text(lx, ty + 9.6 + k * 8.6, t, 7.2, "middle", role="info", owner=ow)
            for sd, names in pins.items():                # 端子の名前(箱は全部、セレクタは入力だけ)
                for nm in names:
                    if (sh in SYM and sh != "mux") or (sh == "mux" and (nm == "y" or sd in "ns")):
                        continue
                    side, pos = self.pin(b["id"], nm)
                    e = self.edge_at(b["id"], side, pos)
                    self.pinlab.append((b["id"], nm) + {"W": (e + 2.5, pos + 2.2, "start"), "E": (e - 2.5, pos + 2.2, "end"),
                                                        "N": (pos, e + 7.5, "middle"), "S": (pos, e - 3, "middle")}[side])

    def pin(self, bid, name):
        """端子 → (辺, 辺に沿った位置)。同じ辺の端子は、書いた順に等間隔"""
        for sd, names in self.A.blocks[bid]["pins"].items():
            if name in names:
                side = self.side(sd)
                lo, hi = self.span(bid, side)
                c, r, w, h = self.cells[bid]
                n, k = len(names), names.index(name)
                ctr = [self.ry[j] + self.rh[j] / 2 for j in range(r, r + h)] if side in "EW" else [self.cx[i] + self.cw[i] / 2 for i in range(c, c + w)]
                if 1 < n <= len(ctr):                     # 行(列)の数だけ端子がある: 行(列)の中央にそろえる → 隣とまっすぐつながる
                    pos = ctr[k]
                elif n == 1 and len(ctr) % 2:
                    pos = ctr[len(ctr) // 2]
                else:
                    pos = lo + (hi - lo) * (k + 0.5) / n
                return side, min(max(pos, lo + 2), hi - 2)

    def pin_port(self, bid, name):
        side, pos = self.pin(bid, name)
        c, r, w, h = self.cells[bid]
        if side in "EW":
            rr = min(range(r, r + h), key=lambda j: abs(self.ry[j] + self.rh[j] / 2 - pos))
            return (("V", c if side == "W" else c + w, rr), pos, side)
        cc = min(range(c, c + w), key=lambda i: abs(self.cx[i] + self.cw[i] / 2 - pos))
        return (("H", cc, r if side == "N" else r + h), pos, side)

    def place_blocks(self):
        A = self.A
        for b in A.blocks.values():
            x, y, w, h = self.rect[b["id"]]
            k = A.die[b["die"]]["idx"]
            ins = 0 if A.schem else 4 * k
            anc = b.get("label_at") or ("tl" if A.schem else ["tl", "br", "tr", "bl"][k % 4])
            right = anc[1] == "r"
            if A.small(b):                                # 名前だけの小さい箱
                nm, fs = b["name"], 8.5
                if h <= 36:                               # 低い行: マスいっぱい
                    dx, dy, dw, dh = x + ins, y, w - 2 * ins, h
                else:                                     # ふつうの行: 角に小さく置く
                    dw, dh = min(w - 2 * ins, tw(nm, fs, True) + 14), 20
                    dx = x + w - ins - dw if right else x + ins
                    dy = y + ins if anc[0] == "t" else y + h - ins - dh
                nm = wrap(nm, max(4, int((dw - 10) / (fs * 0.6))))[0]
                self.drect[b["id"]] = (dx, dy, dw, dh)
                self.text(dx + 5, dy + dh / 2 + fs * .36, nm, fs, "start", role="name", owner=("b", b["id"]), bold=True, tight=b.get("tight", False))
                continue
            if A.isbus(b):                                # バス = 帯。名前は帯の中に白い字で 1 行
                hz_ = w >= h
                self.drect[b["id"]] = (x, y + h / 2 - BUSH / 2, w, BUSH) if hz_ else (x + w / 2 - BUSH / 2, y, BUSH, h)
                ck = b.get("clock")
                t = "   ".join([b["name"]] + [" / ".join(b.get("info", []))] * bool(b.get("info")) + ([f"{ck} {A.clk[ck].get('freq', '')}"] if ck in A.clk else []))
                self.text(x + w / 2 + (0 if hz_ else 3.6), y + h / 2 + (3.6 if hz_ else 0), t, 10, "middle", rot=not hz_, role="bus", owner=("b", b["id"]), bold=True)
                continue
            self.drect[b["id"]] = (x + ins, y + ins, w - 2 * ins, h - 2 * ins)
            shape = b.get("shape")
            icon = shape in ("op", "ram", "fifo")
            ms_ = self.slant(b["id"]) if shape == "mux" else 0
            mu = lambda fs, cut=0: max(4, int((w - 14 - 2 * ins - cut) / (fs * 0.56)))
            cut = 24 if icon else ms_
            nl, fsn = wrap(b["name"], int(mu(FS_NAME, cut) * 0.93)), FS_NAME
            if len(nl) > 1 and len(wrap(b["name"], int(mu(9.2, cut) * 0.93))) == 1:     # 少しだけあふれる名前: 小さくして 1 行に
                nl, fsn = [b["name"]], 9.2
            lines = [(t, fsn, "name") for t in nl]
            for t in b.get("info", []):
                lines += [(u, FS_INFO, "info") for u in wrap(t, mu(FS_INFO, ms_))]
            ck = b.get("clock")
            if ck and ck in A.clk:
                lines.append((f"{ck} {A.clk[ck].get('freq', '')}", FS_INFO, "clk:" + ck))
            if b["id"] in A.detail_of:
                lines.append((f"▸中の回路: 別図 {A.detail_of[b['id']]}", FS_INFO, "more"))
            tot = sum(fs + 2 for _, fs, _ in lines)
            yy = y + 4 + ins + ms_ if anc[0] == "t" else y + h - 5 - ins - tot - ms_
            xx = x + w - 6 - ins if right else x + 6 + ins
            for t, fs, role in lines:
                yy += fs + 2
                sqw = 8 if role.startswith("clk:") else 0
                self.text(xx if right else xx + sqw, yy - 2, t, fs, "end" if right else "start", role=role, owner=("b", b["id"]),
                          bold=role == "name", tight=b.get("tight", False))
                if sqw:
                    self.sq.append((xx - tw(t, fs) - 8 if right else xx, yy - 2 - fs * 0.78, role[4:], b["id"]))
            if icon:                                      # 形の印(演算器の記号・SRAM・FIFO): 文字のない側の上の角
                ix, iy = (x + ins + 4 if right else x + w - ins - 26), y + ins + 4
                self.icons.append((b["id"], shape, ix, iy))
                self.boxes.append(((ix, iy, ix + 22, iy + 18), ("b", b["id"]), "□", True))
            if k:                                         # 上の層: 枠の角に「L2」の札(文字のない側の角)
                tx = x + w - ins - 17 if (right or A.schem) else x + ins + 1
                ty = y + ins + 1 if anc[0] == "b" else y + h - ins - 11
                self.tabs.append((b["id"], tx, ty))
                self.boxes.append(((tx, ty, tx + 16, ty + 10), ("b", b["id"]), "□", True))
            ins_ = b.get("inside")
            if ins_:                                      # 中身: 小さい箱を並べる。横(既定)か縦(inside_dir: v)
                items = []
                for t in ins_:
                    t = str(t)
                    kd = "fifo" if t.startswith("fifo:") else "mem" if t.startswith("mem:") else "box"
                    items.append((kd, t.split(":", 1)[1] if kd != "box" else t))
                n = len(items)
                flow = b.get("inside_flow", True)
                vert = b.get("inside_dir") == "v"
                if vert:
                    top = yy + 5 if anc[0] == "t" else y + 6 + ins
                    bot = y + h - 6 - ins if anc[0] == "t" else y + h - 5 - ins - tot - 5
                    gap = 7 if flow else 4
                    bh = min(40, (bot - top - gap * (n - 1)) / n)
                    gap = min(16, (bot - top - bh * n) / max(n - 1, 1))       # 高さいっぱいに均等に置く(下を空けない)
                    cellsz = [(x + 7 + ins, top + q * (bh + gap), w - 14 - 2 * ins, bh) for q in range(n)]
                else:
                    f0, f1 = b.get("inside_at", [0, 1])
                    gap, bh = (9 if flow else 5), 27
                    x0_, x1_ = x + 8 + ins + (w - 16 - 2 * ins) * f0, x + 8 + ins + (w - 16 - 2 * ins) * f1
                    wt = [0.62 if kd == "fifo" else 1 for kd, _ in items]     # FIFO はアイコンなので狭く
                    unit = (x1_ - x0_ - gap * (n - 1)) / sum(wt)
                    by = y + h * b.get("inside_y", 0.47 if k == 0 else 0.66) - bh / 2
                    cellsz, cx_ = [], x0_
                    for q in range(n):
                        cellsz.append((cx_, by, unit * wt[q], bh)); cx_ += unit * wt[q] + gap
                for q, ((kd, t), (cx_, cy_, cw_, ch_)) in enumerate(zip(items, cellsz)):
                    self.inner.append(dict(bid=b["id"], x=cx_, y=cy_, w=cw_, h=ch_, arrow=(q < n - 1 and flow), vert=vert, kind=kd))
                    if kd == "fifo":                      # FIFO: 小さいアイコン + 下に 1 行
                        self.text(cx_ + cw_ / 2, cy_ + ch_ / 2 + 10.5, t.replace("|", " "), 6.6, "middle", role="info", owner=("b", b["id"]),
                                  tight=b.get("tight", False))
                    else:
                        ls = t.split("|")
                        for m, u in enumerate(ls):
                            self.text(cx_ + cw_ / 2, cy_ + ch_ / 2 + (m - (len(ls) - 1) / 2) * 8 + 2.4, u, 6.6, "middle", role="info",
                                      owner=("b", b["id"]), tight=b.get("tight", False))
                    self.boxes.append(((cx_ - 1, cy_ - 1, cx_ + cw_ + 1, cy_ + ch_ + 1), ("b", b["id"]), "□", True))

    # ---- 配線 ----
    def allowed(self, die, e):
        o, i, j = e
        c, r, w, h = self.ext[die]
        own = self.own[die]
        if o == "V":
            if not (c <= i <= c + w and r <= j < r + h):
                return False
            a, b = own.get((i - 1, j)), own.get((i, j))
        else:
            if not (c <= i < c + w and r <= j <= r + h):
                return False
            a, b = own.get((i, j - 1)), own.get((i, j))
        return not (a is not None and a == b)

    def ends(self, e):
        o, i, j = e
        return [((i, j), self.yg[j]), ((i, j + 1), self.yg[j + 1])] if o == "V" else [((i, j), self.xg[i]), ((i + 1, j), self.xg[i + 1])]

    def elen(self, e):
        return (self.rh[e[2]] + G) if e[0] == "V" else (self.cw[e[1]] + G)

    def ports(self, bid):
        """(通路の区間, 区間に沿った位置, 辺)。小さい箱・台形は、線をつなげる範囲に寄せる"""
        c, r, w, h = self.cells[bid]
        cl = lambda v, sd: (lambda lo, hi: min(max(v, min(lo + 3, (lo + hi) / 2)), max(hi - 3, (lo + hi) / 2)))(*self.span(bid, sd))
        P = []
        for rr in range(r, r + h):
            cy = self.ry[rr] + self.rh[rr] / 2
            P.append((("V", c, rr), cl(cy, "W"), "W")); P.append((("V", c + w, rr), cl(cy, "E"), "E"))
        for cc in range(c, c + w):
            cx = self.cx[cc] + self.cw[cc] / 2
            P.append((("H", cc, r), cl(cx, "N"), "N")); P.append((("H", cc, r + h), cl(cx, "S"), "S"))
        return P

    def side(self, v):
        """model.yaml の辺(e/w/n/s)→ この図での辺"""
        if not v:
            return None
        o = str(v).upper()
        return FLIPSIDE[o] if (self.fx and o in "EW") or (self.fy and o in "NS") else o

    def free(self, bid, side, pos, lw, occ):
        """同じ辺に入る線どうしが重ならない位置"""
        lo, hi = self.span(bid, side)
        lst = occ[(bid, side)]
        hw = lambda v: max(2.6, v * .5 + 2.2)             # 矢じりの半分の幅
        ok = lambda p: all(abs(p - q) >= hw(lw) + hw(w) + 2 for q, w in lst)
        best = pos
        if not ok(pos):
            for d in range(2, 70, 2):
                c = [p for p in (pos - d, pos + d) if lo + 3 <= p <= hi - 3 and ok(p)]
                if c:
                    best = c[0]
                    break
        lst.append((best, lw))
        return best

    def direct(self, a, b, fs=None, ts=None, pa=None, pb=None):
        """向かい合う辺がすき間 1 本をはさんで重なっていれば → (向き, すき間の番号, 重なりの範囲, a の辺, b の辺)"""
        (ac, ar, aw, ah), (bc, br, bw_, bh) = self.cells[a], self.cells[b]
        for sa, ok, gi in (("E", ac + aw == bc, bc), ("W", bc + bw_ == ac, ac), ("S", ar + ah == br, br), ("N", br + bh == ar, ar)):
            if not ok or (fs and fs != sa) or (ts and ts != FLIPSIDE[sa]) or (pa and pa[2] != sa) or (pb and pb[2] != FLIPSIDE[sa]):
                continue
            (l1, h1), (l2, h2) = self.span(a, sa), self.span(b, FLIPSIDE[sa])
            lo, hi = max(l1, l2), min(h1, h2)
            fix = [q[1] for q in (pa, pb) if q]           # 端子が決まっている時は、その位置でまっすぐ結べる時だけ
            if any(not (lo - 0.5 <= q <= hi + 0.5) for q in fix) or (len(fix) == 2 and abs(fix[0] - fix[1]) > 1):
                continue
            if hi - lo >= 8 or fix:
                return ("H" if sa in "EW" else "V", gi, lo, hi, sa, FLIPSIDE[sa], fix[0] if fix else None)
        return None

    def find(self, die, a, b, usage, fs=None, ts=None, pa=None, pb=None, net=(), penj=None):
        PA = [pa] if pa else [p for p in self.ports(a) if self.allowed(die, p[0]) and (not fs or p[2] == fs)]
        PB = [pb] if pb else [p for p in self.ports(b) if self.allowed(die, p[0]) and (not ts or p[2] == ts)]
        up = lambda e: 1.8 if (penj is not None and e[0] == "H" and e[2] <= penj) else 1
        wt = lambda e: (0.15 if e in net else 1 + 0.45 * usage[e]) * up(e)      # 同じ配線の通り道は安く(幹を共有して分岐する)
        best, prev, hq = {}, {}, []
        for k, (e, pos, _) in enumerate(PA):
            for n, cd in self.ends(e):
                c, st = abs(pos - cd) * wt(e), (n, e[0])
                if c < best.get(st, 1e18):
                    best[st], prev[st] = c, ("S", k)
                    heapq.heappush(hq, (c, st))
        C, R = self.A.C, self.A.R
        while hq:
            c, st = heapq.heappop(hq)
            if c > best[st]:
                continue
            (i, j), o = st
            for e2, m in ((("V", i, j), (i, j + 1)), (("V", i, j - 1), (i, j - 1)), (("H", i, j), (i + 1, j)), (("H", i - 1, j), (i - 1, j))):
                if not (0 <= m[0] <= C and 0 <= m[1] <= R) or e2[1] < 0 or e2[2] < 0 or not self.allowed(die, e2):
                    continue
                c2 = c + self.elen(e2) * wt(e2) + (BEND if e2[0] != o else 0)
                st2 = (m, e2[0])
                if c2 < best.get(st2, 1e18):
                    best[st2], prev[st2] = c2, st
                    heapq.heappush(hq, (c2, st2))
        cand = None
        for kb, (e, pos, _) in enumerate(PB):
            for ka, (ea, pa, _) in enumerate(PA):
                if ea == e:
                    t = abs(pa - pos) + 2
                    if cand is None or t < cand[0]:
                        cand = (t, ka, kb, None)
            for n, cd in self.ends(e):
                for o in "VH":
                    c = best.get((n, o))
                    if c is not None:
                        t = c + abs(pos - cd) * wt(e) + (BEND if o != e[0] else 0)
                        if cand is None or t < cand[0]:
                            cand = (t, None, kb, (n, o))
        if cand is None:
            return None
        _, ka, kb, st = cand
        nodes = []
        while st is not None and st[0] != "S":
            nodes.append(st[0]); st = prev[st]
        if ka is None:
            ka = st[1]
        nodes.reverse()
        edges = [PA[ka][0]]
        for n, m in zip(nodes, nodes[1:]):
            (i, j), (p, q) = n, m
            edges.append(("V", i, min(j, q)) if i == p else ("H", min(i, p), j))
        if nodes:
            edges.append(PB[kb][0])
        runs = []
        for e in edges:
            key = (e[0], e[1] if e[0] == "V" else e[2])
            if runs and runs[-1]["key"] == key:
                if e not in runs[-1]["edges"]:
                    runs[-1]["edges"].append(e)
            else:
                runs.append(dict(key=key, edges=[e]))
        return dict(runs=runs, sp=PA[ka], tp=PB[kb])

    def do_routes(self):
        A = self.A
        usage, used = collections.Counter(), collections.defaultdict(dict)     # used[区間] = {段: 配線}
        netE, netslot = collections.defaultdict(set), collections.defaultdict(dict)
        dgrp = collections.defaultdict(list)
        pri = lambda l: (A.focus.index(l["kind"]) if l["kind"] in A.focus else 9)       # 全体図に描く線を先に通す
        slotw = collections.defaultdict(dict)             # (向き, 通路) → {段: 太さ}
        for l in sorted(A.links.values(), key=pri):
            if l.get("bad") or (l.get("via") and l.get("far") != "line"):
                continue
            a, b = l["from"], l["to"]
            fs, ts = self.side(l.get("from_side")), self.side(l.get("to_side"))
            nk = l.get("net") or ("link", l["id"])
            pa = self.pin_port(a, l["fpin"]) if l.get("fpin") else None
            pb = self.pin_port(b, l["tpin"]) if l.get("tpin") else None
            d = self.direct(a, b, fs, ts, pa, pb)
            if d:
                self.route[l["id"]] = dict(direct=d)
                if d[6] is None:
                    dgrp[d[:4]].append(l["id"])
                continue
            penj = None
            if A.schem and self.cells[b][0] + self.cells[b][2] <= self.cells[a][0] and self.cells[b][1] + self.cells[b][3] > self.cells[a][1]:
                penj = self.cells[a][1]                   # 回路の図: 右から左へ戻る線は、上ではなく下を回す
            r = self.find(None if (l.get("via") or A.schem) else A.blocks[a]["die"], a, b, usage, fs, ts, pa, pb, netE[nk], penj)
            if not r:
                if (self.fx, self.fy) == A.base():
                    A.lint.append(f"[経路] {l['id']}: {a} → {b} の通り道がない")
                continue
            r["pinned"] = (pa is not None, pb is not None)
            for run in r["runs"]:                         # 段: 同じ配線は同じ段(重ねて 1 本に見せる)
                for s in ([netslot[nk][run["key"]]] if run["key"] in netslot[nk] else []) + list(range(60)):
                    if all(used[e].get(s, nk) == nk for e in run["edges"]):
                        break
                run["slot"] = s
                netslot[nk].setdefault(run["key"], s)
                slotw[run["key"]][s] = max(slotw[run["key"]].get(s, 0), A.lw(l))
                for e in run["edges"]:
                    if e not in netE[nk]:
                        usage[e] += 1
                    used[e][s] = nk; netE[nk].add(e)
            self.route[l["id"]] = r
        off = {}                                          # (向き, 通路, 段) → 通路の中心からのずれ。太さに合わせて並べる
        for key, sw in slotw.items():
            n = max(sw) + 1
            ws = [sw.get(k, 1.0) for k in range(n)]
            gap = min(3.2, max(0.8, (G - 7 - sum(ws)) / max(n - 1, 1)))
            tot = sum(ws) + gap * (n - 1)
            scale = min(1.0, (G - 5) / tot)
            cur = -tot / 2
            for k in range(n):
                off[key + (k,)] = (cur + ws[k] / 2) * scale
                cur += ws[k] + gap
        pg = collections.defaultdict(list)                # 同じマスの辺から出る線: 位置をずらす
        for lid, r in self.route.items():
            if "runs" in r:
                if not r["pinned"][0]:
                    pg[(A.links[lid]["from"], r["sp"][0])].append((lid, "s"))
                if not r["pinned"][1]:
                    pg[(A.links[lid]["to"], r["tp"][0])].append((lid, "t"))
        for (bid, e), lst in pg.items():
            dx, dy, dw, dh = self.drect[bid]
            span = min(self.rh[e[2]], dh) if e[0] == "V" else min(self.cw[e[1]], dw)
            d = min(12, (span - 12) / len(lst))
            for k, (lid, w) in enumerate(lst):
                self.route[lid][w + "off"] = (k - (len(lst) - 1) / 2) * d
        occ = collections.defaultdict(list)               # (ブロック, 辺) → [(位置, 太さ)]
        for lid, r in self.route.items():
            a, b = A.links[lid]["from"], A.links[lid]["to"]
            if "direct" in r:
                o, gi, lo, hi, sa, sb, pf = r["direct"]
                grp = dgrp[r["direct"][:4]]
                p = pf if pf is not None else lo + (hi - lo) * (grp.index(lid) + 1) / (len(grp) + 1)
                ea, eb = self.edge_at(a, sa, p), self.edge_at(b, sb, p)
                r["pts"] = [(ea, p), (eb, p)] if o == "H" else [(p, ea), (p, eb)]
                occ[(a, sa)].append((p, A.lw(A.links[lid]))); occ[(b, sb)].append((p, A.lw(A.links[lid])))
        for lid, r in self.route.items():
            a, b = A.links[lid]["from"], A.links[lid]["to"]
            if "direct" in r:
                continue
            co = []
            for run in r["runs"]:
                o, line = run["key"]
                co.append((self.xg[line] if o == "V" else self.yg[line]) + off[run["key"] + (run["slot"],)])
            (se, sp, sd), (te, tp, td) = r["sp"], r["tp"]
            if not r["pinned"][0]:
                sp = self.free(a, sd, sp + r.get("soff", 0), A.lw(A.links[lid]), occ)
            if not r["pinned"][1]:
                tp = self.free(b, td, tp + r.get("toff", 0), A.lw(A.links[lid]), occ)
            ss, ts = self.edge_at(a, sd, sp), self.edge_at(b, td, tp)
            pts = [(ss, sp) if se[0] == "V" else (sp, ss)]
            pts.append((co[0], sp) if se[0] == "V" else (sp, co[0]))
            for k in range(len(co) - 1):
                pts.append((co[k], co[k + 1]) if r["runs"][k]["key"][0] == "V" else (co[k + 1], co[k]))
            pts.append((co[-1], tp) if te[0] == "V" else (tp, co[-1]))
            pts.append((ts, tp) if te[0] == "V" else (tp, ts))
            r["pts"] = [p for k, p in enumerate(pts) if k == 0 or abs(p[0] - pts[k - 1][0]) + abs(p[1] - pts[k - 1][1]) > 0.3]
        nets = collections.defaultdict(list)              # 分岐の ●
        for lid in self.route:
            if A.links[lid].get("net"):
                nets[A.links[lid]["net"]].append(lid)
        for lids in nets.values():
            seen = set()
            for i, x in enumerate(lids):
                for y in lids[i + 1:]:
                    p = diverge(self.route[x]["pts"], self.route[y]["pts"])
                    if p and (round(p[0]), round(p[1])) not in seen:
                        seen.add((round(p[0]), round(p[1])))
                        self.dots.append((p[0], p[1], x, y))

    # ---- 層をまたぐ接続の印(真上・真下でつなぐもの)----
    def place_vias(self):
        A = self.A
        pri = lambda l: (A.focus.index(l["kind"]) if l["kind"] in A.focus else 9)
        for l in sorted(A.links.values(), key=pri):
            if not l.get("via") or l.get("bad") or l["id"] in self.route:
                continue
            _, l1, l2 = A.bw(l)
            ra, rb = self.rect[l["from"]], self.rect[l["to"]]
            if l.get("far"):
                self.via[l["id"]] = [self.mark(l, bid, self.rect[bid], [f"{l['via']} {l1}", f"{l2} ⇄{o}"]) for bid, o in ((l["from"], l["to"]), (l["to"], l["from"]))]
                continue
            ox, oy = max(ra[0], rb[0]), max(ra[1], rb[1])
            ow, oh = min(ra[0] + ra[2], rb[0] + rb[2]) - ox, min(ra[1] + ra[3], rb[1] + rb[3]) - oy
            self.via[l["id"]] = [self.mark(l, None, (ox, oy, ow, oh), [f"{l['via']} {l1}", l2])]

    def mark(self, l, bid, r, lines):
        x, y, w, h = r
        lines = [t for t in lines if t]
        tl = max(tw(t, FS_LAB) for t in lines)
        ms = 5 + self.A.lw(l) * 0.6                       # 印の大きさ = ビット幅
        lh = FS_LAB + 1.2
        ty = lambda my, k: my - 1 + k * lh - (len(lines) - 1) * lh / 2 + FS_LAB * .35
        cands = []
        if bid and self.A.small(self.A.blocks[bid]):      # 小さい箱: 箱のすぐ横に置く
            dx, dy, dw, dh = self.drect[bid]
            cands = [(dx - ms - 3, dy + dh / 2, -1), (dx + dw + ms + 3, dy + dh / 2, 1), (dx + dw / 2, dy - ms - 3, 1), (dx + dw / 2, dy + dh + ms + 3, 1)]
        else:
            for fy_ in (0.5, 0.36, 0.64, 0.22, 0.78):
                for fx_ in (0.5, 0.3, 0.7, 0.14, 0.86):
                    for side in (1, -1):
                        mx = x + w * fx_
                        if side == 1 and mx + ms + 3 + tl > x + w - 3 or side == -1 and mx - ms - 3 - tl < x + 3:
                            continue
                        cands.append((mx, y + h * fy_, side))
        best = None
        for n, (mx, my, side) in enumerate(cands):
            bxs = [text_box(mx + side * (ms + 3), ty(my, k), FS_LAB, t, "start" if side == 1 else "end") for k, t in enumerate(lines)]
            sc = (sum(self.hit(b) for b in bxs) + self.hit((mx - ms, my - ms, mx + ms, my + ms))) * 10 + n + sum(self.soft(b) for b in bxs)
            if best is None or sc < best[0]:
                best = (sc, mx, my, side)
        if best is None:
            best = (0, x + w / 2, y + h / 2, 1)
        sc, mx, my, side = best
        if sc >= 10:
            self.over.append(l["id"])
        for k, t in enumerate(lines):
            self.text(mx + side * (ms + 3), ty(my, k), t, FS_LAB, "start" if side == 1 else "end", role="lab", owner=("l", l["id"]),
                      tight=l.get("tight", False))
        self.boxes.append(((mx - ms, my - ms, mx + ms, my + ms), ("l", l["id"]), "⊠", True))
        return (mx, my, side, ms)

    # ---- 線のラベル ----
    def place_labels(self):
        A = self.A
        netseen = set()
        for lid, r in self.route.items():
            l = A.links[lid]
            _, l1, l2 = A.bw(l)
            if l.get("via"):
                l1 = f"{l['via']} {l1}".strip()
            one = " ".join(x for x in (l1, l2) if x)
            if A.schem:                                   # 回路の図: 信号名とビット幅だけ。同じ配線の 2 本目からは書かない
                l1 = " ".join(str(x) for x in (l.get("via"), l.get("label") or l.get("proto"), f"{l['width']}b" if l.get("width") else None) if x)
                one, l2 = ("" if l.get("net") in netseen else l1), ""
                netseen.add(l.get("net") or lid)
            pts = r["pts"]
            hw = A.lw(l) / 2                              # 線の半分の太さ(ラベルを線から離す)
            if not one:
                continue
            two = [x for x in (l1, l2) if x]
            bd = self.bounds(A.blocks[l["from"]]["die"]) if not l.get("via") else None
            segs = sorted(((abs(a[0] - b[0]) + abs(a[1] - b[1]), a, b) for a, b in zip(pts, pts[1:])), key=lambda s: -s[0])
            cands = []
            for n, (ln, a, b) in enumerate(segs):
                horiz = abs(a[1] - b[1]) < 0.5
                # ラベルの向きは、置く場所のすき間で決める: 縦のすき間 = 縦書き、横のすき間 = 横書き
                if "direct" in r:
                    o = r["direct"][0]
                    cx_, cy_ = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                    for lines in ([one], two):
                        tl = max(tw(t, FS_LAB) for t in lines)
                        for side, far_ in ((-1, 0), (1, 0), (-1, 1), (1, 1), (-1, 2), (1, 2)):
                            pen = far_ * 0.8 + (0 if side == 1 else 0.1) + (0 if len(lines) == 1 else .5)
                            if o == "H":                  # 横向きの短い矢印(縦のすき間): 矢印の上か下に、縦書き
                                yc = cy_ + side * (5 + hw + tl / 2 + far_ * 26)
                                xs = [cx_ + (k - (len(lines) - 1) / 2) * (FS_LAB + 1) + FS_LAB * .3 for k in range(len(lines))]
                                cands.append((pen, [(x, yc, t, "middle", True) for x, t in zip(xs, lines)]))
                                if far_ == 0:             # すぐ上・下の横のすき間に、横書き
                                    (ax_, ay_, aw_, ah_), (bx_, by_, bw__, bh_) = self.drect[l["from"]], self.drect[l["to"]]
                                    for yy_ in ((min(ay_, by_) - 4, max(ay_, by_) - 4) if side == -1 else (max(ay_ + ah_, by_ + bh_) + 9, min(ay_ + ah_, by_ + bh_) + 9)):
                                        if abs(yy_ - cy_) > 64:   # 矢印から遠すぎる所には置かない
                                            continue
                                        ys = [yy_ + (k - (len(lines) - 1 if side == -1 else 0)) * (FS_LAB + 1) for k in range(len(lines))]
                                        cands.append((pen + 3, [(cx_, y, t, "middle", False) for y, t in zip(ys, lines)]))
                            else:                         # 縦向きの短い矢印(横のすき間): 矢印の右か左に、横書き
                                ys = [cy_ + (k - (len(lines) - 1) / 2) * (FS_LAB + 1) + FS_LAB * .35 for k in range(len(lines))]
                                cands.append((pen, [(cx_ + side * (4 + hw + far_ * 26), y, t, "start" if side == 1 else "end", False)
                                                    for y, t in zip(ys, lines)]))
                    continue
                for lines in ([one], two):
                    tl = max(tw(t, FS_LAB) for t in lines)
                    fit = ln >= tl + 6
                    for t_ in (0.5, 0.26, 0.74, 0.12, 0.88):
                        if not fit and t_ != 0.5:
                            continue
                        px_, py_ = a[0] + (b[0] - a[0]) * t_, a[1] + (b[1] - a[1]) * t_
                        if fit:                           # 線の端からはみ出さない
                            if horiz:
                                px_ = min(max(px_, min(a[0], b[0]) + tl / 2 + 3), max(a[0], b[0]) - tl / 2 - 3)
                            else:
                                py_ = min(max(py_, min(a[1], b[1]) + tl / 2 + 3), max(a[1], b[1]) - tl / 2 - 3)
                        for side in (-1, 1):
                            pen = n * 1.0 + (0 if fit else 6) + (0 if len(lines) == 1 else .4) + abs(t_ - .5)
                            g = 1.6 + hw
                            if horiz:
                                ys = [py_ - g - .6 - (len(lines) - 1 - k) * (FS_LAB + 1) for k in range(len(lines))] if side == -1 else \
                                     [py_ + g + FS_LAB * .8 + k * (FS_LAB + 1) for k in range(len(lines))]
                                cands.append((pen, [(px_, y, t, "middle", False) for y, t in zip(ys, lines)]))
                            else:
                                xs = [px_ - g - FS_LAB * .2 - (len(lines) - 1 - k) * (FS_LAB + 1) for k in range(len(lines))] if side == -1 else \
                                     [px_ + g + FS_LAB * .8 + k * (FS_LAB + 1) for k in range(len(lines))]
                                cands.append((pen, [(x, py_, t, "middle", True) for x, t in zip(xs, lines)]))
            best = None
            for pen, items in cands:
                bxs = [text_box(x, y, FS_LAB, t, anc, rot) for x, y, t, anc, rot in items]
                ov = sum(self.hit(b) for b in bxs)
                sc = ov * 10 + pen + sum(self.soft(b, bd, True) for b in bxs)
                if best is None or sc < best[0]:
                    best = (sc, ov, items)
                if sc < 0.2:
                    break
            sc, ov, items = best
            if ov > 0:
                self.over.append(lid)
            for x, y, t, anc, rot in items:
                self.text(x, y, t, FS_LAB, anc, rot, role="lab", owner=("l", lid), tight=l.get("tight", False))
            if A.schem and (l.get("width") or 0) > 1:     # バスの斜線: ラベルにいちばん近い線の上
                bx = text_box(items[0][0], items[0][1], FS_LAB, items[0][2], items[0][3], items[0][4])
                c, bestd = ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2), None
                for a, b in zip(pts, pts[1:]):
                    q = (min(max(c[0], min(a[0], b[0]) + 5), max(a[0], b[0]) - 5), a[1]) if abs(a[1] - b[1]) < 0.5 else \
                        (a[0], min(max(c[1], min(a[1], b[1]) + 5), max(a[1], b[1]) - 5))
                    dd = abs(q[0] - c[0]) + abs(q[1] - c[1])
                    if abs(a[0] - b[0]) + abs(a[1] - b[1]) >= 12 and (bestd is None or dd < bestd):
                        bestd, r["tick"] = dd, q
        for lid, r in self.route.items():                 # 動作説明の番号を置く点: 線の上で、文字に重ならない所
            pts = r["pts"]
            r["mid"] = None
            for fr in ((0.8, 0.7, 0.9, 0.6, 0.5, 0.4) if A.links[lid].get("net") else (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88)):
                p = along(pts, fr)
                if self.hit((p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7)) == 0:
                    r["mid"] = p
                    break
            if r["mid"] is None:                          # 短い線: 線の中央から横にずらして、文字のない側に置く
                a, b = pts[0], pts[-1]
                c = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                horiz = abs(a[1] - b[1]) < abs(a[0] - b[0])
                cs = [(c[0] + (0 if horiz else d), c[1] + (d if horiz else 0)) for d in (-11, 11, -22, 22)]
                r["mid"] = min(cs, key=lambda q: self.hit((q[0] - 7, q[1] - 7, q[0] + 7, q[1] + 7)))

    def hollow(self):
        """中が空いている 1 層目のブロック(3 マス以上で、文字と中身の箱が面積の 7% 未満)"""
        out = []
        for b in self.A.blocks.values():
            if self.A.schem or self.A.die[b["die"]]["idx"] != 0 or b["at"][2] * b["at"][3] < 3 or self.A.small(b) or b.get("shape") in ("mux", "bus"):
                continue
            x, y, w, h = self.rect[b["id"]]
            used = sum((bx[2] - bx[0]) * (bx[3] - bx[1]) for bx, ow, t, _ in self.boxes
                       if t != "⊠" and x <= (bx[0] + bx[2]) / 2 <= x + w and y <= (bx[1] + bx[3]) / 2 <= y + h)
            if used / (w * h) < 0.07:
                out.append((b["id"], b["at"][2] * b["at"][3], used / (w * h)))
        return out

    def overlaps(self):
        """文字どうしの重なり(tight: true の相手は許す)→ [(a, b)]"""
        out = []
        bs = [b for b in self.boxes if b[2] not in ("⊠", "□")]
        for i, a in enumerate(bs):
            for b in bs[i + 1:]:
                if a[1] == b[1] or a[3] or b[3]:
                    continue
                ar = inter(a[0], b[0])
                if ar > 0 and ar / min((a[0][2] - a[0][0]) * (a[0][3] - a[0][1]), (b[0][2] - b[0][0]) * (b[0][3] - b[0][1])) > 0.12:
                    out.append((a[2], b[2]))
        return out


# ======================= 描く =======================
def defs():
    return '<defs>' + "".join(
        f'<pattern id="ht_{k}" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">'
        f'<line x1="0" y1="0" x2="0" y2="7" stroke="{c}" stroke-width="2.6" opacity=".30"/></pattern>' for k, c in KHATCH.items()) + '</defs>'


def sym_svg(A, L, b, dm, act):
    """回路の図の記号: セレクタ = 台形、レジスタ = 縦長の箱 + クロックの三角、演算器 = 丸、FIFO = 仕切った箱、SRAM = 橙の箱"""
    x, y, w, h = L.drect[b["id"]]
    k = A.die[b["die"]]["idx"]
    sh = b.get("shape")
    st = DIMS if dm else ACC if act else DIE_STYLE[k % len(DIE_STYLE)][0] if k else INK
    sw = 2.2 if act else 1.3
    if sh == "mux":
        return ('<path d="M' + " L".join(f"{f(px_)},{f(py_)}" for px_, py_ in L.mux_poly(b["id"])) +
                f' z" fill="#fff" stroke="{st}" stroke-width="{sw}" stroke-linejoin="round"/>')
    if sh == "reg":
        ck = A.clk.get(b.get("clock"))
        return (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" fill="#fff" stroke="{st}" stroke-width="{sw}"/>'
                f'<path d="M{f(x + w / 2 - 5.5)},{f(y + h)} l5.5,-8.5 l5.5,8.5" fill="none" stroke="{DIMS if dm else ck["col"] if ck else st}" stroke-width="1.3"/>')
    if sh == "op":
        t = str(b.get("sym", "ƒ"))
        fs = 15 if wlen(t) <= 2 else 10.5 if wlen(t) <= 3 else 8
        return (f'<circle cx="{f(x + w / 2)}" cy="{f(y + h / 2)}" r="{f(w / 2)}" fill="#fff" stroke="{st}" stroke-width="{sw}"/>'
                f'<text x="{f(x + w / 2)}" y="{f(y + h / 2 + fs * .36)}" font-size="{fs}" font-weight="700" fill="{DIMT if dm else INK}" text-anchor="middle">{esc(t)}</text>')
    if sh == "fifo":
        return fifo_cells(x, y, w, h, 4, True, DIMS if dm else ACC if act else FIFO_COL, DIMF if dm else FIFO_FILL, sw)
    mem = sh == "ram"
    return (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="{0 if mem else 3}" fill="{DIMF if dm else "#ffedd5" if mem else "#f1f5f9"}" '
            f'stroke="{st if (dm or act or k) else MEMC if mem else INK}" stroke-width="{sw}"/>')


def body_svg(A, L, mode):
    """図の本体(配置は L のまま)。mode: kind = overview / layer / scene、die、steps、blocks、kinds(描く線の種類)、act_kind"""
    kind = mode["kind"]
    die_only = mode.get("die")
    steps = mode.get("steps") or []
    kinds = set(mode.get("kinds") or A.focus)
    shown = lambda l: l["kind"] in kinds
    actl, actb, stepno = None, None, collections.defaultdict(list)
    if kind == "scene":
        actl, actb = set(), set(mode.get("blocks") or [])
        for n, st in enumerate(steps):
            for x in st.get("links", []):
                if x not in A.links or A.links[x].get("bad"):
                    continue
                actl.add(x); stepno[x].append(st.get("_n", n + 1))
                actb |= {A.links[x]["from"], A.links[x]["to"]}
            actb |= set(st.get("blocks", [])) | set(st.get("state") or {})
        if mode.get("act_kind"):
            for l in A.links.values():
                if l["kind"] == mode["act_kind"] and not l.get("bad"):
                    actl.add(l["id"]); actb |= {l["from"], l["to"]}
    dimb = lambda bid: actb is not None and bid not in actb
    diml = lambda lid: actl is not None and lid not in actl
    hidden_die = lambda d: die_only is not None and d != die_only
    b_ = []
    for nm, col, x0, y0, x1, y1 in L.grect:               # サブシステムの枠: 薄い色の地 + 題
        b_.append(f'<rect x="{f(x0)}" y="{f(y0)}" width="{f(x1 - x0)}" height="{f(y1 - y0)}" rx="7" fill="{col}" fill-opacity=".09" '
                  f'stroke="{col}" stroke-opacity=".45" stroke-width="1"/>'
                  f'<text x="{f(x0 + 7)}" y="{f(y0 + 9)}" font-size="8" font-weight="800" fill="{col}">{esc(nm)}</text>')
    # 層の範囲・原点の印
    for d in A.dies:
        c, r, w, h = L.ext[d["id"]]
        x, y, pw, ph = L.px(c, r, w, h)
        full = (w, h) == (A.C, A.R)
        col = DIE_STYLE[d["idx"] % len(DIE_STYLE)][0]
        ghost = hidden_die(d["id"])
        if A.schem:                                       # 回路の図: 上の層は薄い色の地(枠の線は引かない)
            if d["idx"] and not ghost:
                b_.append(f'<rect x="{f(x - G / 2)}" y="{f(y - G / 2)}" width="{f(pw + G)}" height="{f(ph + G)}" rx="6" fill="{col}" fill-opacity=".07"/>'
                          f'<text x="{f(x - G / 2 + 5)}" y="{f(y - G / 2 + 10)}" font-size="8" font-weight="700" fill="{col}">{esc(d["id"])} {esc(d["name"])}</text>')
            continue
        e = G - 5 - 3 * d["idx"] if full else 8
        b_.append(f'<rect x="{f(x - e)}" y="{f(y - e)}" width="{f(pw + 2 * e)}" height="{f(ph + 2 * e)}" rx="5" fill="none" '
                  f'stroke="{DIMS if ghost else col}" stroke-width="{1 if ghost else 2.2 if d["idx"] else 1.6}"/>')
        if ghost:
            continue
        if d["idx"]:                                      # 上の層の範囲: 外形の線の上に札
            t = f'{d["id"]} {d["name"]} の範囲'
            tx_ = x + pw + e - 22 - tw(t, 8, True)        # 外形の右上(左上はサブシステムの題)
            b_.append(f'<rect x="{f(tx_)}" y="{f(y - e - 6)}" width="{f(tw(t, 8, True) + 10)}" height="12" rx="2" fill="{col}"/>'
                      f'<text x="{f(tx_ + 5)}" y="{f(y - e + 3)}" font-size="8" font-weight="700" fill="#fff">{esc(t)}</text>')
        mx, my = A.state(d, L.fx, L.fy)
        ox, oy = (x + pw + e if mx else x - e), (y - e if my else y + ph + e)
        sx, sy = (-1 if mx else 1), (-1 if my else 1)
        b_.append(f'<path d="M{f(ox)},{f(oy)} l{9 * sx},0 l{-9 * sx},{-9 * sy} z" fill="{col}"/>')
        gy = oy - 4 * sy if full else oy + 13 * sy        # 小さい層は、外形のすぐ外(すき間)に書く
        b_.append(f'<g transform="translate({f(ox + 12 * sx)},{f(gy)}) scale({sx},{sy})"><path d="M0,0 v-10 h6 M0,-5.5 h4" '
                  f'fill="none" stroke="{col}" stroke-width="1.8"/></g>')
        b_.append(f'<text x="{f(ox + 22 * sx)}" y="{f(gy - 1 * sy + (5 if my else 0))}" font-size="6.8" fill="{col}" '
                  f'text-anchor="{"end" if mx else "start"}">{esc(d["id"])} 原点{"(裏から見ている → F が鏡文字)" if mx != my else ""}</text>')
    # ブロックの枠。上の層は、枠の色 + 斜線(種類の色)+ 札。破線は使わない
    for b in A.blocks.values():
        x, y, w, h = L.drect[b["id"]]
        k = A.die[b["die"]]["idx"]
        col = DIE_STYLE[k % len(DIE_STYLE)][0]
        if hidden_die(b["die"]):
            b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="3" fill="none" stroke="{DIMS}" stroke-width="0.8"/>')
            continue
        dm = dimb(b["id"])
        act = actb is not None and not dm
        if A.schem:
            b_.append(sym_svg(A, L, b, dm, act))
            continue
        small = A.small(b)
        shape = None if small else b.get("shape")
        if shape == "bus":                                # バスの帯(色 = 線の種類の色)
            bc = LCOL.get(b.get("bus", "data"), LCOL["data"])
            b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="4" fill="{DIMF if dm else bc}" '
                      f'stroke="{DIMS if dm else ACC if act else bc}" stroke-width="{2.6 if act else 1}"/>')
            continue
        fill = DIMF if dm else FIFO_FILL if shape == "fifo" else KFILL.get(b["kind"], KFILL["misc"])
        if k and not die_only and not small and not A.schem:      # 重ねて描く時: 下の層の色を消さないよう、斜線だけ
            fill = "none" if dm else f'url(#ht_{b["kind"] if b["kind"] in KHATCH else "misc"})'
        sw = 2.4 if act else (1.9 if k else 1.3) if not small else (1.4 if k else 0.9)
        sc_ = DIMS if dm else ACC if act else MEMC if (b["kind"] == "mem" and not k) else col if (k or not small) else "#94a3b8"
        if shape == "mux":
            b_.append('<path d="M' + " L".join(f"{f(px_)},{f(py_)}" for px_, py_ in L.mux_poly(b["id"])) +
                      f' z" fill="{fill}" stroke="{sc_}" stroke-width="{sw}" stroke-linejoin="round"/>')
        else:
            b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="{10 if shape == "op" else 3}" fill="{fill}" '
                      f'stroke="{sc_}" stroke-width="{sw}"/>')
        if shape == "reg":                                # レジスタ: 下の辺にクロックの三角
            b_.append(f'<path d="M{f(x + w - 17)},{f(y + h)} l4.5,-7 l4.5,7" fill="none" stroke="{sc_}" stroke-width="1.1"/>')
    for bid, nm, tx, ty, anc in L.pinlab:                 # 端子の名前
        if not hidden_die(A.blocks[bid]["die"]):
            b_.append(f'<text x="{f(tx)}" y="{f(ty)}" font-size="6.2" fill="{DIMT if dimb(bid) else "#475569"}" text-anchor="{anc}">{esc(nm)}</text>')
    for bid, shape, ix, iy in L.icons:
        if hidden_die(A.blocks[bid]["die"]):
            continue
        dm = dimb(bid)
        if shape == "op":
            b_.append(f'<circle cx="{f(ix + 12)}" cy="{f(iy + 9)}" r="9" fill="#fff" stroke="{DIMS if dm else "#1d4ed8"}" stroke-width="1.2"/>'
                      f'<text x="{f(ix + 12)}" y="{f(iy + 12.6)}" font-size="10" font-weight="800" fill="{DIMT if dm else "#1d4ed8"}" '
                      f'text-anchor="middle">{esc(A.blocks[bid].get("sym", "ƒ"))}</text>')
        elif shape == "ram":                              # SRAM: 小さい格子
            for q in range(12):
                b_.append(f'<rect x="{f(ix + 1 + (q % 4) * 5)}" y="{f(iy + 2 + (q // 4) * 4.5)}" width="5" height="4.5" '
                          f'fill="{DIMF if dm else "#fff"}" stroke="{DIMS if dm else MEMC}" stroke-width="0.8"/>')
        else:
            b_.append(fifo_cells(ix, iy + 3, 22, 10, 4, True, DIMS if dm else FIFO_COL, DIMF if dm else "#fff", 0.9))
    for bid, tx, ty in L.tabs:
        if hidden_die(A.blocks[bid]["die"]) or die_only:
            continue
        col = DIE_STYLE[A.die[A.blocks[bid]["die"]]["idx"] % len(DIE_STYLE)][0]
        b_.append(f'<rect x="{f(tx)}" y="{f(ty)}" width="16" height="10" rx="2" fill="{DIMS if dimb(bid) else col}"/>'
                  f'<text x="{f(tx + 8)}" y="{f(ty + 7.8)}" font-size="7.2" font-weight="800" fill="#fff" text-anchor="middle">{esc(A.blocks[bid]["die"])}</text>')
    for it in L.inner:
        bid, x, y, w, h = it["bid"], it["x"], it["y"], it["w"], it["h"]
        if hidden_die(A.blocks[bid]["die"]):
            continue
        dm = dimb(bid)
        kk = A.die[A.blocks[bid]["die"]]["idx"]
        icol = DIE_STYLE[kk % len(DIE_STYLE)][0] if kk else "#64748b"
        if it["kind"] == "fifo":                          # FIFO: 小さいアイコン(長辺を接した長方形 4 つ)
            b_.append(fifo_cells(x + w / 2 - 11, y + 2.5, 22, 10, 4, True, DIMS if dm else FIFO_COL, DIMF if dm else FIFO_FILL, 0.9))
        else:
            mem = it["kind"] == "mem"
            b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="2" fill="{DIMF if dm else "#ffedd5" if mem else "#fff"}" '
                      f'fill-opacity="{1 if mem else .8}" stroke="{DIMS if dm else MEMC if mem else icol}" stroke-width="{1.1 if mem else 0.8}"/>')
        if it["arrow"] and it["vert"]:
            b_.append(f'<path d="M{f(x + w / 2)},{f(y + h + 1)} v4.5 m-2.4,-2.4 l2.4,2.4 l2.4,-2.4" fill="none" stroke="{DIMS if dm else "#64748b"}" stroke-width="0.9"/>')
        elif it["arrow"]:
            b_.append(f'<path d="M{f(x + w + 1.5)},{f(y + h / 2)} h5 m-2.6,-2.4 l2.6,2.4 l-2.6,2.4" fill="none" stroke="{DIMS if dm else "#64748b"}" stroke-width="0.9"/>')

    # 線。太さ = ビット幅
    def lcol(l):
        if l.get("via"):
            return VIAC
        k = A.die[A.blocks[l["from"]]["die"]]["idx"]
        return DIE_STYLE[k % len(DIE_STYLE)][0] if (k and l["kind"] == "data") else LCOL.get(l["kind"], LCOL["ctrl"])
    lay = [[], [], []]                                    # 薄い線 → ふつうの線 → 登場する線
    for lid, r in sorted(L.route.items(), key=lambda kv: (A.links[kv[0]]["kind"] == "data", A.lw(A.links[kv[0]]))):   # 細い線・制御の線を先に(太いデータの線を上に)
        l = A.links[lid]
        if not shown(l) or (hidden_die(A.blocks[l["from"]]["die"]) and (not l.get("via") or hidden_die(A.blocks[l["to"]]["die"]))):
            continue
        dm = diml(lid)
        act = actl is not None and not dm
        sw = A.lw(l)
        col = DIMS if dm else ACC if act else lcol(l)
        pts = r["pts"]
        both = l.get("dir") in ("both", "half")
        a = (3.6 + sw * 1.3) if A.schem else (4.5 + sw * 1.35)     # 矢じりの長さ(太さに合わせる)
        cut = lambda p, q: (p[0] + (q[0] - p[0]) / max(math.hypot(q[0] - p[0], q[1] - p[1]), 1e-6) * a * .8,
                            p[1] + (q[1] - p[1]) / max(math.hypot(q[0] - p[0], q[1] - p[1]), 1e-6) * a * .8)
        pp = list(pts)
        pp[-1] = cut(pts[-1], pts[-2])                    # 線は矢じりの根元まで
        if both:
            pp[0] = cut(pts[0], pts[1])
        d_ = "M" + " L".join(f"{f(x)},{f(y)}" for x, y in pp)
        o = lay[0 if dm else 2 if act else 1]
        if not dm:
            o.append(f'<path d="{d_}" fill="none" stroke="#fff" stroke-width="{f(sw + 2.4)}" stroke-linejoin="round"/>')
        o.append(f'<path d="{d_}" fill="none" stroke="{col}" stroke-width="{f(sw)}" stroke-linejoin="round"'
                 + (' stroke-dasharray="1.5 2.5"' if l["kind"] == "clk" else ' stroke-dasharray="4.5 2.5"' if (A.schem and l["kind"] == "ctrl") else "") + '/>')
        if r.get("tick"):                                 # バスの斜線
            tx, ty = r["tick"]
            o.append(f'<path d="M{f(tx - 3)},{f(ty + 4.5)} L{f(tx + 3)},{f(ty - 4.5)}" stroke="{col}" stroke-width="1.1"/>')
        for (ex, ey), (qx, qy) in ([(pts[-1], pts[-2])] + ([(pts[0], pts[1])] if both else [])):
            dx, dy = ex - qx, ey - qy
            n = max(math.hypot(dx, dy), 1e-6)
            ux, uy = dx / n, dy / n
            hw = max(2.2, sw * .5 + 1.8) if A.schem else max(2.8, sw * .85 + 1.8)
            o.append(f'<path d="M{f(ex)},{f(ey)} L{f(ex - ux * a - uy * hw)},{f(ey - uy * a + ux * hw)} '
                     f'L{f(ex - ux * a + uy * hw)},{f(ey - uy * a - ux * hw)} z" fill="{col}"/>')
        if l.get("tab"):                                  # ポートの札(プロトコル・ポート名)。バスの帯の縁の内側に置く(帯の中央は名前の行)
            ends = [(pts[0], pts[1], l["from"]), (pts[-1], pts[-2], l["to"])]
            for (ex, ey), (qx, qy), _ in [e for e in ends if A.isbus(A.blocks[e[2]])]:
                n = max(math.hypot(ex - qx, ey - qy), 1e-6)
                ux, uy = (ex - qx) / n, (ey - qy) / n     # 帯の中へ向かう向き
                tw_ = max(20, tw(l["tab"], 6.6, True) + 7)
                cx_, cy_ = ex + ux * (tw_ / 2 + .5 if abs(ux) > .5 else 0), ey + uy * (5.2 if abs(uy) > .5 else 0)
                o.append(f'<rect x="{f(cx_ - tw_ / 2)}" y="{f(cy_ - 4.7)}" width="{f(tw_)}" height="9.4" rx="1.5" fill="{DIMF if dm else TABC}" '
                         f'stroke="{DIMS if dm else "#b45309"}" stroke-width="0.8"/><text x="{f(cx_)}" y="{f(cy_ + 2.4)}" font-size="6.6" '
                         f'font-weight="700" fill="{DIMT if dm else "#422006"}" text-anchor="middle">{esc(l["tab"])}</text>')
        if l.get("mux"):                                  # セレクタ(行き先の手前に台形)
            (ex, ey), (qx, qy) = pts[-1], pts[-2]
            n = max(math.hypot(ex - qx, ey - qy), 1e-6)
            ux, uy = (ex - qx) / n, (ey - qy) / n
            cx_, cy_ = ex - ux * (a + 7), ey - uy * (a + 7)
            vx, vy = -uy, ux
            P = [(cx_ - ux * 4 + vx * 8, cy_ - uy * 4 + vy * 8), (cx_ + ux * 4 + vx * 4.5, cy_ + uy * 4 + vy * 4.5),
                 (cx_ + ux * 4 - vx * 4.5, cy_ + uy * 4 - vy * 4.5), (cx_ - ux * 4 - vx * 8, cy_ - uy * 4 - vy * 8)]
            o.append('<path d="M' + " L".join(f"{f(x)},{f(y)}" for x, y in P) + f' z" fill="#fff" stroke="{col}" stroke-width="1.2"/>')
            o.append(f'<text x="{f(cx_ + vx * 11 + (3 if abs(vx) > .5 else 0))}" y="{f(cy_ + vy * 11 + 2.4)}" font-size="6.4" fill="{col}" '
                     f'text-anchor="{"start" if vx >= 0 else "end"}">{esc(l["mux"])}</text>')
        if l.get("via"):                                  # 線で結ぶ層またぎ: 線の途中に ⊠
            mx, my, ms = r["viapt"]
            o.append(f'<rect x="{f(mx - ms)}" y="{f(my - ms)}" width="{f(2 * ms)}" height="{f(2 * ms)}" fill="#fff" stroke="{col}" '
                     f'stroke-width="{1.2 + ms * .12:.1f}"/><path d="M{f(mx - ms)},{f(my - ms)} l{f(2 * ms)},{f(2 * ms)} '
                     f'M{f(mx + ms)},{f(my - ms)} l{f(-2 * ms)},{f(2 * ms)}" stroke="{col}" stroke-width="1"/>')
    for dx_, dy_, la, lb in L.dots:                       # 分岐の ●
        la_, lb_ = A.links[la], A.links[lb]
        if not shown(la_) or hidden_die(A.blocks[la_["from"]]["die"]):
            continue
        dm = diml(la) and diml(lb)
        act = actl is not None and not dm
        lay[0 if dm else 2 if act else 1].append(f'<circle cx="{f(dx_)}" cy="{f(dy_)}" r="{f(max(2.4, A.lw(la_) / 2 + 1.5))}" fill="{DIMS if dm else ACC if act else lcol(la_)}"/>')
    b_ += lay[0] + lay[1] + lay[2]
    # 層をまたぐ接続の印 ⊠(大きさ = ビット幅)
    for lid, ms_ in L.via.items():
        l = A.links[lid]
        if not shown(l):
            continue
        da, db = A.blocks[l["from"]]["die"], A.blocks[l["to"]]["die"]
        dm = diml(lid)
        act = actl is not None and not dm
        col = DIMS if dm else ACC if act else VIAC
        for n, (mx, my, _, ms) in enumerate(ms_):
            if die_only and len(ms_) > 1 and (da, db)[n] != die_only:
                continue
            if die_only and die_only not in (da, db):
                continue
            b_.append(f'<rect x="{f(mx - ms)}" y="{f(my - ms)}" width="{f(2 * ms)}" height="{f(2 * ms)}" fill="#fff" stroke="{col}" '
                      f'stroke-width="{1.2 + ms * .12:.1f}"/><path d="M{f(mx - ms)},{f(my - ms)} l{f(2 * ms)},{f(2 * ms)} '
                      f'M{f(mx + ms)},{f(my - ms)} l{f(-2 * ms)},{f(2 * ms)}" stroke="{col}" stroke-width="1"/>')
    # クロックの色の印
    for x, y, ck, bid in L.sq:
        if hidden_die(A.blocks[bid]["die"]):
            continue
        b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="5.5" height="5.5" fill="{DIMT if dimb(bid) else A.clk[ck]["col"]}"/>')
    # 文字(線の上に乗るラベルは白い縁をつける)
    for t in L.texts:
        ow, oid = t["owner"]
        if ow == "b":
            if hidden_die(A.blocks[oid]["die"]):
                continue
            dm = dimb(oid)
            col = DIMT if dm else "#fff" if t["role"] == "bus" else INK if t["role"] == "name" else \
                A.clk[t["role"][4:]]["col"] if t["role"].startswith("clk:") else ACC if t["role"] == "more" else "#475569"
        else:
            l = A.links[oid]
            if not shown(l):
                continue
            da, db = A.blocks[l["from"]]["die"], A.blocks[l["to"]]["die"]
            if die_only and die_only not in (da, db) or (die_only and not l.get("via") and da != die_only):
                continue
            dm = diml(oid)
            col = DIMT if dm else "#9a3412" if (actl is not None) else VIAC if l.get("via") else \
                "#0f172a" if l["kind"] == "data" else LCOL.get(l["kind"], GRAY)
        at = f'x="{f(t["x"])}" y="{f(t["y"])}" font-size="{t["fs"]}" text-anchor="{t["anc"]}"' + (' font-weight="700"' if t["bold"] else "")
        if t["rot"]:
            at += f' transform="rotate(-90 {f(t["x"])} {f(t["y"])})"'
        if ow == "l" and not dm:
            b_.append(f'<text {at} fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round">{esc(t["t"])}</text>')
        b_.append(f'<text {at} fill="{col}">{esc(t["t"])}</text>')
    # 動作の番号(同じ線を何度も使う時は、まとめて 1·4·7 と書く)
    for lid, ns in stepno.items():
        if lid in L.route:
            px_, py_ = L.route[lid]["mid"]
        elif lid in L.via:
            v = L.via[lid][0]
            px_, py_ = v[0] - (v[3] + 9) * v[2], v[1]    # ラベルの反対側
        else:
            continue
        t = "·".join(str(n) for n in ns)
        wp = max(13.2, len(t) * 4.9 + 8)
        b_.append(f'<rect x="{f(px_ - wp / 2)}" y="{f(py_ - 6.6)}" width="{f(wp)}" height="13.2" rx="6.6" fill="{ACC}" stroke="#fff" stroke-width="1.3"/>'
                  f'<text x="{f(px_)}" y="{f(py_ + 2.9)}" font-size="8.2" font-weight="800" fill="#fff" text-anchor="middle">{t}</text>')
    return "\n".join(b_)


def step_states(A, steps):
    """動作の表の行(state に出てくるブロック、出てくる順)"""
    rows = []
    for st in steps:
        for k in (st.get("state") or {}):
            if k not in rows:
                rows.append(k)
    return rows


def draw(A, L, mode, title, out, png=False):
    kind = mode["kind"]
    die_only = mode.get("die")
    steps = mode.get("steps") or []
    kinds = set(mode.get("kinds") or A.focus)
    hidden_die = lambda d: die_only is not None and d != die_only
    body = body_svg(A, L, mode)
    # ---- 切り出しの範囲 ----
    crop = mode.get("crop")
    if crop:
        rs = [L.rect[x] for x in crop]
        bx0, by0 = min(r[0] for r in rs) - G + 3, min(r[1] for r in rs) - G + 3
        bx1, by1 = max(r[0] + r[2] for r in rs) + G - 3, max(r[1] + r[3] for r in rs) + G - 3
        bx0, by0, bx1, by1 = max(bx0, 0), max(by0, 0), min(bx1, L.W), min(by1, L.H)
    elif die_only and tuple(L.ext[die_only][2:]) != (A.C, A.R):
        c, r, w, h = L.ext[die_only]
        x, y, pw, ph = L.px(c, r, w, h)
        bx0, by0, bx1, by1 = max(x - G - 4, 0), max(y - G - 4, 0), min(x + pw + G + 4, L.W), min(y + ph + G + 4, L.H)
    else:
        bx0, by0, bx1, by1 = 0, 0, L.W, L.H
    vw, vh = bx1 - bx0, by1 - by0
    part = vw < L.W - 1 or vh < L.H - 1
    zoom = min(2.0, max(1.0, 1500 / vw)) if part else 1.0
    BW, BH = vw * zoom, vh * zoom
    # ---- 上の帯: 題・視点と向き・断面・(切り出しの地図)----
    nd = len(A.dies)
    XS = 318
    H0 = max(42 + 12.5 * nd, 28 + (nd - 1) * 24 + 14 + 12) + 2
    W = max(BW, 1180)
    h_ = [f'<text x="10" y="22" font-size="16" font-weight="800" fill="{INK}">{esc(title)}</text>']
    vtop = A.s.get("view", "top") == "top"
    if A.schem:
        h_.append(f'<text x="10" y="38" font-size="9.5" font-weight="700" fill="{INK}">回路のつながりを示す図(物理配置ではない)'
                  f'<tspan font-weight="400" fill="{GRAY}" dx="10">台形 = セレクタ、縦長の箱 + 三角 = レジスタ(三角の色 = クロック)、丸 = 演算器、橙の箱 = SRAM、● = 分岐、⊠ = 層をまたぐ接続</tspan></text>')
    else:
        vt = "上面(放熱側)から透視" if vtop else "下面(バンプ側)から透視"
        if mode.get("design"):
            bfx, bfy = A.base()
            vt = f"層 {die_only} を回路面から見た向き(設計データの向き)" + ("。全体図とは左右が反転" if L.fx != bfx else "") + ("。全体図とは上下が反転" if L.fy != bfy else "")
        h_.append(f'<text x="10" y="38" font-size="9.5" font-weight="700" fill="{INK}">視点: {esc(vt)}'
                  f'<tspan font-weight="400" fill="{GRAY}" dx="10">▶ と F の字 = 各層の設計データの原点と向き。F が鏡文字なら、その層は裏から見ている</tspan></text>')
    for k, d in enumerate(A.dies):
        mx, my = A.state(d, L.fx, L.fy)
        col, cn = DIE_STYLE[d["idx"] % len(DIE_STYLE)]
        if A.schem:
            frame = f"{cn}の地・{cn}の線" if d["idx"] else "地の色なし・黒の線"
            st = "この層にある回路"
        else:
            frame = f"{cn}の枠 + 斜線 + 「{d['id']}」の札" if d["idx"] else f"{cn}の枠"
            st = "表(回路面)から見ている = 設計データと同じ向き" if not mx and not my else "180°回した向き(鏡像ではない)" if mx and my else \
                 f"裏から透かして見ている = 設計データと{'左右' if mx else '上下'}が反転"
        h_.append(f'<text x="10" y="{f(51.5 + 12.5 * k)}" font-size="9" fill="{DIMT if hidden_die(d["id"]) else col}"><tspan font-weight="700">'
                  f'{esc(d["id"])} {esc(d["name"])}({frame})</tspan>: 回路面 {"上(Face-up)" if d["face"] == "up" else "下(Face-down)"} → {st}'
                  f'<tspan fill="{GRAY}" dx="8">{esc(d.get("note", ""))}</tspan></text>')
    sx0, sy0 = W - XS, 8
    h_.append(f'<text x="{sx0}" y="{sy0 + 8}" font-size="8" font-weight="700" fill="{GRAY}">断面(横から見た積み方)</text>')
    eye = lambda x, y, up: (f'<text x="{x}" y="{y}" font-size="8" font-weight="700" fill="{ACC}" text-anchor="middle">'
                            f'{"▲ 視点(下から)" if up else "視点(上から)▼"}</text>')
    yb = sy0 + 20 + (nd - 1) * 24
    if vtop and not A.schem:
        h_.append(eye(sx0 + 170, sy0 + 8, False))
    full_at = A.s.get("stack_full") or {}
    for k, d in enumerate(A.dies):
        y = yb - k * 24
        col = DIE_STYLE[d["idx"] % len(DIE_STYLE)][0]
        gh = hidden_die(d["id"])
        c, r, w, h = full_at.get(d["id"], d["at"])
        CC = A.s.get("stack_cols", A.C)
        x = sx0 + 6 + 100 * c / CC
        wd = 100 * w / CC
        h_.append(f'<rect x="{f(x)}" y="{y}" width="{f(wd)}" height="14" fill="{"#f8fafc" if gh else "#eef2f7"}" stroke="{DIMS if gh else col}" stroke-width="1"/>')
        fy_ = y + 1.5 if d["face"] == "up" else y + 12.5
        h_.append(f'<line x1="{f(x + 1)}" y1="{fy_}" x2="{f(x + wd - 1)}" y2="{fy_}" stroke="{DIMS if gh else col}" stroke-width="3"/>')
        h_.append(f'<text x="{sx0 + 112}" y="{y + 10}" font-size="8" fill="{DIMT if gh else col}"><tspan font-weight="700">{esc(d["id"])}</tspan> '
                  f'{esc(d["name"])} 回路面{"↑" if d["face"] == "up" else "↓"}</text>')
        for bd in A.s.get("bonds", []):
            if bd["between"][0] == d["id"] and bd["between"][1] in A.die:
                h_.append(f'<text x="{sx0 + 112}" y="{y - 2.5}" font-size="6.8" fill="{VIAC}">⊠ {esc(bd.get("type", ""))} {esc(bd.get("note", ""))}</text>')
                for q in range(8):
                    h_.append(f'<rect x="{f(x + 4 + q * (wd - 10) / 7)}" y="{y - 9}" width="2.4" height="8" fill="{VIAC}" opacity=".7"/>')
    for q in range(11):
        h_.append(f'<circle cx="{f(sx0 + 12 + q * 8.8)}" cy="{yb + 18.5}" r="2.6" fill="#94a3b8"/>')
    h_.append(f'<text x="{sx0 + 112}" y="{yb + 21}" font-size="7" fill="{GRAY}">バンプ(基板側)。太い線の側が回路面</text>')
    if not vtop and not A.schem:
        h_.append(eye(sx0 + 96, yb + 33, True))
        H0 = max(H0, yb + 40)
    if part:                                              # 切り出しの地図
        mw = 150
        ms = mw / L.W
        mx0, my0 = W - XS - mw - 26, 8
        h_.append(f'<rect x="{mx0}" y="{my0}" width="{mw}" height="{f(L.H * ms)}" fill="#fff" stroke="#94a3b8" stroke-width="0.8"/>')
        for b in A.blocks.values():
            if A.die[b["die"]]["idx"] == 0:
                x, y, w, h = L.rect[b["id"]]
                h_.append(f'<rect x="{f(mx0 + x * ms)}" y="{f(my0 + y * ms)}" width="{f(w * ms)}" height="{f(h * ms)}" fill="#e2e8f0"/>')
        h_.append(f'<rect x="{f(mx0 + bx0 * ms)}" y="{f(my0 + by0 * ms)}" width="{f(vw * ms)}" height="{f(vh * ms)}" fill="{ACC}" fill-opacity=".18" '
                  f'stroke="{ACC}" stroke-width="1.4"/>')
        h_.append(f'<text x="{mx0 - 4}" y="{my0 + 9}" font-size="8" fill="{GRAY}" text-anchor="end">全体図のこの範囲 →</text>')
        H0 = max(H0, my0 + L.H * ms + 8)
    # ---- 下の帯: 動作の手順・動作の表・凡例 ----
    ft, y = [], 0
    if steps:
        per = -(-len(steps) // 2) if len(steps) > 3 else len(steps)
        for n, st in enumerate(steps):
            cx_, cy_ = 10 + (n // per) * (W / 2), 14 + (n % per) * 15
            ft.append(f'<circle cx="{cx_ + 7}" cy="{cy_ - 3.5}" r="6.6" fill="{ACC}"/><text x="{cx_ + 7}" y="{cy_ - .6}" font-size="8.2" '
                      f'font-weight="800" fill="#fff" text-anchor="middle">{st.get("_n", n + 1)}</text>'
                      f'<text x="{cx_ + 19}" y="{cy_}" font-size="10.5" fill="{INK}">{esc(st.get("text", ""))}</text>')
        y = per * 15 + 10
        rows = step_states(A, steps)
        if rows:                                          # 動作の表: どのステップで、何を選び、何を読み書きするか
            n = len(steps)
            hwid = max(tw(A.bname(k), 9, True) for k in rows) + 16
            cwid = min(200, (W - 20 - hwid) / n)
            ft.append(f'<text x="10" y="{y + 11}" font-size="8.5" font-weight="700" fill="{GRAY}">動作の表</text>')
            for i, st in enumerate(steps):
                cx_ = 10 + hwid + (i + .5) * cwid
                ft.append(f'<circle cx="{f(cx_)}" cy="{y + 8}" r="6.6" fill="{ACC}"/><text x="{f(cx_)}" y="{y + 10.9}" font-size="8.2" '
                          f'font-weight="800" fill="#fff" text-anchor="middle">{st.get("_n", i + 1)}</text>')
            for ri, key in enumerate(rows):
                yy = y + 18 + ri * 15
                ft.append(f'<text x="10" y="{yy + 10.3}" font-size="9" font-weight="700" fill="{INK}">{esc(A.bname(key))}</text>')
                prev = None
                for i, st in enumerate(steps):
                    v = (st.get("state") or {}).get(key)
                    cx_ = 10 + hwid + i * cwid
                    chg = v is not None and str(v) != prev
                    fill, stroke = ("#fed7aa", "#f97316") if chg else ("#fff7ed", "#fdba74") if v is not None else ("#f8fafc", "#e2e8f0")
                    ft.append(f'<rect x="{f(cx_ + 1)}" y="{yy}" width="{f(cwid - 2)}" height="14" rx="2" fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>')
                    if v is not None:
                        t = wrap(str(v), max(3, int((cwid - 6) / (8.5 * 0.56))))[0]
                        ft.append(f'<text x="{f(cx_ + cwid / 2)}" y="{yy + 10.2}" font-size="8.5" font-weight="{700 if chg else 400}" '
                                  f'fill="{"#7c2d12" if chg else "#9a3412"}" text-anchor="middle">{esc(t)}</text>')
                        prev = str(v)
            y += 18 + len(rows) * 15 + 6
    leg = []
    x = 10
    used_k = {b["kind"] for b in A.blocks.values()}
    for kk, nm in KNAME.items():
        if kk not in used_k or A.schem:
            continue
        leg.append(f'<rect x="{x}" y="{y + 3}" width="10" height="8" fill="{KFILL[kk]}" stroke="{MEMC if kk == "mem" else "#94a3b8"}" stroke-width=".6"/>'
                   f'<text x="{x + 13}" y="{y + 10.5}" font-size="8.5" fill="{INK}">{nm}</text>')
        x += 20 + wlen(nm) * 4.9
    leg.append(fifo_cells(x + 4, y + 2.5, 20, 9, 4, True, FIFO_COL, FIFO_FILL, 0.9) + f'<text x="{x + 28}" y="{y + 10.5}" font-size="8.5" fill="{INK}">FIFO</text>')
    x += 58
    if A.schem:
        for nm, dash in (("データ", ""), ("制御", ' stroke-dasharray="4.5 2.5"')):
            leg.append(f'<line x1="{x}" y1="{y + 7}" x2="{x + 26}" y2="{y + 7}" stroke="{LCOL["ctrl" if dash else "data"]}" stroke-width="1.4"{dash}/>'
                       f'<text x="{x + 30}" y="{y + 10.5}" font-size="8.5" fill="{INK}">{nm}</text>')
            x += 40 + wlen(nm) * 4.9
    for wd_ in ((16, 128, 512, 2048) if A.schem else (64, 256, 1024, 4096)):     # 線の太さの見本
        sw = A.lw(dict(kind="data", width=wd_))
        leg.append(f'<line x1="{x}" y1="{y + 7}" x2="{x + 22}" y2="{y + 7}" stroke="{LCOL["data"]}" stroke-width="{f(sw)}"/>'
                   f'<text x="{x + 26}" y="{y + 10.5}" font-size="8.5" fill="{INK}">{wd_}b</text>')
        x += 34 + len(str(wd_)) * 5.5
    for ck, c in A.clk.items():
        if not any(b.get("clock") == ck for b in A.blocks.values()):
            continue
        leg.append(f'<rect x="{x + 6}" y="{y + 4}" width="6" height="6" fill="{c["col"]}"/>'
                   f'<text x="{x + 15}" y="{y + 10.5}" font-size="8.5" fill="{c["col"]}">{esc(ck)} {esc(c.get("freq", ""))}'
                   f'<tspan fill="{GRAY}">(← {esc(c.get("from", "?"))})</tspan></text>')
        x += 24 + tw(f"{ck} {c.get('freq', '')}(← {c.get('from', '?')})", 8.5)
    hid = sorted({l["kind"] for l in A.links.values()} - kinds)
    notes = ["線: 太さ = ビット幅。ラベル = ビット幅×クロック、向きと帯域(→ 片方向 / ⇄各 両方向でそれぞれこの帯域 / ⇄交互 半二重)、プロトコル。"
             "帯域 = 幅×クロック÷8(GB/s はバイト)。直列は レーン数L×速度。紺 = データ、灰 = 制御。縦のすき間のラベルは縦書き。"]
    if A.schem:
        notes = ["線: 実線 = データ、破線 = 制御。ラベル = 信号名とビット幅(斜線つき。線の太さもビット幅)。● = 分岐(同じ信号が複数の行き先へ)。"
                 "箱の中の小さい字 = 端子の名前。記号の名前は記号の上(上から線が入る記号は下)。",
                 "層: 上の層にある回路は薄い色の地の上に、その層の色の線で描く。⊠ = 層をまたぐ接続。帯域とクロック周波数は arch の表と全体図に書く。"]
    elif nd > 1:
        notes.append("積層: 同じ位置に重ねて描く。上の層は色つきの枠 + 斜線(斜線の色 = 種類。橙 = SRAM)+ 層の名前の札。文字は 1 層目が左上、2 層目が右下。"
                     "⊠ = 層をまたぐ接続(大きさ = ビット幅)。")
    t3 = ""
    if any(A.isbus(b) for b in A.blocks.values()):
        t3 += "帯 = バス(紺 = データ、灰 = 管理)。ブロックは短い矢印でバスにつながる。黄の札 = ポート(プロトコル)。薄い色の枠 = サブシステム。"
    if any(A.small(b) for b in A.blocks.values()):
        t3 += "名前だけの小さい箱 = データの流れに関係の薄いブロック(クロック・電源・温度・BIST)。"
    if hid:
        t3 += f"この図に描いていない線: {'・'.join(hid)}(別図 focus_*)。"
    if kind == "scene":
        t3 += "薄い所 = この説明に登場しない。配置は元の図と同じ。" + ("番号 1·4·7 = その線を 1・4・7 番目で使う。" if steps else "") + \
              ("動作の表の濃い色 = 前と変わった(切り替え)。" if step_states(A, steps) else "")
    if t3:
        notes.append(t3)
    for n, t in enumerate(notes):
        leg.append(f'<text x="10" y="{y + 24 + n * 12}" font-size="8.5" fill="{GRAY}">{esc(t)}</text>')
    FH = y + 24 + len(notes) * 12
    Hh = H0 + BH + FH + 4
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(Hh)}" font-family="{FONT}">',
           '<rect width="100%" height="100%" fill="#fff"/>', defs(), "\n".join(h_),
           f'<svg x="0" y="{f(H0)}" width="{f(BW)}" height="{f(BH)}" viewBox="{f(bx0)} {f(by0)} {f(vw)} {f(vh)}">{body}</svg>',
           f'<g transform="translate(0,{f(H0 + BH)})">' + "\n".join(ft + leg) + '</g>', '</svg>']
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(svg))
    if png:
        to_png(out)
    return out


def draw_frames(A, L, sc, title, out, png=False):
    """動作説明のコマ割り: 同じ図を 1 ステップずつ並べる(配置は変えない)"""
    steps = sc.get("steps", [])
    n = len(steps)
    s = sc.get("frame_scale") or max(0.45, min(1.0, 630 / L.W))
    fw, fh = L.W * s, L.H * s
    cols = sc.get("frame_cols") or max(1, min(n, int(1960 // (fw + 14))))
    capH = 34
    W = max(cols * (fw + 14) + 6, 900)
    H = 44 + -(-n // cols) * (fh + capH + 10) + 6
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" font-family="{FONT}">',
         '<rect width="100%" height="100%" fill="#fff"/>', defs(),
         f'<text x="10" y="22" font-size="16" font-weight="800" fill="{INK}">{esc(title)}</text>',
         f'<text x="10" y="37" font-size="9" fill="{GRAY}">同じ図を 1 ステップずつ並べたもの(配置は変えない)。橙 = そのステップで動く線とブロック、薄い所 = 動かない。'
         f'〔 〕 = そのステップの選択・読み書き</text>']
    rows = step_states(A, steps)
    prev = {}
    for k, st in enumerate(steps):
        x, y = 6 + (k % cols) * (fw + 14), 44 + (k // cols) * (fh + capH + 10)
        body = body_svg(A, L, dict(kind="scene", steps=[dict(st, _n=k + 1)], blocks=sc.get("blocks")))
        o.append(f'<circle cx="{f(x + 9)}" cy="{f(y + 10)}" r="8.5" fill="{ACC}"/><text x="{f(x + 9)}" y="{f(y + 13.8)}" font-size="10.5" '
                 f'font-weight="800" fill="#fff" text-anchor="middle">{k + 1}</text>'
                 f'<text x="{f(x + 23)}" y="{f(y + 14)}" font-size="11.5" font-weight="700" fill="{INK}">{esc(st.get("text", ""))}</text>')
        sts, cx_ = st.get("state") or {}, x + 23
        for key in rows:
            if key not in sts:
                continue
            v = str(sts[key])
            chg = prev.get(key) != v
            prev[key] = v
            t = f"〔{A.bname(key)}: {v}〕"
            o.append(f'<text x="{f(cx_)}" y="{f(y + 28)}" font-size="9.5" font-weight="{700 if chg else 400}" fill="{"#c2410c" if chg else GRAY}">{esc(t)}</text>')
            cx_ += tw(t, 9.5, chg) + 4
        o.append(f'<rect x="{f(x)}" y="{f(y + capH)}" width="{f(fw)}" height="{f(fh)}" fill="#fff" stroke="#cbd5e1" stroke-width="1"/>'
                 f'<svg x="{f(x)}" y="{f(y + capH)}" width="{f(fw)}" height="{f(fh)}" viewBox="0 0 {f(L.W)} {f(L.H)}">{body}</svg>')
    o.append('</svg>')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(o))
    if png:
        to_png(out)
    return out


# ======================= 表(arch.md)=======================
def report(A, L, out):
    M = [f"# {A.s.get('title', 'アーキテクチャ')}", "", "## 積層と向き", "",
         "| 層(下から) | 名前 | 回路面 | この図での見え方 | 備考 |", "|---|---|---|---|---|"]
    for d in A.dies:
        mx, my = A.state(d, *A.base())
        st = "回路の図(物理配置ではない)" if A.schem else "表から(設計データと同じ)" if not mx and not my else "180°回転" if mx and my else \
            f"裏から(設計データと{'左右' if mx else '上下'}反転)"
        M.append(f"| {d['id']} | {d['name']} | {'上 Face-up' if d['face'] == 'up' else '下 Face-down'} | {st} | {d.get('note', '')} |")
    for b in A.s.get("bonds", []):
        M.append(f"\n- {b['between'][0]} ⇔ {b['between'][1]}: {b.get('type', '')} {b.get('note', '')}")
    M += ["", "## クロック", "", "| クロック | 周波数 | 源 | 使うブロック | 備考 |", "|---|---|---|---|---|"]
    for ck, c in A.clk.items():
        us = [b["id"] for b in A.blocks.values() if b.get("clock") == ck]
        M.append(f"| {ck} | {c.get('freq', '')} | {c.get('from', '')} | {len(us)}({', '.join(us[:6])}{' …' if len(us) > 6 else ''}) | {c.get('note', '')} |")
    M += ["", "## ブロック", "", "| ブロック | 層 | 位置 [列, 行, 幅, 高さ] | 種類 | 形 | クロック | 諸元 | 図での扱い |", "|---|---|---|---|---|---|---|---|"]
    for b in A.blocks.values():
        M.append(f"| {b['name']} (`{b['id']}`) | {b['die']} | {b['at']} | {KNAME.get(b['kind'], b['kind'])} | {b.get('shape', '')} | {b.get('clock', '')} | "
                 f"{' / '.join(b.get('info', []))} | {'小さい箱' if A.small(b) else ''}{' 中の回路: 別図 ' + A.detail_of[b['id']] if b['id'] in A.detail_of else ''} |")
    M += ["", "## 接続(帯域 = ビット幅 × クロック ÷ 8)", "",
          "| id | から | へ | 向き | 幅・速度 | 帯域(片方向あたり) | プロトコル | 層をまたぐ | 種類 | 描く図 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for l in A.links.values():
        by, l1, _ = A.bw(l)
        dr = {"both": "両方向(それぞれ)", "half": "半二重(交互)"}.get(l.get("dir"), "片方向 →")
        M.append(f"| {l['id']} | {l['from']} | {l['to']} | {dr} | {l1} | {fmt_bytes(by) if by else ''} | "
                 f"{l.get('proto', '')} | {l.get('via', '')} | {l['kind']}{' セレクタ: ' + str(l['mux']) if l.get('mux') else ''} | "
                 f"{'この図' if l['kind'] in A.focus else '別図 focus_' + l['kind']} |")
    for sc in A.s.get("scenes", []):
        steps = sc.get("steps", [])
        M += ["", f"## 動作: {sc.get('title', sc['id'])}", ""]
        for n, st in enumerate(steps):
            M.append(f"{n + 1}. {st.get('text', '')}(接続: {', '.join(st.get('links', []))})")
        rows = step_states(A, steps)
        if rows:
            M += ["", "| | " + " | ".join(str(n + 1) for n in range(len(steps))) + " |", "|---|" + "---|" * len(steps)]
            for key in rows:
                M.append(f"| {A.bname(key)} | " + " | ".join(str((st.get("state") or {}).get(key, "")) for st in steps) + " |")
    open(out, "w", encoding="utf-8").write("\n".join(M) + "\n")


def lint_layout(A, L):
    for a, b in L.overlaps()[:12]:
        A.lint.append(f"[重なり] 文字が重なっている: 「{a}」と「{b}」(位置を変える・label_at を変える。詰めた方がよい所なら tight: true)")
    for bid, n, r in L.hollow():
        A.lint.append(f"[空き] {bid}({n} マス)の中が空いている(文字が面積の {r * 100:.0f}%)。小さくするか、inside で中身を書く。"
                      f"中の回路を描くなら details で別図にする")


def render(spec, outdir, png=False, name="arch", sub=False):
    A = Arch(spec)
    L = Layout(A, *A.base())
    lint_layout(A, L)
    outs = []
    t0 = spec.get("title", "アーキテクチャ全体図")
    P = lambda s: os.path.join(outdir, f"{name}_{s}.svg")
    same = "(元の図と同じ配置)" if sub else "(全体図と同じ配置)"
    outs.append(draw(A, L, dict(kind="overview"), t0, os.path.join(outdir, f"{name}.svg") if sub else P("overview"), png))
    if len(A.dies) > 1 and (not sub or spec.get("layers")):
        for d in A.dies:
            outs.append(draw(A, L, dict(kind="layer", die=d["id"]), f"{t0} — {d['id']} {d['name']} だけ(全体図と同じ向き)", P(f"layer_{d['id']}"), png))
            if any(A.state(d, L.fx, L.fy)):                # 全体図で裏から見えている層: 設計データの向きの図も出す
                L2 = Layout(A, *A.state(d))
                outs.append(draw(A, L2, dict(kind="layer", die=d["id"], design=True), f"{t0} — {d['id']} {d['name']}(回路面から見た向き = 設計データの向き)",
                                 P(f"layer_{d['id']}_design"), png))
    sc_by = {}
    for sc in spec.get("scenes", []):
        sc_by[sc["id"]] = sc
        tt = sc.get("title", sc["id"])
        outs.append(draw(A, L, dict(kind="scene", steps=sc.get("steps", []), blocks=sc.get("blocks")), tt + same, P(f"scene_{sc['id']}"), png))
        if sc.get("frames"):
            outs.append(draw_frames(A, L, sc, tt + " — コマ割り", P(f"scene_{sc['id']}_frames"), png))
    for cr in spec.get("crops", []):
        sc = sc_by.get(cr.get("scene"))
        mode = dict(kind="scene" if sc else "overview", crop=cr["blocks"], steps=(sc or {}).get("steps", []), blocks=(sc or {}).get("blocks"))
        outs.append(draw(A, L, mode, cr.get("title", cr["id"]), P(f"crop_{cr['id']}"), png))
    for k in sorted({l["kind"] for l in A.links.values()} - set(A.focus)):     # 全体図に描かない種類の線: 別図(同じ配置)
        tt = (spec.get("focus_titles") or {}).get(k, FOCUS_TITLE.get(k, f"別図 — {k}"))
        outs.append(draw(A, L, dict(kind="scene", kinds=A.focus + [k], act_kind=k), tt + same, P(f"focus_{k}"), png))
    report(A, L, os.path.join(outdir, f"{name}.md"))
    for dt in spec.get("details", []):                    # ブロックの中の回路(階層の 1 段下): 別図。動作説明も同じように出す
        used = {b.get("die") for b in dt["blocks"]}
        at = dt.get("stack_at") or {}
        ds = dict(title=dt.get("title", dt["id"]), view=spec.get("view", "top"), grid=dt["grid"], blocks=dt["blocks"], links=dt.get("links", []),
                  clocks=dt.get("clocks", spec.get("clocks", [])), bonds=spec.get("bonds", []), focus=dt.get("focus", spec.get("focus", ["data", "ctrl"])),
                  schematic=dt.get("schematic", False), scenes=dt.get("scenes", []), crops=dt.get("crops", []), details=dt.get("details", []),
                  layers=dt.get("layers", False), stack_cols=A.s.get("stack_cols", A.C),
                  stack_full={d["id"]: (A.s.get("stack_full") or {}).get(d["id"], d["at"]) for d in A.dies},
                  stack=[dict({k: v for k, v in d.items() if k != "idx"}, at=at.get(d["id"], [0, 0] + list(dt["grid"]))) for d in A.dies if d["id"] in used])
        if "small_kinds" in spec:
            ds["small_kinds"] = spec["small_kinds"]
        A2, _, o2 = render(ds, outdir, png, f"{name}_detail_{dt['id']}", sub=True)
        outs += o2
        A.lint += [f"(別図 {dt['id']}) {x}" for x in A2.lint]
    return A, L, outs


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render"); r.add_argument("spec"); r.add_argument("-o", "--out", default="out"); r.add_argument("--png", action="store_true")
    l = sub.add_parser("lint"); l.add_argument("spec")
    g = ap.parse_args()
    spec = load(g.spec)
    if g.cmd == "render":
        A, L, outs = render(spec, g.out, g.png)
        print(f"生成: {len(outs)} 枚 + arch.md → {g.out}/")
        for o in outs:
            print("  " + os.path.basename(o))
    else:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            A, L, outs = render(spec, td)
    print(f"ブロック {len(A.blocks)} / 接続 {len(A.links)} / 層 {len(A.dies)} / マス {A.C}×{A.R}")
    if A.lint:
        print(f"lint {len(A.lint)} 件:")
        for x in A.lint:
            print("  " + x)
    else:
        print("lint 0 件")
    return 1 if A.lint and g.cmd == "lint" else 0


if __name__ == "__main__":
    sys.exit(main())
