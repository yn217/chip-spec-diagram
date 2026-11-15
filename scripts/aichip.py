#!/usr/bin/env python3
"""aichip — AI 向けチップ(SoC ではない、演算タイル + メモリ + 高速 I/O、積層あり)のアーキテクチャ図

  python3 aichip.py render model.yaml -o out/ [--png]     # 全体図・層ごとの図・動作説明・切り出し・arch.md を全部出す
  python3 aichip.py lint   model.yaml

1 つのモデル(model.yaml)から、同じ配置のまま次を作る。図を描き分けない(読む人が混乱しないように):
  arch_overview.svg        全体図。積層は 1 枚に重ねる(1 層目 = 実線の枠、2 層目 = 破線の枠、層をまたぐ接続 = ⊠)
  arch_layer_<層>.svg      層ごとの図(物理配置を見る時)。ほかの層は薄い枠だけ
  arch_layer_<層>_design.svg  その層を回路面から見た向き(設計データの向き)。全体図と左右が反転する層にだけ出す
  arch_scene_<id>.svg      動作説明。全体図と同じ配置で、登場しない所を薄くし、登場する線に ①②… を振る
  arch_crop_<id>.svg       切り出し。全体図の一部を拡大(どこを切り出したかの小さい地図つき)
  arch.md                  ブロック・接続(ビット幅 × クロック = 帯域)・クロック・積層と向きの表

描き方の決まり(R55〜R58。くわしくは diagrams/ai-chip-architecture/GUIDE.md):
  ・マス目に敷き詰める(空きマスを作らない)。ブロックの間は配線用のすき間(32px)だけ
  ・情報は省かない。ブロック = 名前・諸元・クロック名と周波数、線 = ビット幅×クロック=帯域・プロトコル。字は小さくてよい(拡大して読む)
  ・縦のすき間を通る線のラベルは縦書き(-90°)、横のすき間は横書き。文字どうしは重ねない(枠・線の上に乗るのは可)
  ・積層の座標は「上面から見た向き」で書く。回路面が下の層は、設計データと左右が反転して見える → 原点の印と F の字で示す

model.yaml の書き方は examples/aichip/infer_accel.yaml。
"""
import os, sys, re, argparse, collections, heapq
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import esc, wlen, INK, to_png, hz, load, FONT, text_box

GRAY = "#64748b"
CW, CH, G, PAD = 118, 78, 32, 6          # マスの幅・高さ、すき間、外の余白
BEND = 46
KFILL = {"compute": "#dbeafe", "mem": "#ffedd5", "io": "#dcfce7", "clk": "#fef9c3", "ctrl": "#ede9fe", "pwr": "#fee2e2",
         "test": "#f1f5f9", "misc": "#f1f5f9"}
KNAME = {"compute": "演算", "mem": "メモリ", "io": "I/O", "ctrl": "制御", "clk": "クロック", "pwr": "電源", "test": "テスト"}
DIE_STYLE = [("#334155", ""), ("#7c3aed", "5 3"), ("#0f766e", "2 3"), ("#be185d", "8 3 2 3")]
LCOL = {"data": "#1e3a8a", "ctrl": "#64748b", "clk": "#b45309"}
CLKPAL = ["#0369a1", "#b45309", "#15803d", "#be185d", "#6d28d9", "#0f766e", "#a16207"]
ACC = "#d9480f"                           # 動作説明で登場する線・枠
DIMS, DIMF, DIMT = "#d9dfe7", "#fbfcfd", "#c4ccd6"
FS_NAME, FS_INFO, FS_LAB = 10.5, 7.5, 6.8


def f(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def bps(v):
    """'64Gbps' / '112Gb/s' → bit/s"""
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
    return f"{gb * 1000:.3g}MB/s"


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


# ======================= モデル =======================
class Arch:
    def __init__(self, spec):
        self.s, self.lint = spec, []
        self.C, self.R = spec["grid"]
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
                self.links[ll["id"]] = ll
        self.check()

    def bw(self, l):
        """→ (帯域 バイト/秒 or None, 1 行目, 2 行目)"""
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
        l2 = " ".join(x for x in [fmt_bytes(b) if b else "", str(l.get("proto", ""))] if x)
        return b, l1, l2

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
            if b.get("clock") is None:
                L.append(f"[情報] {b['id']}: クロックがない(クロックで動かないブロックは clock: none と書く)")
            elif b["clock"] != "none" and b["clock"] not in self.clk:
                L.append(f"[情報] {b['id']}: クロック {b['clock']} が clocks にない")
        self.own = own
        for d in self.dies:                               # 空き: その層の範囲のうち、ブロックのないマス
            dc, dr, dw, dh = d["at"]
            emp = [(x, y) for y in range(dr, dr + dh) for x in range(dc, dc + dw) if (x, y) not in own[d["id"]]]
            if emp:
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
                             f"途中を引き回すなら far: true と書く")
                    l["far"] = True
            by, l1, l2 = self.bw(l)
            if l["kind"] != "clk" and not by:
                L.append(f"[情報] {l['id']}({l['from']} → {l['to']}): ビット幅とクロック(または レーン数と速度)がなく、帯域がわからない")
            if l.get("bw") and by and abs(bytes_s(l["bw"]) - by) / by > 0.02:
                L.append(f"[帯域] {l['id']}: 書いてある帯域 {l['bw']} と、幅 × クロックからの計算 {fmt_bytes(by)} が合わない")
        ids = set(self.links)
        for sc in self.s.get("scenes", []):
            for st in sc.get("steps", []):
                for x in st.get("links", []):
                    if x not in ids:
                        L.append(f"[動作] {sc['id']}: 接続 {x} がない")
        for cr in self.s.get("crops", []):
            for x in cr.get("blocks", []):
                if x not in self.blocks:
                    L.append(f"[切り出し] {cr['id']}: ブロック {x} がない")

    def state(self, d, fx=False, fy=False):
        """層 d が、モデルの座標(上面から見た向き)を (fx, fy) だけ反転した図でどう見えるか → (左右反転, 上下反転)。
        設計データ(回路面から見た向き)に対して"""
        mx, my = False, False
        if d["face"] == "down":
            mx, my = (True, False) if d["flip"] == "y" else (False, True)
        return mx ^ fx, my ^ fy

    def base(self):
        """全体図の反転: 下面から見る図は、上面から見た向きを左右に反転する"""
        return (self.s.get("view", "top") == "bottom", False)


# ======================= 配置・配線(図の種類によらず 1 回だけ決める)=======================
class Layout:
    def __init__(self, A, fx=False, fy=False):
        self.A, self.fx, self.fy = A, fx, fy
        C, R = A.C, A.R
        self.X0 = self.Y0 = PAD + G
        self.W = 2 * (PAD + G) + C * CW + (C - 1) * G
        self.H = 2 * (PAD + G) + R * CH + (R - 1) * G
        self.xg = [self.X0 + i * (CW + G) - G / 2 for i in range(C + 1)]
        self.yg = [self.Y0 + j * (CH + G) - G / 2 for j in range(R + 1)]
        self.cells, self.rect, self.own = {}, {}, collections.defaultdict(dict)
        for b in A.blocks.values():
            self.cells[b["id"]] = self.flip(b["at"])
            c, r, w, h = self.cells[b["id"]]
            self.rect[b["id"]] = self.px(c, r, w, h)
            for x in range(c, c + w):
                for y in range(r, r + h):
                    self.own[b["die"]][(x, y)] = b["id"]
        self.ext = {d["id"]: self.flip(d["at"]) for d in A.dies}
        self.texts, self.boxes, self.over = [], [], []
        self.route, self.via, self.sq, self.inner, self.lines = {}, {}, [], [], []
        self.place_blocks()
        self.do_routes()
        for r in self.route.values():                     # 線の通り道(ラベルを線に沿って重ねないため)
            for a, b in zip(r["pts"], r["pts"][1:]):
                self.lines.append((min(a[0], b[0]) - 1.6, min(a[1], b[1]) - 1.6, max(a[0], b[0]) + 1.6, max(a[1], b[1]) + 1.6))
        self.place_vias()
        self.place_labels()

    def flip(self, at):
        c, r, w, h = at
        return [self.A.C - c - w if self.fx else c, self.A.R - r - h if self.fy else r, w, h]

    def px(self, c, r, w, h):
        return (self.X0 + c * (CW + G), self.Y0 + r * (CH + G), w * CW + (w - 1) * G, h * CH + (h - 1) * G)

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
        if (w, h) == (self.A.C, self.A.R):
            return (0, 0, self.W, self.H)
        x, y, pw, ph = self.px(c, r, w, h)
        return (x - 8, y - 8, x + pw + 8, y + ph + 8)

    def soft(self, bx, bd=None):
        """文字ではないものとの重なり(線に沿って乗る・その層の外に出る)。lint にはしないが、避ける"""
        b0, b1, b2, b3 = bd or (0, 0, self.W, self.H)
        out = max(0, b0 - bx[0]) + max(0, b1 - bx[1]) + max(0, bx[2] - b2) + max(0, bx[3] - b3)
        return sum(inter(bx, ln) for ln in self.lines) * 0.5 + out * 40

    def place_blocks(self):
        A = self.A
        for b in A.blocks.values():
            x, y, w, h = self.rect[b["id"]]
            k = A.die[b["die"]]["idx"]
            ins = 3 * k
            anc = b.get("label_at") or ["tl", "br", "tr", "bl"][k % 4]
            mu = lambda fs: max(4, int((w - 14 - 2 * ins) / (fs * 0.56)))
            lines = [(t, FS_NAME, "name") for t in wrap(b["name"], int(mu(FS_NAME) * 0.93))]
            for t in b.get("info", []):
                lines += [(u, FS_INFO, "info") for u in wrap(t, mu(FS_INFO))]
            ck = b.get("clock")
            if ck and ck in A.clk:
                lines.append((f"{ck} {A.clk[ck].get('freq', '')}", FS_INFO, "clk:" + ck))
            tot = sum(fs + 2 for _, fs, _ in lines)
            yy = y + 4 + ins if anc[0] == "t" else y + h - 5 - ins - tot
            right = anc[1] == "r"
            xx = x + w - 6 - ins if right else x + 6 + ins
            for t, fs, role in lines:
                yy += fs + 2
                sqw = 8 if role.startswith("clk:") else 0
                self.text(xx if right else xx + sqw, yy - 2, t, fs, "end" if right else "start", role=role, owner=("b", b["id"]),
                          bold=role == "name", tight=b.get("tight", False))
                if sqw:
                    self.sq.append((xx - tw(t, fs) - 8 if right else xx, yy - 2 - fs * 0.78, role[4:], b["id"]))
            ins_ = b.get("inside")
            if ins_:                                      # 中身: 小さい箱を並べる(流れる順)。横(既定)か縦(inside_dir: v)
                n = len(ins_)
                flow = b.get("inside_flow", True)
                if b.get("inside_dir") == "v":
                    top = yy + 5 if anc[0] == "t" else y + 6 + ins
                    bot = y + h - 6 - ins if anc[0] == "t" else y + h - 5 - ins - tot - 5
                    gap = 7 if flow else 4
                    bh = min(40, (bot - top - gap * (n - 1)) / n)
                    gap = min(16, (bot - top - bh * n) / max(n - 1, 1))       # 高さいっぱいに均等に置く(下を空けない)
                    cellsz = [(x + 7 + ins, top + q * (bh + gap), w - 14 - 2 * ins, bh) for q in range(n)]
                else:
                    f0, f1 = b.get("inside_at", [0, 1])
                    gap, bh = 9, 27
                    x0_, x1_ = x + 8 + ins + (w - 16 - 2 * ins) * f0, x + 8 + ins + (w - 16 - 2 * ins) * f1
                    bw_ = (x1_ - x0_ - gap * (n - 1)) / n
                    by = y + h * b.get("inside_y", 0.47 if k == 0 else 0.66) - bh / 2
                    cellsz = [(x0_ + q * (bw_ + gap), by, bw_, bh) for q in range(n)]
                for q, (t, (cx_, cy_, cw_, ch_)) in enumerate(zip(ins_, cellsz)):
                    self.inner.append((b["id"], cx_, cy_, cw_, ch_, (q < n - 1 and flow), b.get("inside_dir") == "v"))
                    ls = str(t).split("|")
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

    def ports(self, bid):
        c, r, w, h = self.cells[bid]
        x, y, pw, ph = self.rect[bid]
        cy = lambda rr: self.Y0 + rr * (CH + G) + CH / 2
        cx = lambda cc: self.X0 + cc * (CW + G) + CW / 2
        P = []
        for rr in range(r, r + h):
            P.append((("V", c, rr), cy(rr), x)); P.append((("V", c + w, rr), cy(rr), x + pw))
        for cc in range(c, c + w):
            P.append((("H", cc, r), cx(cc), y)); P.append((("H", cc, r + h), cx(cc), y + ph))
        return P

    def direct(self, a, b):
        """向かい合う辺がすき間 1 本をはさんで重なっていれば → (向き, すき間の番号, 重なりの範囲, a の辺, b の辺)"""
        (ac, ar, aw, ah), (bc, br, bw_, bh) = self.cells[a], self.cells[b]
        (ax, ay, apw, aph), (bx, by, bpw, bph) = self.rect[a], self.rect[b]
        lo, hi = max(ay, by), min(ay + aph, by + bph)
        if hi - lo >= 20:
            if ac + aw == bc:
                return ("H", bc, lo, hi, ax + apw, bx)
            if bc + bw_ == ac:
                return ("H", ac, lo, hi, ax, bx + bpw)
        lo, hi = max(ax, bx), min(ax + apw, bx + bpw)
        if hi - lo >= 20:
            if ar + ah == br:
                return ("V", br, lo, hi, ay + aph, by)
            if br + bh == ar:
                return ("V", ar, lo, hi, ay, by + bph)
        return None

    def find(self, die, a, b, usage):
        PA, PB = self.ports(a), self.ports(b)
        PA = [p for p in PA if self.allowed(die, p[0])]
        PB = [p for p in PB if self.allowed(die, p[0])]
        wt = lambda e: 1 + 0.45 * usage[e]
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
                if not (0 <= m[0] <= C and 0 <= m[1] <= R) or not self.allowed(die, e2):
                    continue
                c2 = c + (CH + G if e2[0] == "V" else CW + G) * wt(e2) + (BEND if e2[0] != o else 0)
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
        if PB[kb][0] != edges[-1] or len(edges) > 1 or nodes:
            if not (len(edges) == 1 and not nodes):
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
        usage, used = collections.Counter(), collections.defaultdict(set)
        dgrp = collections.defaultdict(list)
        order = sorted(A.links.values(), key=lambda l: {"data": 0, "ctrl": 1, "clk": 2}[l["kind"]])
        for l in order:
            if l.get("bad") or l.get("via"):
                continue
            a, b = l["from"], l["to"]
            d = self.direct(a, b)
            if d:
                self.route[l["id"]] = dict(direct=d)
                dgrp[(d[0], d[1], d[2], d[3])].append(l["id"])
                continue
            r = self.find(A.blocks[a]["die"], a, b, usage)
            if not r:
                A.lint.append(f"[経路] {l['id']}: {a} → {b} の通り道がない") if (self.fx, self.fy) == A.base() else None
                continue
            for run in r["runs"]:
                s = 0
                while any(s in used[e] for e in run["edges"]):
                    s += 1
                run["slot"] = s
                for e in run["edges"]:
                    used[e].add(s); usage[e] += 1
            self.route[l["id"]] = r
        ns = collections.Counter()
        for e, ss in used.items():
            key = (e[0], e[1] if e[0] == "V" else e[2])
            ns[key] = max(ns[key], max(ss) + 1)
        pg = collections.defaultdict(list)                # 同じマスの辺から出る線: 位置をずらす
        for lid, r in self.route.items():
            if "runs" in r:
                pg[(A.links[lid]["from"], r["sp"][0])].append((lid, "s"))
                pg[(A.links[lid]["to"], r["tp"][0])].append((lid, "t"))
        for (bid, e), lst in pg.items():
            span = CH if e[0] == "V" else CW
            d = min(11, (span - 16) / len(lst))
            for k, (lid, w) in enumerate(lst):
                self.route[lid][w + "off"] = (k - (len(lst) - 1) / 2) * d
        for lid, r in self.route.items():
            if "direct" in r:
                o, gi, lo, hi, ea, eb = r["direct"]
                grp = dgrp[(o, gi, lo, hi)]
                p = lo + (hi - lo) * (grp.index(lid) + 1) / (len(grp) + 1)
                r["pts"] = [(ea, p), (eb, p)] if o == "H" else [(p, ea), (p, eb)]
                continue
            co = []
            for run in r["runs"]:
                o, line = run["key"]
                n = ns[run["key"]]
                tr = min(6.0, (G - 9) / max(n, 1))
                co.append((self.xg[line] if o == "V" else self.yg[line]) + (run["slot"] - (n - 1) / 2) * tr)
            (se, sp, ss), (te, tp, ts) = r["sp"], r["tp"]
            sp, tp = sp + r.get("soff", 0), tp + r.get("toff", 0)
            pts = [(ss, sp) if se[0] == "V" else (sp, ss)]
            pts.append((co[0], sp) if se[0] == "V" else (sp, co[0]))
            for k in range(len(co) - 1):
                pts.append((co[k], co[k + 1]) if r["runs"][k]["key"][0] == "V" else (co[k + 1], co[k]))
            pts.append((co[-1], tp) if te[0] == "V" else (tp, co[-1]))
            pts.append((ts, tp) if te[0] == "V" else (tp, ts))
            r["pts"] = [p for k, p in enumerate(pts) if k == 0 or abs(p[0] - pts[k - 1][0]) + abs(p[1] - pts[k - 1][1]) > 0.3]

    # ---- 層をまたぐ接続の印 ----
    def place_vias(self):
        A = self.A
        for l in A.links.values():
            if not l.get("via") or l.get("bad"):
                continue
            _, l1, l2 = A.bw(l)
            ra, rb = self.rect[l["from"]], self.rect[l["to"]]
            if l.get("far"):
                self.via[l["id"]] = [self.mark(l, r, [f"{l['via']} {l1}", f"{l2} ⇄{o}"]) for r, o in ((ra, l["to"]), (rb, l["from"]))]
                continue
            ox, oy = max(ra[0], rb[0]), max(ra[1], rb[1])
            ow, oh = min(ra[0] + ra[2], rb[0] + rb[2]) - ox, min(ra[1] + ra[3], rb[1] + rb[3]) - oy
            self.via[l["id"]] = [self.mark(l, (ox, oy, ow, oh), [f"{l['via']} {l1}", l2])]

    def mark(self, l, r, lines):
        x, y, w, h = r
        lines = [t for t in lines if t]
        tl = max(tw(t, FS_LAB) for t in lines)
        best = None
        for n, (fx_, fy_) in enumerate([(a, b) for b in (0.5, 0.36, 0.64, 0.22, 0.78) for a in (0.5, 0.3, 0.7, 0.14, 0.86)]):
            mx, my = x + w * fx_, y + h * fy_
            for side in (1, -1):
                if side == 1 and mx + 8 + tl > x + w - 3 or side == -1 and mx - 8 - tl < x + 3:
                    continue
                bxs = [text_box(mx + side * 8, my - 1 + k * (FS_LAB + 1.2) - (len(lines) - 1) * (FS_LAB + 1.2) / 2 + FS_LAB * .35, FS_LAB, t,
                                "start" if side == 1 else "end") for k, t in enumerate(lines)]
                bxs.append((mx - 6, my - 6, mx + 6, my + 6))
                sc = sum(self.hit(b) for b in bxs) * 10 + n + sum(self.soft(b) for b in bxs[:-1])
                if best is None or sc < best[0]:
                    best = (sc, mx, my, side, bxs)
        if best is None:
            best = (0, x + w / 2, y + h / 2, 1, [])
        sc, mx, my, side, bxs = best
        if sc >= 10:
            self.over.append(l["id"])
        for k, t in enumerate(lines):
            self.text(mx + side * 8, my - 1 + k * (FS_LAB + 1.2) - (len(lines) - 1) * (FS_LAB + 1.2) / 2 + FS_LAB * .35, t, FS_LAB,
                      "start" if side == 1 else "end", role="lab", owner=("l", l["id"]), tight=l.get("tight", False))
        self.boxes.append(((mx - 6, my - 6, mx + 6, my + 6), ("l", l["id"]), "⊠", True))
        return (mx, my, side)

    # ---- 線のラベル ----
    def place_labels(self):
        A = self.A
        for lid, r in self.route.items():
            l = A.links[lid]
            _, l1, l2 = A.bw(l)
            one = " ".join(x for x in (l1, l2) if x)
            if not one:
                r["mid"] = r["pts"][len(r["pts"]) // 2]
                continue
            two = [x for x in (l1, l2) if x]
            pts = r["pts"]
            bd = self.bounds(A.blocks[l["from"]]["die"])
            segs = sorted(((abs(a[0] - b[0]) + abs(a[1] - b[1]), a, b) for a, b in zip(pts, pts[1:])), key=lambda s: -s[0])
            cands = []
            for n, (ln, a, b) in enumerate(segs):
                horiz = abs(a[1] - b[1]) < 0.5
                # ラベルの向きは、置く場所のすき間で決める: 縦のすき間 = 縦書き、横のすき間 = 横書き
                if "direct" in r:
                    o, gi, lo, hi, _, _ = r["direct"]
                    cx_, cy_ = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                    for lines in ([one], two):
                        tl = max(tw(t, FS_LAB) for t in lines)
                        for side, far_ in ((-1, 0), (1, 0), (-1, 1), (1, 1), (-1, 2), (1, 2)):
                            n = far_ * 0.8 + (0 if side == 1 else 0.1)
                            if o == "H":                  # 横向きの短い矢印(縦のすき間): 矢印の上か下に、縦書き
                                yc = cy_ + side * (5 + tl / 2 + far_ * 26)
                                xs = [cx_ + (k - (len(lines) - 1) / 2) * (FS_LAB + 1) + FS_LAB * .3 for k in range(len(lines))]
                                cands.append((n + (0 if len(lines) == 1 else .5), [(x, yc, t, "middle", True) for x, t in zip(xs, lines)], (cx_, cy_)))
                            else:                         # 縦向きの短い矢印(横のすき間): 矢印の右か左に、横書き
                                ys = [cy_ + (k - (len(lines) - 1) / 2) * (FS_LAB + 1) + FS_LAB * .35 for k in range(len(lines))]
                                cands.append((n + (0 if len(lines) == 1 else .5), [(cx_ + side * (4 + far_ * 26), y, t, "start" if side == 1 else "end", False)
                                                                                   for y, t in zip(ys, lines)], (cx_, cy_)))
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
                            if horiz:
                                ys = [py_ - 2.2 - (len(lines) - 1 - k) * (FS_LAB + 1) for k in range(len(lines))] if side == -1 else \
                                     [py_ + 2.2 + FS_LAB * .8 + k * (FS_LAB + 1) for k in range(len(lines))]
                                cands.append((pen, [(px_, y, t, "middle", False) for y, t in zip(ys, lines)], (px_, py_)))
                            else:
                                xs = [px_ - 2.4 - FS_LAB * .2 - (len(lines) - 1 - k) * (FS_LAB + 1) for k in range(len(lines))] if side == -1 else \
                                     [px_ + 2.4 + FS_LAB * .8 + k * (FS_LAB + 1) for k in range(len(lines))]
                                cands.append((pen, [(x, py_, t, "middle", True) for x, t in zip(xs, lines)], (px_, py_)))
            best = None
            for pen, items, mid in cands:
                bxs = [text_box(x, y, FS_LAB, t, anc, rot) for x, y, t, anc, rot in items]
                ov = sum(self.hit(b) for b in bxs)
                sc = ov * 10 + pen + sum(self.soft(b, bd) for b in bxs)
                if best is None or sc < best[0]:
                    best = (sc, ov, items, mid)
                if sc < 0.2:
                    break
            sc, ov, items, mid = best
            if ov > 0:
                self.over.append(lid)
            for x, y, t, anc, rot in items:
                self.text(x, y, t, FS_LAB, anc, rot, role="lab", owner=("l", lid), tight=l.get("tight", False))
            r["mid"] = mid
        for lid, r in self.route.items():                 # 動作説明の番号を置く点: 線の上で、文字に重ならない所
            pts = r["pts"]
            tot = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(pts, pts[1:]))
            for fr in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.12, 0.88):
                d, p = tot * fr, pts[0]
                for a, b in zip(pts, pts[1:]):
                    ln = abs(a[0] - b[0]) + abs(a[1] - b[1])
                    if d <= ln and ln > 0:
                        p = (a[0] + (b[0] - a[0]) * d / ln, a[1] + (b[1] - a[1]) * d / ln)
                        break
                    d -= ln
                if self.hit((p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7)) == 0:
                    r["mid"] = p
                    break
            else:                                         # 短い線: 線の中央から横にずらして、文字のない側に置く
                a, b = pts[0], pts[-1]
                c = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                horiz = abs(a[1] - b[1]) < abs(a[0] - b[0])
                cs = [(c[0] + (0 if horiz else d), c[1] + (d if horiz else 0)) for d in (-11, 11, -22, 22)]
                r["mid"] = min(cs, key=lambda q: self.hit((q[0] - 7, q[1] - 7, q[0] + 7, q[1] + 7)))

    def hollow(self):
        """中が空いている 1 層目のブロック(3 マス以上で、文字と中身の箱が面積の 7% 未満)"""
        out = []
        for b in self.A.blocks.values():
            if self.A.die[b["die"]]["idx"] != 0 or b["at"][2] * b["at"][3] < 3:
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
                if a[1] == b[1] or (a[3] and b[3]):
                    continue
                ar = inter(a[0], b[0])
                if ar > 0 and ar / min((a[0][2] - a[0][0]) * (a[0][3] - a[0][1]), (b[0][2] - b[0][0]) * (b[0][3] - b[0][1])) > 0.12:
                    if not (a[3] or b[3]):
                        out.append((a[2], b[2]))
        return out


# ======================= 描く =======================
def draw(A, L, mode, title, out, png=False):
    """mode: kind = overview / layer / scene、die、steps、crop(ブロックの並び)"""
    kind = mode["kind"]
    die_only = mode.get("die")
    steps = mode.get("steps") or []
    actl, actb, stepno = None, None, collections.defaultdict(list)
    if kind == "scene":
        actl, actb = set(), set(mode.get("blocks") or [])
        for n, st in enumerate(steps):
            for x in st.get("links", []):
                actl.add(x); stepno[x].append(n + 1)
                actb |= {A.links[x]["from"], A.links[x]["to"]}
            actb |= set(st.get("blocks", []))
    dimb = lambda bid: actb is not None and bid not in actb
    diml = lambda lid: actl is not None and lid not in actl
    hidden_die = lambda d: die_only is not None and d != die_only
    b_ = []
    # 層の外形・原点の印
    for d in A.dies:
        c, r, w, h = L.ext[d["id"]]
        x, y, pw, ph = L.px(c, r, w, h)
        full = (w, h) == (A.C, A.R)
        e = G - 5 - 3 * d["idx"] if full else 7
        col, dash = DIE_STYLE[d["idx"] % len(DIE_STYLE)]
        ghost = hidden_die(d["id"])
        b_.append(f'<rect x="{f(x - e)}" y="{f(y - e)}" width="{f(pw + 2 * e)}" height="{f(ph + 2 * e)}" rx="5" fill="none" '
                  f'stroke="{DIMS if ghost else col}" stroke-width="{1 if ghost else 1.6}"' + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')
        if ghost:
            continue
        mx, my = A.state(d, L.fx, L.fy)
        ox, oy = (x + pw + e if mx else x - e), (y - e if my else y + ph + e)
        sx, sy = (-1 if mx else 1), (-1 if my else 1)
        b_.append(f'<path d="M{f(ox)},{f(oy)} l{9 * sx},0 l{-9 * sx},{-9 * sy} z" fill="{col}"/>')
        gy = oy - 4 * sy if full else oy + 13 * sy        # 小さい層は、外形のすぐ外(すき間)に書く
        b_.append(f'<g transform="translate({f(ox + 12 * sx)},{f(gy)}) scale({sx},{sy})"><path d="M0,0 v-10 h6 M0,-5.5 h4" '
                  f'fill="none" stroke="{col}" stroke-width="1.8"/></g>')
        b_.append(f'<text x="{f(ox + 22 * sx)}" y="{f(gy - 1 * sy + (5 if my else 0))}" font-size="6.8" fill="{col}" '
                  f'text-anchor="{"end" if mx else "start"}">{esc(d["id"])} 原点{"(裏から見ている → F が鏡文字)" if mx != my else ""}</text>')
    # ブロックの枠
    for b in A.blocks.values():
        x, y, w, h = L.rect[b["id"]]
        k = A.die[b["die"]]["idx"]
        col, dash = DIE_STYLE[k % len(DIE_STYLE)]
        ins = 3 * k
        if hidden_die(b["die"]):
            b_.append(f'<rect x="{f(x + ins)}" y="{f(y + ins)}" width="{f(w - 2 * ins)}" height="{f(h - 2 * ins)}" rx="3" fill="none" '
                      f'stroke="{DIMS}" stroke-width="0.8" stroke-dasharray="3 3"/>')
            continue
        dm = dimb(b["id"])
        act = actb is not None and not dm
        op = "" if k == 0 or die_only else ' fill-opacity="0"'
        b_.append(f'<rect x="{f(x + ins)}" y="{f(y + ins)}" width="{f(w - 2 * ins)}" height="{f(h - 2 * ins)}" rx="3" '
                  f'fill="{DIMF if dm else KFILL.get(b["kind"], KFILL["misc"])}"{op} stroke="{DIMS if dm else ACC if act else col}" '
                  f'stroke-width="{2.2 if act else 1.3}"' + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')
    for bid, x, y, w, h, arrow, vert in L.inner:
        if hidden_die(A.blocks[bid]["die"]):
            continue
        dm = dimb(bid)
        kk = A.die[A.blocks[bid]["die"]]["idx"]
        icol, idash = DIE_STYLE[kk % len(DIE_STYLE)] if kk else ("#64748b", "")
        b_.append(f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="2" fill="{"#fff" if not dm else DIMF}" fill-opacity=".8" '
                  f'stroke="{DIMS if dm else icol}" stroke-width="0.8"' + (' stroke-dasharray="3 2"' if idash else "") + '/>')
        if arrow and vert:
            b_.append(f'<path d="M{f(x + w / 2)},{f(y + h + 1)} v4.5 m-2.4,-2.4 l2.4,2.4 l2.4,-2.4" fill="none" stroke="{DIMS if dm else "#64748b"}" stroke-width="0.9"/>')
        elif arrow:
            b_.append(f'<path d="M{f(x + w + 1.5)},{f(y + h / 2)} h5 m-2.6,-2.4 l2.6,2.4 l-2.6,2.4" fill="none" stroke="{DIMS if dm else "#64748b"}" stroke-width="0.9"/>')
    # 線
    def lstyle(l, dm, act):
        by, _, _ = A.bw(l)
        gb = (by or 0) / 1e9
        sw = 0.9 if l["kind"] != "data" else 1.1 if gb < 10 else 1.6 if gb < 100 else 2.2 if gb < 500 else 2.9
        col = DIMS if dm else ACC if act else LCOL[l["kind"]]
        dash = DIE_STYLE[A.die[A.blocks[l["from"]]["die"]]["idx"] % len(DIE_STYLE)][1]
        if l["kind"] == "clk":
            dash = "1.5 2.5"
        return col, sw + (1.0 if act else 0), dash
    lay = [[], [], []]                                    # 薄い線 → ふつうの線 → 登場する線
    for lid, r in L.route.items():
        l = A.links[lid]
        if hidden_die(A.blocks[l["from"]]["die"]):
            continue
        dm = diml(lid)
        act = actl is not None and not dm
        col, sw, dash = lstyle(l, dm, act)
        pts = r["pts"]
        d_ = "M" + " L".join(f"{f(x)},{f(y)}" for x, y in pts)
        o = lay[0 if dm else 2 if act else 1]
        if not dm:
            o.append(f'<path d="{d_}" fill="none" stroke="#fff" stroke-width="{sw + 2.4}" stroke-linejoin="round"/>')
        o.append(f'<path d="{d_}" fill="none" stroke="{col}" stroke-width="{sw}" stroke-linejoin="round"'
                 + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')
        for (ex, ey), (qx, qy) in ([(pts[-1], pts[-2])] + ([(pts[0], pts[1])] if l.get("dir") == "both" else [])):
            dx, dy = ex - qx, ey - qy
            n = max((dx * dx + dy * dy) ** .5, 1e-6)
            ux, uy = dx / n, dy / n
            a = 5.2 + sw * .7
            o.append(f'<path d="M{f(ex)},{f(ey)} L{f(ex - ux * a - uy * a * .5)},{f(ey - uy * a + ux * a * .5)} '
                     f'L{f(ex - ux * a + uy * a * .5)},{f(ey - uy * a - ux * a * .5)} z" fill="{col}"/>')
    b_ += lay[0] + lay[1] + lay[2]
    # 層をまたぐ接続の印 ⊠
    for lid, ms in L.via.items():
        l = A.links[lid]
        da, db = A.blocks[l["from"]]["die"], A.blocks[l["to"]]["die"]
        dm = diml(lid)
        act = actl is not None and not dm
        col = DIMS if dm else ACC if act else "#7c2d12"
        for n, (mx, my, _) in enumerate(ms):
            if die_only and (da, db)[n if len(ms) > 1 else 0] != die_only and len(ms) > 1:
                continue
            if die_only and die_only not in (da, db):
                continue
            b_.append(f'<rect x="{f(mx - 5)}" y="{f(my - 5)}" width="10" height="10" fill="#fff" stroke="{col}" stroke-width="{1.8 if act else 1.3}"/>'
                      f'<path d="M{f(mx - 5)},{f(my - 5)} l10,10 M{f(mx + 5)},{f(my - 5)} l-10,10" stroke="{col}" stroke-width="1"/>')
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
            col = DIMT if dm else INK if t["role"] == "name" else A.clk[t["role"][4:]]["col"] if t["role"].startswith("clk:") else "#475569"
        else:
            l = A.links[oid]
            da, db = A.blocks[l["from"]]["die"], A.blocks[l["to"]]["die"]
            if die_only and die_only not in (da, db) or (die_only and not l.get("via") and da != die_only):
                continue
            dm = diml(oid)
            col = DIMT if dm else "#9a3412" if (actl is not None) else "#7c2d12" if l.get("via") else LCOL[l["kind"]] if l["kind"] != "data" else "#0f172a"
        at = f'x="{f(t["x"])}" y="{f(t["y"])}" font-size="{t["fs"]}" text-anchor="{t["anc"]}"' + (' font-weight="700"' if t["bold"] else "")
        if t["rot"]:
            at += f' transform="rotate(-90 {f(t["x"])} {f(t["y"])})"'
        if ow == "l" and not dm:
            b_.append(f'<text {at} fill="none" stroke="#fff" stroke-width="2.4" stroke-linejoin="round">{esc(t["t"])}</text>')
        b_.append(f'<text {at} fill="{col}">{esc(t["t"])}</text>')
    # 動作の番号
    for lid, ns in stepno.items():
        if lid in L.route:
            px_, py_ = L.route[lid]["mid"]
        elif lid in L.via:
            px_, py_ = L.via[lid][0][0] - 13 * L.via[lid][0][2], L.via[lid][0][1]      # ラベルの反対側
        else:
            continue
        for k, n in enumerate(ns):
            cx_ = px_ + (k - (len(ns) - 1) / 2) * 14
            b_.append(f'<circle cx="{f(cx_)}" cy="{f(py_)}" r="6.6" fill="{ACC}" stroke="#fff" stroke-width="1.3"/>'
                      f'<text x="{f(cx_)}" y="{f(py_ + 2.9)}" font-size="8.2" font-weight="800" fill="#fff" text-anchor="middle">{n}</text>')
    body = "\n".join(b_)
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
    zoom = min(2.0, max(1.0, 1500 / vw)) if (crop or vw < 1100) else 1.0
    BW, BH = vw * zoom, vh * zoom
    # ---- 上の帯: 題・視点と向き・断面・(切り出しの地図)----
    nd = len(A.dies)
    XS = 318                                              # 断面の幅
    H0 = max(42 + 12.5 * nd, 28 + (nd - 1) * 24 + 14 + 12) + 2
    W = max(BW, 1180)
    h_ = [f'<text x="10" y="22" font-size="16" font-weight="800" fill="{INK}">{esc(title)}</text>']
    vtop = A.s.get("view", "top") == "top"
    vt = "上面(放熱側)から透視" if vtop else "下面(バンプ側)から透視"
    if mode.get("design"):
        bfx, bfy = A.base()
        vt = f"層 {die_only} を回路面から見た向き(設計データの向き)" + ("。全体図とは左右が反転" if L.fx != bfx else "") + ("。全体図とは上下が反転" if L.fy != bfy else "")
    h_.append(f'<text x="10" y="38" font-size="9.5" font-weight="700" fill="{INK}">視点: {esc(vt)}'
              f'<tspan font-weight="400" fill="{GRAY}" dx="10">▶ と F の字 = 各層の設計データの原点と向き。F が鏡文字なら、その層は裏から見ている</tspan></text>')
    for k, d in enumerate(A.dies):
        mx, my = A.state(d, L.fx, L.fy)
        col = DIE_STYLE[d["idx"] % len(DIE_STYLE)][0]
        st = "表(回路面)から見ている = 設計データと同じ向き" if not mx and not my else "180°回した向き(鏡像ではない)" if mx and my else \
             f"裏から透かして見ている = 設計データと{'左右' if mx else '上下'}が反転"
        frame = ["実線の枠", "破線の枠", "点線の枠", "鎖線の枠"][d["idx"] % 4]
        h_.append(f'<text x="10" y="{f(51.5 + 12.5 * k)}" font-size="9" fill="{DIMT if hidden_die(d["id"]) else col}"><tspan font-weight="700">'
                  f'{esc(d["id"])} {esc(d["name"])}({frame})</tspan>: 回路面 {"上(Face-up)" if d["face"] == "up" else "下(Face-down)"} → {st}'
                  f'<tspan fill="{GRAY}" dx="8">{esc(d.get("note", ""))}</tspan></text>')
    # 断面(下から順に積む)
    sx0, sy0 = W - XS, 8
    h_.append(f'<text x="{sx0}" y="{sy0 + 8}" font-size="8" font-weight="700" fill="{GRAY}">断面(横から見た積み方)</text>')
    eye = lambda y, up: (f'<text x="{sx0 + 96}" y="{y}" font-size="8" font-weight="700" fill="{ACC}" text-anchor="middle">'
                         f'{"▲ 視点(下から)" if up else "視点(上から)▼"}</text>')
    yb = sy0 + 20 + (nd - 1) * 24 + (0 if vtop else 0)
    if vtop:
        h_.append(eye(sy0 + 8, False).replace(f'x="{sx0 + 96}"', f'x="{sx0 + 170}"'))
    for k, d in enumerate(A.dies):
        y = yb - k * 24
        col = DIE_STYLE[d["idx"] % len(DIE_STYLE)][0]
        gh = hidden_die(d["id"])
        c, r, w, h = d["at"]
        x = sx0 + 6 + 100 * c / A.C
        wd = 100 * w / A.C
        h_.append(f'<rect x="{f(x)}" y="{y}" width="{f(wd)}" height="14" fill="{"#f8fafc" if gh else "#eef2f7"}" stroke="{DIMS if gh else col}" stroke-width="1"/>')
        fy_ = y + 1.5 if d["face"] == "up" else y + 12.5
        h_.append(f'<line x1="{f(x + 1)}" y1="{fy_}" x2="{f(x + wd - 1)}" y2="{fy_}" stroke="{DIMS if gh else col}" stroke-width="3"/>')
        h_.append(f'<text x="{sx0 + 112}" y="{y + 10}" font-size="8" fill="{DIMT if gh else col}"><tspan font-weight="700">{esc(d["id"])}</tspan> '
                  f'{esc(d["name"])} 回路面{"↑" if d["face"] == "up" else "↓"}</text>')
        for bd in A.s.get("bonds", []):
            if bd["between"][0] == d["id"]:
                h_.append(f'<text x="{sx0 + 112}" y="{y - 2.5}" font-size="6.8" fill="#7c2d12">⊠ {esc(bd.get("type", ""))} {esc(bd.get("note", ""))}</text>')
                for q in range(8):
                    h_.append(f'<rect x="{f(x + 4 + q * (wd - 10) / 7)}" y="{y - 9}" width="2.4" height="8" fill="#7c2d12" opacity=".7"/>')
    for q in range(11):
        h_.append(f'<circle cx="{f(sx0 + 12 + q * 8.8)}" cy="{yb + 18.5}" r="2.6" fill="#94a3b8"/>')
    h_.append(f'<text x="{sx0 + 112}" y="{yb + 21}" font-size="7" fill="{GRAY}">バンプ(基板側)。太い線の側が回路面</text>')
    if not vtop:
        h_.append(eye(yb + 33, True))
        H0 = max(H0, yb + 40)
    # 切り出しの地図
    if crop or zoom > 1:
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
    # ---- 下の帯: 動作の手順・凡例 ----
    ft, y = [], 0
    if steps:
        per = -(-len(steps) // 2) if len(steps) > 3 else len(steps)
        for n, st in enumerate(steps):
            cx_, cy_ = 10 + (n // per) * (W / 2), 14 + (n % per) * 15
            ft.append(f'<circle cx="{cx_ + 7}" cy="{cy_ - 3.5}" r="6.6" fill="{ACC}"/><text x="{cx_ + 7}" y="{cy_ - .6}" font-size="8.2" '
                      f'font-weight="800" fill="#fff" text-anchor="middle">{n + 1}</text>'
                      f'<text x="{cx_ + 19}" y="{cy_}" font-size="10.5" fill="{INK}">{esc(st.get("text", ""))}</text>')
        y = per * 15 + 12
    leg = []
    x = 10
    for k, (kk, nm) in enumerate(KNAME.items()):
        leg.append(f'<rect x="{x}" y="{y + 3}" width="10" height="8" fill="{KFILL[kk]}" stroke="#94a3b8" stroke-width=".6"/>'
                   f'<text x="{x + 13}" y="{y + 10.5}" font-size="8.5" fill="{INK}">{nm}</text>')
        x += 20 + wlen(nm) * 4.9
    for ck, c in A.clk.items():
        leg.append(f'<rect x="{x + 6}" y="{y + 4}" width="6" height="6" fill="{c["col"]}"/>'
                   f'<text x="{x + 15}" y="{y + 10.5}" font-size="8.5" fill="{c["col"]}">{esc(ck)} {esc(c.get("freq", ""))}'
                   f'<tspan fill="{GRAY}">(← {esc(c.get("from", "?"))})</tspan></text>')
        x += 24 + tw(f"{ck} {c.get('freq', '')}(← {c.get('from', '?')})", 8.5)
    notes = ["線のラベル = ビット幅×クロック=帯域(GB/s はバイト)・プロトコル。直列は レーン数L×速度。線の太さ = 帯域(10 / 100 / 500GB/s で 4 段)。"
             "紺 = データ、灰 = 制御、茶の点線 = クロック。縦のすき間のラベルは縦書き。",
             "積層: 同じ位置に重ねて描く。枠の線の種類が層(上の帯)。文字は 1 層目が左上、2 層目が右下。⊠ = 層をまたぐ接続(HB など)で、重なっている位置でつながる。"
             + ("薄い所 = この動作に登場しない。配置は全体図と同じ。" if kind == "scene" else "")]
    for n, t in enumerate(notes):
        leg.append(f'<text x="10" y="{y + 24 + n * 12}" font-size="8.5" fill="{GRAY}">{esc(t)}</text>')
    FH = y + 24 + len(notes) * 12
    Hh = H0 + BH + FH + 4
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(Hh)}" font-family="{FONT}">',
           '<rect width="100%" height="100%" fill="#fff"/>', "\n".join(h_),
           f'<svg x="0" y="{f(H0)}" width="{f(BW)}" height="{f(BH)}" viewBox="{f(bx0)} {f(by0)} {f(vw)} {f(vh)}">{body}</svg>',
           f'<g transform="translate(0,{f(H0 + BH)})">' + "\n".join(ft + leg) + '</g>', '</svg>']
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(svg))
    if png:
        to_png(out)
    return out


# ======================= 表(arch.md)=======================
def report(A, L, out):
    M = [f"# {A.s.get('title', 'アーキテクチャ')}", "", "## 積層と向き", "",
         "| 層(下から) | 名前 | 回路面 | この図での見え方 | 備考 |", "|---|---|---|---|---|"]
    for d in A.dies:
        mx, my = A.state(d, *A.base())
        st = "表から(設計データと同じ)" if not mx and not my else "180°回転" if mx and my else f"裏から(設計データと{'左右' if mx else '上下'}反転)"
        M.append(f"| {d['id']} | {d['name']} | {'上 Face-up' if d['face'] == 'up' else '下 Face-down'} | {st} | {d.get('note', '')} |")
    for b in A.s.get("bonds", []):
        M.append(f"\n- {b['between'][0]} ⇔ {b['between'][1]}: {b.get('type', '')} {b.get('note', '')}")
    M += ["", "## クロック", "", "| クロック | 周波数 | 源 | 使うブロック | 備考 |", "|---|---|---|---|---|"]
    for ck, c in A.clk.items():
        us = [b["id"] for b in A.blocks.values() if b.get("clock") == ck]
        M.append(f"| {ck} | {c.get('freq', '')} | {c.get('from', '')} | {len(us)}({', '.join(us[:6])}{' …' if len(us) > 6 else ''}) | {c.get('note', '')} |")
    M += ["", "## ブロック", "", "| ブロック | 層 | 位置 [列, 行, 幅, 高さ] | 種類 | クロック | 諸元 |", "|---|---|---|---|---|---|"]
    for b in A.blocks.values():
        M.append(f"| {b['name']} (`{b['id']}`) | {b['die']} | {b['at']} | {KNAME.get(b['kind'], b['kind'])} | {b.get('clock', '')} | {' / '.join(b.get('info', []))} |")
    M += ["", "## 接続(帯域 = ビット幅 × クロック ÷ 8)", "",
          "| id | から | へ | 向き | 幅・速度 | 帯域 | プロトコル | 層をまたぐ | 種類 |", "|---|---|---|---|---|---|---|---|---|"]
    for l in A.links.values():
        by, l1, _ = A.bw(l)
        M.append(f"| {l['id']} | {l['from']} | {l['to']} | {'双方向' if l.get('dir') == 'both' else '→'} | {l1} | {fmt_bytes(by) if by else ''} | "
                 f"{l.get('proto', '')} | {l.get('via', '')} | {l['kind']} |")
    if A.s.get("scenes"):
        M += ["", "## 動作", ""]
        for sc in A.s["scenes"]:
            M.append(f"### {sc.get('title', sc['id'])}")
            for n, st in enumerate(sc.get("steps", [])):
                M.append(f"{n + 1}. {st.get('text', '')}(接続: {', '.join(st.get('links', []))})")
            M.append("")
    open(out, "w", encoding="utf-8").write("\n".join(M) + "\n")


def render(spec, outdir, png=False):
    A = Arch(spec)
    L = Layout(A, *A.base())
    for a, b in L.overlaps()[:12]:
        A.lint.append(f"[重なり] 文字が重なっている: 「{a}」と「{b}」(位置を変える・label_at を変える。詰めた方がよい所なら tight: true)")
    for bid, n, r in L.hollow():
        A.lint.append(f"[空き] {bid}({n} マス)の中が空いている(文字が面積の {r * 100:.0f}%)。小さくするか、inside で中身を書く")
    outs = []
    t0 = spec.get("title", "アーキテクチャ全体図")
    outs.append(draw(A, L, dict(kind="overview"), t0, os.path.join(outdir, "arch_overview.svg"), png))
    if len(A.dies) > 1:
        for d in A.dies:
            outs.append(draw(A, L, dict(kind="layer", die=d["id"]), f"{t0} — {d['id']} {d['name']} だけ(全体図と同じ向き)",
                             os.path.join(outdir, f"arch_layer_{d['id']}.svg"), png))
            if any(A.state(d, L.fx, L.fy)):                # 全体図で裏から見えている層: 設計データの向きの図も出す
                L2 = Layout(A, *A.state(d))
                outs.append(draw(A, L2, dict(kind="layer", die=d["id"], design=True), f"{t0} — {d['id']} {d['name']}(回路面から見た向き = 設計データの向き)",
                                 os.path.join(outdir, f"arch_layer_{d['id']}_design.svg"), png))
    sc_by = {}
    for sc in spec.get("scenes", []):
        sc_by[sc["id"]] = sc
        outs.append(draw(A, L, dict(kind="scene", steps=sc.get("steps", []), blocks=sc.get("blocks")),
                         sc.get("title", sc["id"]) + "(全体図と同じ配置)", os.path.join(outdir, f"arch_scene_{sc['id']}.svg"), png))
    for cr in spec.get("crops", []):
        sc = sc_by.get(cr.get("scene"))
        mode = dict(kind="scene" if sc else "overview", crop=cr["blocks"], steps=(sc or {}).get("steps", []), blocks=(sc or {}).get("blocks"))
        outs.append(draw(A, L, mode, cr.get("title", cr["id"]), os.path.join(outdir, f"arch_crop_{cr['id']}.svg"), png))
    report(A, L, os.path.join(outdir, "arch.md"))
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
        A = Arch(spec)
        L = Layout(A, *A.base())
        for a, b in L.overlaps()[:12]:
            A.lint.append(f"[重なり] 文字が重なっている: 「{a}」と「{b}」")
    nb = sum(len(v) for v in A.own.values())
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
