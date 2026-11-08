#!/usr/bin/env python3
"""chipdiag — チップ仕様書向けブロック図レンダラ

YAML の「図の仕様」(構造 + グループ + 時系列シナリオ) から SVG を決定的に生成する。
  - 構造(ベース図)は1回だけ定義し、全ステップ・全ビューで座標を共有する
  - 動作(データフロー矢印)とグループ(半透明の色分け)は上に重ねるレイヤ
  - 階層は children で表現し、詳細図は同じ座標系のズームで生成する

使い方:
  python3 chipdiag.py render  spec.yaml -o out/ [--png]   # 構造/グループ/各ステップ/詳細図
  python3 chipdiag.py lint    spec.yaml                   # 規約チェックのみ
  python3 chipdiag.py anchors spec.yaml                   # flows.path に使える経路点の一覧
  python3 chipdiag.py freeze  spec.yaml -o fixed.yaml     # 自動配置の座標を書き出して固定
"""
import sys, os, re, html, yaml, argparse, shutil, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from layout import needs_layout, autolayout, strip_private
from addrmap import parse_addr, parse_ranges, hx, fmt_size, range_text

FONT = "'Noto Sans JP','Noto Sans CJK JP','Hiragino Kaku Gothic ProN','Yu Gothic',Meiryo,sans-serif"
INK = "#1e293b"
GRAY = "#64748b"
PAST = "#a3adbb"

MAX_LABEL = 16      # 図内ラベルの最大表示幅(半角換算)
MAX_FLOWS = 3       # 1ステップに重ねる矢印の上限


def esc(s):
    return html.escape(str(s), quote=True)


def f(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def wlen(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


PERF = "#0369a1"
BUS_PAL = ["#2563eb", "#16a34a", "#7c3aed", "#0d9488", "#ca8a04", "#0284c7", "#4d7c0f", "#be185d"]
FIFO_COL, FIFO_FILL = "#a21caf", "#fae8ff"   # FIFO 専用色(メモリ橙・CDC赤・注記青・グループ色と重ならない)


def hz(v):
    """'1.2GHz' / '800MHz' / 1e9 → Hz"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    import re as _re
    m = _re.match(r"\s*([\d.]+)\s*([kKmMgG]?)[hH]z", str(v))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


def fmt_bw(bps):
    gb = bps / 1e9
    return f"{gb:.3g}GB/s" if gb >= 1 else f"{gb*1000:.3g}MB/s"


def bw_text(o):
    """要素の帯域表示: bw 明記 > width(bit)×clock から計算"""
    if o.get("bw"):
        return str(o["bw"])
    f_ = hz(o.get("clock"))
    if o.get("width") and f_:
        return fmt_bw(o["width"] / 8 * f_)
    return None


def bus_perf_text(bus):
    """バス幅 / クロック / 帯域 → '256bit / 1GHz / 32GB/s'"""
    parts = [f"{bus['width']}bit" if bus.get("width") else None, str(bus["clock"]) if bus.get("clock") else None,
             bw_text(bus)]
    return " / ".join(x for x in parts if x)


STYLE = {
    "logic":  dict(fill="#ffffff", stroke=INK, sw=1.6, dash=None),
    "memory": dict(fill="#fff7ed", stroke="#c2410c", sw=1.8, dash=None),
    "ext":    dict(fill="#f8fafc", stroke=GRAY, sw=1.4, dash="5 4"),
    "child":  dict(fill="#f1f5f9", stroke="#475569", sw=1.2, dash=None),
    "fifo":   dict(fill=FIFO_FILL, stroke=FIFO_COL, sw=1.6, dash=None),
    "bridge": dict(fill="#f1f5f9", stroke="#334155", sw=1.6, dash=None),
}


class Chip:
    def __init__(self, spec):
        self.s = spec
        self.blocks, self.anch, self.warn = {}, {}, []
        self.colors = set()
        self.expand_bus_links(spec)
        self.expand_attached(spec)
        self.attach_opt = {}     # (bus, "blk.port") -> {bus_port, cdc}
        for bus in spec.get("buses", []):
            norm = []
            for a in bus.get("attach", []):
                if isinstance(a, dict):
                    key = a["port"]
                    self.attach_opt[(bus["id"], key)] = a
                    if a.get("addr"):
                        bus["_has_addr"] = True   # 自動配置: アドレスを書く帯の高さを取る
                    if a.get("fifo"):
                        bus.setdefault("_fifo_ports", []).append(key)   # 自動配置: FIFO 記号と段数の文字の余白
                else:
                    key = a
                norm.append(key)
            bus["attach"] = norm
        self.domains = {d["id"]: d for d in spec.get("clocks", {}).get("domains", [])}
        def resolve(o):
            d = self.domains.get(o.get("clock_domain"))
            if d and not o.get("clock"):
                o["clock"] = d.get("freq")
            for p in o.get("ports", []):   # 端子ごとのクロック(ブリッジ等)
                dp = self.domains.get(p.get("clock_domain"))
                if dp and not p.get("clock"):
                    p["clock"] = dp.get("freq")
            for c in o.get("children", []):
                resolve(c)
        for o in spec.get("blocks", []) + spec.get("buses", []):
            resolve(o)
        self.auto = needs_layout(spec)
        if self.auto:
            import copy
            spec["_orig"] = copy.deepcopy({k: v for k, v in spec.items() if k != "_orig"})
            autolayout(spec, self.warn)

        def reg(b, parent=None, depth=0, ext=False):
            b["_parent"], b["_depth"], b["_ext"] = parent, depth, ext
            if b["id"] in self.blocks:
                self.warn.append(f"ID重複: {b['id']}")
            self.blocks[b["id"]] = b
            b["_hidden"] = "rect" not in b   # この図の深さより下(詳細ページで描く)
            for p in ([] if b["_hidden"] else b.get("ports", [])):
                self.anch[f"{b['id']}.{p['id']}"] = self.port_xy(b, p) + (p,)
            for c in b.get("children", []):
                reg(c, b["id"], depth + 1)

        for b in spec.get("external", []):
            reg(b, ext=True)
        for b in spec.get("blocks", []):
            reg(b)
        for p in spec.get("pads", []):
            self.anch[p["id"]] = (p["at"][0], p["at"][1], p["side"], None, p)
        self.buses = {b["id"]: b for b in spec.get("buses", [])}
        self.steps = spec.get("scenario", {}).get("steps", [])
        # バスグループの色: アーキテクチャ図とメモリマップで同じ色を使う(紐付けのため)
        self.bus_color = {bid: b.get("color") or BUS_PAL[i % len(BUS_PAL)] for i, (bid, b) in enumerate(self.buses.items())}
        self.block_buses = {}
        for bid, bus in self.buses.items():
            for a in bus.get("attach", []):
                self.block_buses.setdefault(a.split(".")[0], [])
                if bid not in self.block_buses[a.split(".")[0]]:
                    self.block_buses[a.split(".")[0]].append(bid)
        self.bus_ports = {}
        for bus in self.buses.values():
            up = str(bus.get("label", "")).upper() + str(bus.get("protocol", "")).upper()
            shared = bus.get("type") == "shared" or "APB" in up or "AHB" in up
            bus["_shared"] = shared
            sel = bus.get("select_prefix") or ("PSEL" if "APB" in up else ("HSEL" if "AHB" in up else "SEL"))
            base = bus.get("index_base", 0 if shared else 1)
            cnt = {"M": base, "S": base}
            for a in bus.get("attach", []):
                opt = self.attach_opt.get((bus["id"], a), {})
                role = (self.anch.get(a, (0, 0, 0, 0, {}))[4] or {}).get("role")
                brole = {"M": "S", "S": "M"}.get(role)   # クロスバー側は相手の逆
                own = self.blocks.get((self.anch.get(a) or (0, 0, 0, None))[3])
                while own is not None and own["_parent"]:
                    own = self.blocks[own["_parent"]]
                n = int((own or {}).get("count", 1) or 1)   # 繰り返しブロック → 番号を範囲で
                if opt.get("bus_port"):
                    lab = opt["bus_port"]
                    brole = lab[0].upper() if lab[:1].upper() in "MS" else brole
                elif shared:
                    if role == "S":   # 共有バス: スレーブに選択信号の番号、マスタ側は記号なし
                        lab = f"{sel}{cnt['S']}" + (f"-{cnt['S'] + n - 1}" if n > 1 else "")
                        brole = "S"
                        cnt["S"] += n
                    else:
                        lab, brole = None, None
                elif brole:
                    lab = f"{brole}{cnt[brole]}" + (f"-{cnt[brole] + n - 1}" if n > 1 else "")
                    cnt[brole] += n
                else:
                    lab = None
                self.bus_ports[(bus["id"], a)] = (brole, lab)
        for g in spec.get("groups", []):
            for r in g.get("regions", []):
                if r.get("blocks") and not r.get("rects"):
                    rr = []
                    for oid in r["blocks"]:
                        o = self.blocks.get(oid) or self.buses.get(oid)
                        if o and "rect" in o:
                            x, y, w, h = o["rect"]
                            rr.append([x - 9, y - 9, w + 18, h + 18])
                        else:
                            self.warn.append(f"グループ {g['id']}: 未定義の {oid}")
                    r["rects"] = rr
        self.auto_bus_group()
        self.auto_clock_group()
        self.lat_results = []
        if spec.get("latency", {}).get("accesses"):
            from latency import expand_steps
            expand_steps(self)
        self.passes = {}   # bid -> [('V',x)|('H',y)] 矢印がブロックを貫通する位置
        for st in self.steps:
            for fl in st["flows"]:
                for a, b in zip(fl["path"], fl["path"][1:]):
                    A, B = self.anch.get(a), self.anch.get(b)
                    if A and B and A[3] and A[3] == B[3]:
                        if A[2] in ("N", "S") and B[2] in ("N", "S"):
                            self.passes.setdefault(A[3], []).append(("V", (A[0] + B[0]) / 2))
                        else:
                            self.passes.setdefault(A[3], []).append(("H", min(A[1], B[1]), max(A[1], B[1])))
        self.lint()

    @staticmethod
    def expand_attached(spec):
        """attached: 親の枠に引っ付けて描く小さな枠(例 CPU の I$ / D$)。トップのブロックとして展開し、
        自動配置で親の枠に接して並べる(親の count・クロックを引き継ぐ)"""
        if spec.get("_attached_done"):
            return
        add = []
        for b in spec.get("blocks", []):
            for a in b.get("attached", []) or []:
                a = dict(a)
                a["_att_of"] = b["id"]
                a.setdefault("clock_domain", b.get("clock_domain"))
                if a.get("clock_domain") is None:
                    a.pop("clock_domain")
                add.append(a)       # count は親(コア)の重ね表示だけで示す
        spec.setdefault("blocks", []).extend(add)
        spec["_attached_done"] = True

    @staticmethod
    def expand_bus_links(spec):
        """bus_links: バスとバスを双方向につなぐ(どのCPUからも全アドレスに届くように)
          - {between: [noc, rt], to_b: "0x3040_0000/4M", to_a: default, label: NoC⇄RT}
        → ブリッジのブロック(端子4つ: A側 S/M、B側 M/S)を作り、両方のバスに attach する。
          to_b = A から B へ転送するアドレス窓(A 側のスレーブ端子のデコード範囲)。default = A で他に当たらない全て"""
        if spec.get("_bus_links_done"):
            return
        buses = {b["id"]: b for b in spec.get("buses", [])}
        for bl in spec.get("bus_links", []):
            a, b = bl["between"]
            if a not in buses or b not in buses:
                continue
            bid = bl.get("id") or f"bl_{a}_{b}"
            ca, cb = buses[a].get("clock_domain"), buses[b].get("clock_domain")
            ports = []
            # 既定: バスどうしを直接つなぐ(専用のモジュールは描かない)。線は送り側のクロックのまま入り、
            #   受け側のインターコネクトの中で CDC。block: true なら従来どおりブリッジの箱を描く
            virt = not bl.get("block")
            if bl.get("to_b") is not None:
                ports += [{"id": "a_s", "role": "S", "clock_domain": ca, "width": buses[a].get("width")},
                          {"id": "b_m", "role": "M", "clock_domain": ca if virt else cb,
                           "width": (buses[a] if virt else buses[b]).get("width")}]
            if bl.get("to_a") is not None:
                ports += [{"id": "b_s", "role": "S", "clock_domain": cb, "width": buses[b].get("width")},
                          {"id": "a_m", "role": "M", "clock_domain": cb if virt else ca,
                           "width": (buses[b] if virt else buses[a]).get("width")}]
            ports = [{k: v for k, v in p.items() if v is not None} for p in ports]
            spec.setdefault("blocks", []).append({
                "id": bid, "label": bl.get("label", f"{buses[a].get('label', a)}⇄{buses[b].get('label', b)}"),
                "kind": "bridge", "ports": ports, "_buslink": [a, b], "status": bl.get("status"),
                **({"virtual": True} if virt else {})})
            if bl.get("to_b") is not None:
                buses[a].setdefault("attach", []).append({"port": f"{bid}.a_s", "addr": bl["to_b"], **({"fifo": bl["fifo"]} if bl.get("fifo") else {})})
                buses[b]["attach"].append(f"{bid}.b_m")
            if bl.get("to_a") is not None:
                buses[b].setdefault("attach", []).append({"port": f"{bid}.b_s", "addr": bl["to_a"], **({"fifo": bl["fifo"]} if bl.get("fifo") else {})})
                buses[a]["attach"].append(f"{bid}.a_m")
        spec["_bus_links_done"] = True

    # ---------- 幾何 ----------
    @staticmethod
    def port_xy(b, p):
        x, y, w, h = b["rect"]
        t = p.get("at", 0.5)
        s = p.setdefault("side", "N")
        if s in ("N", "S"):
            px = x + w * t if t <= 1 else t
            return (px, y if s == "N" else y + h, s, b["id"])
        py = y + h * t if t <= 1 else t
        return (x if s == "W" else x + w, py, s, b["id"])

    def link_routes(self, detail=True):
        """結線ごとの折れ線(中間区間が他と重なれば 10px ずつずらす)。{index: pts}"""
        routes, segs = {}, []
        def clash(a, _unused, horiz):
            (x1, y1), (x2, y2) = a
            for (u1, v1), (u2, v2), h in segs:
                if h != horiz:
                    continue
                if horiz and abs(y1 - v1) < 5 and min(max(x1, x2), max(u1, u2)) - max(min(x1, x2), min(u1, u2)) > 2:
                    return True
                if not horiz and abs(x1 - u1) < 5 and min(max(y1, y2), max(v1, v2)) - max(min(y1, y2), min(v1, v2)) > 2:
                    return True
            return False
        for i, ln in enumerate(self.s.get("links", [])):
            if ln["from"] not in self.anch or ln["to"] not in self.anch:
                continue
            pts = [list(p) for p in self.polyline(self.nodes([ln["from"], ln["to"]]))]
            if len(pts) == 4:   # Z字: 中央の区間だけ動かせる
                horiz = abs(pts[1][1] - pts[2][1]) < 0.6
                k = 0
                while clash((tuple(pts[1]), tuple(pts[2])), None, horiz) and k < 8:
                    k += 1
                    d = 10 * ((k + 1) // 2) * (1 if k % 2 else -1)
                    base = self.polyline(self.nodes([ln["from"], ln["to"]]))
                    if horiz:
                        pts[1][1] = pts[2][1] = base[1][1] + d
                    else:
                        pts[1][0] = pts[2][0] = base[1][0] + d
            pts = [tuple(p) for p in pts]
            for a, b in zip(pts, pts[1:]):
                segs.append((a, b, abs(a[1] - b[1]) < 0.6))
            routes[i] = pts
        return routes

    def link_dir(self, ln):
        """結線の矢印: 明記 > IO(inout)は双方向 > M→S / out→in > 書いた向き"""
        if ln.get("dir"):
            return ln["dir"]
        ends = [self.anch.get(ln["from"]), self.anch.get(ln["to"])]
        ps = [(e[4] or {}) if e else {} for e in ends]
        if any(p.get("dir") == "inout" for p in ps) or any(e and e[3] is None for e in ends):
            return "both"
        return "fwd"

    # ---------- バス接続(スタブ) ----------
    def stub(self, bus, a):
        """(px,py, jx,jy, vertical) ポート位置とバスとの接点"""
        bx, by, bw, bh = bus["rect"]
        x, y = self.anch[a][:2]
        if bx <= x <= bx + bw:
            return x, y, x, (by if y < by else by + bh), True
        return x, y, (bx if x < bx else bx + bw), y, False

    def port_width(self, bus, a):
        p = self.anch[a][4] or {}
        return p.get("width") or bus.get("width")

    @staticmethod
    def trim(pts, d0, d1):
        """折れ線の両端を d0/d1 だけ縮める(矢印の先がポート記号に隠れないように)"""
        pts = [list(p) for p in pts]
        for idx, nxt, d in ((0, 1, d0), (-1, -2, d1)):
            if d <= 0 or len(pts) < 2:
                continue
            (x0, y0), (x1, y1) = pts[idx], pts[nxt]
            L = abs(x1 - x0) + abs(y1 - y0)
            if L > d * 2:
                t = d / L
                pts[idx] = [x0 + (x1 - x0) * t, y0 + (y1 - y0) * t]
        return [tuple(p) for p in pts]

    # ---------- クロック/CDC ----------
    def clk_key(self, o):
        """比較用: ドメイン名があればそれ、なければ周波数"""
        if o is None:
            return None
        if o.get("clock_domain"):
            return ("d", o["clock_domain"])
        h = hz(o.get("clock"))
        return ("f", h) if h else None

    def owner_clock(self, key):
        p = (self.anch.get(key) or (0, 0, 0, None, {}))[4] or {}
        if p.get("clock") or p.get("clock_domain"):   # 端子にクロックがあればそれ
            return p
        bid = self.anch[key][3] if key in self.anch else None
        b = self.blocks.get(bid)
        while b is not None and not b.get("clock") and not b.get("clock_domain") and b["_parent"]:
            b = self.blocks[b["_parent"]]
        return b

    def cdc_between(self, a, b, explicit=None):
        """CDC判定: explicit(true/false/文字列) > ドメイン/周波数の比較"""
        if explicit is False:
            return None
        if explicit:
            return explicit if isinstance(explicit, str) else "CDC"
        ka, kb = self.clk_key(a), self.clk_key(b)
        if ka and kb and ka != kb:
            return "CDC"
        return None

    CDC_OFF = 16   # 枠の内側への距離

    def inside(self, x, y, side, d=None):
        """ポート位置 (x,y) からブロックの内側へ d 進んだ点"""
        d = d or self.CDC_OFF
        return {"N": (x, y + d), "S": (x, y - d), "W": (x + d, y), "E": (x - d, y)}.get(side, (x, y))

    FIFO_CELL = (5.5, 15)   # セルの短辺・長辺(px)。spec の style.fifo_scale で拡大縮小

    def fifo_dims(self, vert):
        k = float(self.s.get("style", {}).get("fifo_scale", 1.0))
        a, b = self.FIFO_CELL[0] * k, self.FIFO_CELL[1] * k
        return (b, 4 * a) if vert else (4 * a, b)   # 縦の流れ: セルを縦に積む

    CDC_W = 28.6

    def mark_box(self, x, y, lab, vert):
        """記号の占有矩形。FIFO: セルの中心が (x,y)。非同期FIFO: セルの右に CDC を並べる"""
        if "FIFO" in lab:
            gw, gh = self.fifo_dims(vert)
            x1 = x + gw / 2 + (3 + self.CDC_W if lab.startswith("AFIFO") else 0)
            return (x - gw / 2, y - gh / 2, x1, y + gh / 2)
        w = wlen(lab) * 6.2 + 10
        return (x - w / 2, y - 6.5, x + w / 2, y + 6.5)

    def mark_w(self, lab, vert=False):
        b = self.mark_box(0, 0, lab, vert)
        return b[2] - b[0]

    def mark_depth(self, lab, vert):
        """ポートから内側へ置く距離(記号がポート記号に重ならない最小)"""
        b = self.mark_box(0, 0, lab, vert)
        return 8 + ((b[3] - b[1]) / 2 if vert else (b[2] - b[0]) / 2)

    @staticmethod
    def mark_label(cdc, fifo):
        if not fifo:
            return cdc
        dep = "" if fifo is True else f" {fifo}段"
        return (f"AFIFO{dep}" if cdc else f"FIFO{dep}")

    def _clash(self, x, y, lab, vert, pts, extra=()):
        a = self.mark_box(x, y, lab, vert)
        for q in pts:
            b = self.mark_box(q[0], q[1], q[3], q[2])
            if min(a[2], b[2]) - max(a[0], b[0]) > -2 and min(a[3], b[3]) - max(a[1], b[1]) > -2:
                return True
        for sx, sy in extra:   # 横から入るポートの番号記号
            if min(a[2], sx + 14) - max(a[0], sx - 14) > 0 and min(a[3], sy + 8) - max(a[1], sy - 8) > 0:
                return True
        return False

    def cdc_points(self, detail=False):
        """CDC はモジュールの中に置く: [(x, y, vertical, label, desc)]
        バス接続: 既定はクロスバーの中(cdc_in: bus)。cdc_in: block でブロック側。
        結線: 既定は受け側ブロックの中(cdc_in: to)。cdc_in: from で送り側。"""
        pts = []
        if not hasattr(self, "mark_hosts"):
            self.mark_hosts = set()   # CDC/FIFO 枠を中に持つブロック(ラベルを上へ退避)
        self._mhost = {}
        self._mjunc = {}   # 記号 → それがのる接続の接点(線をFIFOまで延ばす)
        self.cdc_leaders = []      # 引き出し線は使わない(CDCは位置が重要: 自分の接続線の上から動かさない)
        self.cdc_unresolved = []
        for bus in self.buses.values():
            bx, by, bw, bh = bus["rect"]
            side_j = [self.stub(bus, q)[2:4] for q in bus.get("attach", []) if not self.stub(bus, q)[4]]
            # 横から入る接続を先に置き、縦の接続は自分の線の延長上(クロスバーの奥)へだけ動かす
            for a in sorted(bus.get("attach", []), key=lambda a: 1 if self.stub(bus, a)[4] else 0):
                opt = self.attach_opt.get((bus["id"], a), {})
                lab = self.mark_label(self.cdc_between(self.owner_clock(a), bus, opt.get("cdc")), opt.get("fifo"))
                if not lab:
                    continue
                px, py, jx, jy, vert = self.stub(bus, a)
                where = opt.get("cdc_in", bus.get("cdc_in", "bus"))
                if where == "block":
                    x, y = self.inside(px, py, self.anch[a][2], self.mark_depth(lab, vert))
                    self.mark_hosts.add(self.anch[a][3])
                elif vert:
                    d = 1 if py < jy else -1
                    x, y = jx, jy + d * self.mark_depth(lab, True)
                    k = 0
                    while self._clash(x, y, lab, vert, pts, side_j) and k < 6:
                        y += d * 4
                        k += 1
                else:
                    sw = self.mark_box(0, 0, lab, False)
                    x, y = jx + (1 if px < jx else -1) * (13 + (sw[2] - sw[0]) / 2), py
                if self._clash(x, y, lab, vert, pts, side_j if vert and where != "block" else ()):
                    self.cdc_unresolved.append(a)
                self._mhost[(round(x, 1), round(y, 1))] = self.anch[a][3] if where == "block" else bus["id"]
                self._mjunc[(round(x, 1), round(y, 1))] = (px, py) if where == "block" else (jx, jy)
                pts.append((x, y, vert, lab, f"{a} ↔ {bus['id']} ({'クロスバー内' if where != 'block' else 'ブロック内'})"))
        for b in self.blocks.values():   # 端子どうしでクロックが違うブロック(ブリッジ等) → 中にCDC
            if b["_ext"] or b.get("_hidden") or b.get("cdc") is False or (b["_depth"] > 1 and not detail):
                continue
            ks = {self.clk_key(p) for p in b.get("ports", []) if self.clk_key(p)}
            if len(ks) >= 2:
                x, y, w, h = b["rect"]
                lab = b.get("cdc") if isinstance(b.get("cdc"), str) else "CDC"
                if b.get("virtual"):
                    continue
                if b.get("_mp"):     # 縦長の枠: 名前は中央のまま、CDC はその下
                    pts.append((x + w / 2, y + h / 2 + 26, False, lab, f"{b['id']} 内(端子のクロックが異なる)"))
                    continue
                self.mark_hosts.add(b["id"])
                pts.append((x + w / 2, y + h / 2 + 9, False, lab, f"{b['id']} 内(端子のクロックが異なる)"))
        for ln in self.s.get("links", []):
            if ln.get("level", 0) > 0 and not detail:
                continue
            if ln["from"] not in self.anch or ln["to"] not in self.anch:
                continue
            A, B = self.anch[ln["from"]], self.anch[ln["to"]]
            if A[3] is None or B[3] is None or self.blocks[A[3]]["_ext"] or self.blocks[B[3]]["_ext"]:
                continue
            if not (self.visible(A[3]) and self.visible(B[3])):
                continue
            lab = self.mark_label(self.cdc_between(self.owner_clock(ln["from"]), self.owner_clock(ln["to"]), ln.get("cdc")),
                                  ln.get("fifo"))
            if not lab:
                continue
            P = A if ln.get("cdc_in", "to") == "from" else B
            vflow = P[2] in ("N", "S")
            extra = 24 if (self.blocks[P[3]].get("kind") == "memory" and P[2] == "W") else 0   # メモリ記号の帯を避ける
            x, y = self.inside(P[0], P[1], P[2], self.mark_depth(lab, vflow) + extra)   # 記号だけ端子の内側、文字は空いている所
            self.mark_hosts.add(P[3])
            self._mhost[(round(x, 1), round(y, 1))] = P[3]
            self._mjunc[(round(x, 1), round(y, 1))] = (P[0], P[1])
            pts.append((x, y, P[2] in ("N", "S"), lab, f"{ln['from']} → {ln['to']} ({P[3]} 内)"))
        return pts

    def auto_bus_group(self):
        """バスグループ: 各バスとその帯につながるブロック(ブリッジを除く)を半透明で重ねる"""
        if len(self.buses) < 2 or any(g["id"] == "bus" for g in self.s.get("groups", [])):
            return
        regions = []
        for bid, bus in self.buses.items():
            rects = [[bus["rect"][0] - 6, bus["rect"][1] - 6, bus["rect"][2] + 12, bus["rect"][3] + 12]]
            for blk, bl in self.block_buses.items():
                b = self.blocks.get(blk)
                if b and bl == [bid] and "rect" in b and not b["_ext"]:
                    x, y, w, h = b["rect"]
                    rects.append([x - 9, y - 9, w + 18, h + 18])
            regions.append({"label": bus.get("label", bid), "color": self.bus_color[bid], "rects": rects})
        self.s.setdefault("groups", []).insert(0, {"id": "bus", "label": "バスグループ", "regions": regions})

    def auto_clock_group(self):
        """clocks.domains からクロックドメインの色分けレイヤを自動生成(手書きの group 'clock' があればそちら優先)"""
        if not self.domains or any(g["id"] == "clock" for g in self.s.get("groups", [])):
            return
        palette = ["#2563eb", "#9333ea", "#db2777", "#0891b2", "#ea580c", "#16a34a", "#ca8a04"]
        regions = []
        for i, (did, d) in enumerate(self.domains.items()):
            rects = []
            for o in list(self.blocks.values()) + list(self.buses.values()):
                if o.get("clock_domain") == did and not o.get("_ext") and not o.get("_hidden"):
                    if o.get("_parent") and self.blocks[o["_parent"]].get("clock_domain") == did:
                        continue
                    x, y, w, h = o["rect"]
                    rects.append([x - 9, y - 9, w + 18, h + 18])
            if rects:
                regions.append({"label": f"{d.get('label', did)} {d.get('freq', '')}{'(仮)' if d.get('assumed') else ''}".strip(),
                                "color": d.get("color", palette[i % len(palette)]), "rects": rects})
        if regions:
            self.s.setdefault("groups", []).append({"id": "clock", "label": "クロックドメイン", "regions": regions})

    # ---------- 規約チェック ----------
    def lint(self):
        for b in self.blocks.values():
            if wlen(b.get("label", "")) > MAX_LABEL and not b.get("virtual"):
                self.warn.append(f"ラベルが長い({b['id']}): 説明は図外の表へ")
            if b.get("sub") and wlen(b["sub"]) > MAX_LABEL:
                self.warn.append(f"補足が長い({b['id']})")
        linked = set()
        for ln in self.s.get("links", []):
            linked |= {ln["from"].split(".")[0], ln["to"].split(".")[0]}
        for bus in self.buses.values():
            linked |= {a.split(".")[0] for a in bus.get("attach", [])}
        ext_ids = {b["id"] for b in self.blocks.values() if b["_ext"]}
        for p in self.s.get("pads", []):
            if p.get("external") == "none":
                continue
            nb = {x for ln in self.s.get("links", []) for x in (ln["from"], ln["to"])
                  if p["id"] in (ln["from"], ln["to"])} - {p["id"]}
            if not any(x.split(".")[0] in ext_ids for x in nb):
                self.warn.append(f"{p['id']}: チップ外の接続先(データの入口/出口)が未定義 → external を追加")
        for b in self.blocks.values():
            if b.get("kind") == "memory" and not b["_ext"] and b["id"] not in linked and not b.get("_hidden") \
                    and not (b["_parent"] and self.blocks[b["_parent"]].get("pack")):
                self.warn.append(f"{b['id']}: メモリの接続先(バス/演算器)が不明 → データフローが描けない")
        miss = []
        for bus in self.buses.values():
            if not bw_text(bus):
                miss.append(f"バス {bus['id']}(width/clock)")
        top_blocks = [b for b in self.blocks.values() if not b["_ext"] and b["_depth"] == 0]
        noclk = [b["id"] for b in top_blocks if not b.get("clock") and not any(p.get("clock") for p in b.get("ports", []))]
        if noclk:
            miss.append("クロック: " + ",".join(noclk))
        nobw = [p["id"] for p in self.s.get("pads", []) if not p.get("bw") and p.get("external") != "none"]
        if nobw:
            miss.append("IO帯域: " + ",".join(nobw))
        if miss:
            self.warn.append("[perf] 未記入 → " + " / ".join(miss))
        unk = []
        for bus in self.buses.values():
            for a in bus.get("attach", []):
                if a in self.anch and (self.clk_key(self.owner_clock(a)) is None or self.clk_key(bus) is None):
                    unk.append(a)
        if unk and any(self.clk_key(o) for o in list(self.blocks.values()) + list(self.buses.values())):
            self.warn.append(f"[cdc] クロック不明のため非同期判定できない接続: {', '.join(unk[:6])}{' ...' if len(unk) > 6 else ''}")
        # 結線がブロックを貫通していないか(詳細レベルも含めて)
        def anc(bid):
            out_ = set()
            while bid:
                out_.add(bid)
                bid = self.blocks[bid]["_parent"]
            return out_
        thru = []
        for ln in self.s.get("links", []):
            if ln["from"] not in self.anch or ln["to"] not in self.anch:
                continue
            if self.auto and ln.get("level", 0) > 0:
                continue   # 内部の結線は詳細ページ側で検査
            ends = set()
            for e in (ln["from"], ln["to"]):
                if self.anch[e][3]:
                    ends |= anc(self.anch[e][3])
                    ao = self.blocks.get(self.anch[e][3], {}).get("_att_of")
                    if ao:
                        ends.add(ao)       # 中の I$/D$ から出る線は親(コア)の枠を通ってよい
            pts = self.polyline(self.nodes([ln["from"], ln["to"]]))
            for b in self.blocks.values():
                if b["id"] in ends or b["_ext"] or b.get("_hidden"):
                    continue
                x, y, w, h = b["rect"]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    if min(x1, x2) < x + w - 2 and max(x1, x2) > x + 2 and min(y1, y2) < y + h - 2 and max(y1, y2) > y + 2:
                        thru.append(f"{ln['from']}→{ln['to']} が {b['id']}")
                        break
        if thru:
            self.warn.append("[貫通] 結線がブロックを横切る: " + " / ".join(thru[:5]) + (" ..." if len(thru) > 5 else "")
                             + " → place/order/children の順序で直す")
        rts = self.link_routes()
        lns = self.s.get("links", [])
        segs = [(i, a, b) for i, pts in rts.items() for a, b in zip(pts, pts[1:])]
        dup = set()
        for ii, (i, a, b) in enumerate(segs):
            for (j, c, d) in segs[ii + 1:]:
                if i == j or {lns[i]["from"], lns[i]["to"]} & {lns[j]["from"], lns[j]["to"]}:
                    continue   # 同じ端子を共有する線は合流してよい
                h1, h2 = abs(a[1] - b[1]) < 0.6, abs(c[1] - d[1]) < 0.6
                if h1 and h2 and abs(a[1] - c[1]) < 3 and min(max(a[0], b[0]), max(c[0], d[0])) - max(min(a[0], b[0]), min(c[0], d[0])) > 4:
                    dup.add((i, j))
                if not h1 and not h2 and abs(a[0] - c[0]) < 3 and min(max(a[1], b[1]), max(c[1], d[1])) - max(min(a[1], b[1]), min(c[1], d[1])) > 4:
                    dup.add((i, j))
        if dup:
            self.warn.append("[線の重なり] " + " / ".join(f"{lns[i]['from']}→{lns[i]['to']} と {lns[j]['from']}→{lns[j]['to']}"
                                                        for i, j in list(dup)[:4]))
        self.cdc_points(True)
        if self.cdc_unresolved:
            self.warn.append("[CDC位置] 接続の位置に置けない(他の記号と重なる): " + ", ".join(self.cdc_unresolved[:6])
                             + " → 接続の間隔を空ける(order/width)か cdc_in を見直す")
        conn_keys = {a for bus in self.buses.values() for a in bus.get("attach", [])} | \
            {x for ln in self.s.get("links", []) for x in (ln["from"], ln["to"])}
        lone = [b["id"] for b in self.blocks.values()
                if b.get("kind") == "fifo" and not any(f"{b['id']}.{p['id']}" in conn_keys for p in b.get("ports", []))]
        if lone:
            self.warn.append(f"[info] 接続のないFIFO: {', '.join(lone)}(サブブロックを並べるだけの図なら可。データの流れを示すなら入口/出口を結線)")
        try:
            from reach import check_reach, check_owner
            for w in check_reach(self) + check_owner(self):
                self.warn.append(w)
        except Exception as e:
            self.warn.append(f"[到達性] 検査できない: {e}")
        try:
            from memmap import regions, check as mm_check
            for w in mm_check(regions(self)):
                self.warn.append("[アドレス] " + w)
        except Exception as e:
            self.warn.append(f"[アドレス] 検査できない: {e}")
        tbd = [b["id"] for b in self.blocks.values() if b.get("status") == "tbd"]
        if tbd:
            self.warn.append(f"[info] 未決(TBD)ブロック: {', '.join(tbd)}")
        for ln in self.s.get("links", []):
            for a in (ln["from"], ln["to"]):
                if a not in self.anch and a not in self.blocks and not self.blocks.get(a.split(".")[0], {}).get("_hidden"):
                    self.warn.append(f"結線の端点が未定義: {a}")
        for bus in self.buses.values():
            for a in bus.get("attach", []):
                if a not in self.anch:
                    self.warn.append(f"バス接続先が未定義: {a}")
                elif self.anch[a][4].get("role") not in ("M", "S"):
                    self.warn.append(f"バス接続ポートにM/S未指定: {a}")
        for i, st in enumerate(self.steps, 1):
            if len(st.get("flows", [])) > MAX_FLOWS:
                self.warn.append(f"Step{i}: 矢印{len(st['flows'])}本 → ステップ分割を推奨")
            for fl in st.get("flows", []):
                for a in fl["path"]:
                    if a not in self.anch and a not in self.buses and a not in self.blocks:
                        self.warn.append(f"Step{i}: 未定義の経路点 {a}")

    # ---------- 配線 ----------
    def nodes(self, path, lane=0.0):
        raw = []
        for a in path:
            if a in self.buses:
                raw.append(("bus", a))
            elif a in self.anch:
                raw.append(("pt", self.anch[a]))
            else:
                x, y, w, h = self.blocks[a]["rect"]
                raw.append(("pt", (x + w / 2, y + h / 2, "C", a, {})))
        out = []
        for i, (t, v) in enumerate(raw):
            if t == "pt":
                out.append((v[0], v[1], v[2]))
                continue
            bx, by, bw, bh = self.buses[v]["rect"]
            cy = self.flow_cy(self.buses[v]) + lane
            for Q, enter in ((raw[i - 1][1], True), (raw[i + 1][1], False)):
                qx, qy = Q[0], Q[1]
                if bx <= qx <= bx + bw:
                    seg = [(qx, cy, "B")]
                else:
                    ex = bx if qx < bx else bx + bw
                    seg = [(ex, qy, "B"), (ex, cy, "B")]
                    if not enter:
                        seg = seg[::-1]
                out += seg
        return out

    @staticmethod
    def polyline(nodes):
        pts = [nodes[0][:2]]
        for a, b in zip(nodes, nodes[1:]):
            ax, ay, asd = a
            bx, by, bsd = b
            if abs(ax - bx) < 0.6 or abs(ay - by) < 0.6:
                pts.append((bx, by))
            elif asd in ("N", "S") or (asd not in ("W", "E") and bsd in ("N", "S")):
                my = (ay + by) / 2
                pts += [(ax, my), (bx, my), (bx, by)]
            else:
                mx = (ax + bx) / 2
                pts += [(mx, ay), (mx, by), (bx, by)]
        res = []
        for p in pts:
            if not res or abs(res[-1][0] - p[0]) + abs(res[-1][1] - p[1]) > 0.6:
                res.append(p)
        return res

    @staticmethod
    def d(pts):
        return "M" + " L".join(f"{f(x)},{f(y)}" for x, y in pts)

    # ---------- 部品描画 ----------
    def port_svg(self, x, y, p, show_name=False, inner=False):
        role = p.get("role")
        s = ""
        if role in ("M", "S"):
            fill, tc = (INK, "#fff") if role == "M" else ("#fff", INK)
            s += (f'<rect x="{f(x-7)}" y="{f(y-7)}" width="14" height="14" rx="2" fill="{fill}" '
                  f'stroke="{INK}" stroke-width="1.4"/>'
                  f'<text x="{f(x)}" y="{f(y+3.6)}" font-size="9.5" font-weight="700" fill="{tc}" '
                  f'text-anchor="middle">{role}</text>')
        elif p.get("dir") == "inout":
            s += (f'<path d="M{f(x)},{f(y-6)} L{f(x+6)},{f(y)} L{f(x)},{f(y+6)} L{f(x-6)},{f(y)} z" '
                  f'fill="#fff" stroke="{INK}" stroke-width="1.4"/>')
        else:
            s += (f'<rect x="{f(x-4)}" y="{f(y-4)}" width="8" height="8" fill="#fff" '
                  f'stroke="#475569" stroke-width="1.2"/>')
        if show_name and p.get("name"):
            # 端子名はブロックの内側に書く(外側は接続線の注記に使う)
            if inner:   # 最上位ブロックの端子 → 内側(外側はバス接続の注記が使う)
                dx, dy, anc = {"N": (6, 17, "start"), "S": (6, -9, "start"),
                               "W": (10, -6, "start"), "E": (-10, -6, "end")}[p["side"]]
            else:       # 子ブロックの端子 → 外側(小さい枠のラベルと重ねない)
                dx, dy, anc = {"N": (6, -8, "start"), "S": (6, 15, "start"),
                               "W": (-9, -8, "end"), "E": (9, -8, "start")}[p["side"]]
            s += (f'<text x="{f(x+dx)}" y="{f(y+dy)}" font-size="10" fill="{GRAY}" '
                  f'text-anchor="{anc}">{esc(p["name"])}</text>')
        return s

    def block_svg(self, b, open_children):
        x, y, w, h = b["rect"]
        if b.get("virtual"):      # バスどうしの直接接続: 箱は描かず、上下の端子を線でつなぐ
            return self.virtual_lines(b)
        if b.get("kind") == "fifo" and not b["_ext"]:
            return self.fifo_block(b)
        if b["_parent"] and self.blocks[b["_parent"]].get("pack"):
            return self.cell_svg(b)
        bar = self.bus_bar(b)
        kind = "ext" if b["_ext"] else ("child" if b["_depth"] > 0 and b.get("kind") not in ("memory", "fifo")
                                       else b.get("kind", "logic"))
        st = dict(STYLE.get(kind, STYLE["logic"]))
        status = b.get("status")
        if status in ("tbd", "assumed"):
            st.update(stroke="#94a3b8", dash="6 4", fill="#f8fafc" if status == "tbd" else st["fill"])
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        rx = 4 if kind != "child" else 3
        s = ""
        n_ = int(b.get("count", 1) or 1)
        for k in range(min(n_ - 1, 3), 0, -1):   # 複数個: 右上へ斜めにずらして重ねる
            s += (f'<rect x="{f(x+5*k)}" y="{f(y-5*k)}" width="{f(w)}" height="{f(h)}" rx="{rx}" fill="{st["fill"]}" '
                  f'stroke="{st["stroke"]}" stroke-width="{st["sw"]}"/>')
        s += (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" rx="{rx}" fill="{st["fill"]}" '
             f'stroke="{st["stroke"]}" stroke-width="{st["sw"]}"{dash}/>')
        s += bar
        if n_ > 1:      # 中に I$/D$ があれば、名前の下(右上)に
            ny_ = y + 38 if b.get("attached") else y + h - 6
            s += (f'<text x="{f(x+w-6 if not b.get("attached") else x+w-10)}" y="{f(ny_)}" font-size="11" font-weight="800" fill="{INK}" '
                  f'text-anchor="end">×{n_}</text>')
        if b.get("kind") == "fifo":     # FIFO記号: 左端に縦のスロット
            for k in range(4):
                xx = x + 7 + k * 4.5
                s += f'<line x1="{f(xx)}" y1="{f(y+8)}" x2="{f(xx)}" y2="{f(y+h-8)}" stroke="{st["stroke"]}" stroke-width="1.6"/>'
        if b.get("kind") == "memory" and not b.get("_att_of"):   # メモリ記号: 左端にワード行(コアの中の小枠は省く)
            for k in range(4):
                yy = y + 10 + k * (h - 20) / 3
                s += (f'<line x1="{f(x+7)}" y1="{f(yy)}" x2="{f(x+19)}" y2="{f(yy)}" '
                      f'stroke="{st["stroke"]}" stroke-width="1.6"/>')
            s += (f'<line x1="{f(x+25)}" y1="{f(y+6)}" x2="{f(x+25)}" y2="{f(y+h-6)}" '
                  f'stroke="{st["stroke"]}" stroke-width="1" opacity=".5"/>')
        if status in ("tbd", "assumed"):
            t = "TBD" if status == "tbd" else "仮"
            tw = 30 if status == "tbd" else 22
            s += (f'<rect x="{f(x+w-tw-4)}" y="{f(y+4)}" width="{tw}" height="16" rx="3" fill="#f59e0b"/>'
                  f'<text x="{f(x+w-4-tw/2)}" y="{f(y+16)}" font-size="10.5" font-weight="800" fill="#fff" '
                  f'text-anchor="middle">{t}</text>')
        fs = 12 if b.get("_att_of") else (13 if b["_depth"] > 0 else 14.5)
        has_kids = bool(b.get("children")) or bool(b.get("attached"))   # 中に枠(子・I$/D$)→ 名前は右上
        cx = x + w / 2 + (8 if b.get("kind") in ("memory", "fifo") and not b.get("_att_of") else 0)
        if has_kids and (open_children or b.get("attached")):   # 子を表示する時はラベルを右上へ
            s += (f'<text x="{f(x+w-10)}" y="{f(y+20)}" font-size="{fs}" font-weight="700" '
                  f'fill="{INK}" text-anchor="end">{esc(b["label"])}</text>')
        else:
            sub = b.get("sub")
            cyy = y + h / 2
            for ps in self.passes.get(b["id"], []):
                if ps[0] == "V":   # 縦に貫通 → 広い側の中央へ
                    px = ps[1]
                    side_w = max(px - x, x + w - px) - 14
                    k_ = 0.72
                    if wlen(b["label"]) * fs * k_ > side_w:   # 片側に収まらなければ文字を小さく
                        fs = max(10, side_w / (wlen(b["label"]) * k_))
                    lw = wlen(b["label"]) * fs * k_
                    if px - x > x + w - px:      # 左が広い: 線の左に収める
                        cx = max(min((x + px) / 2, px - 7 - lw / 2), x + 4 + lw / 2)
                    else:
                        cx = min(max((px + x + w) / 2, px + 7 + lw / 2), x + w - 4 - lw / 2)
                else:              # 横に貫通 → 上下の広い側へ
                    top, bot = ps[1] - 6 - y, y + h - ps[2] - 6
                    cyy = y + top / 2 if top >= bot else y + h - bot / 2
            if b["id"] in getattr(self, "mark_hosts", set()):
                topN = any(p.get("side") == "N" for p in b.get("ports", []))
                cyy = y + 9 + (11 if topN else 0) + (7 if sub else 0)   # 中のCDC/FIFO枠・上辺の端子と重ねない
            ly = cyy + (fs * 0.35) - (7 if sub else 0)
            if b.get("_att_of"):   # コアの中の小枠: 下辺の端子と重ねないよう上寄せ
                ly = y + 16
            s += (f'<text x="{f(cx)}" y="{f(ly)}" font-size="{fs}" font-weight="700" fill="{INK}" '
                  f'text-anchor="middle">{esc(b["label"])}</text>')
            if sub:
                s += (f'<text x="{f(cx)}" y="{f(ly+(13 if b.get("_att_of") else 16))}" font-size="{10.5 if b.get("_att_of") else 11.5}" fill="{GRAY}" '
                      f'text-anchor="middle">{esc(sub)}</text>')
            if has_kids:   # 折りたたみ記号: 中に詳細図があることを示す
                s += (f'<g transform="translate({f(x+w-24)},{f(y+h-24)})"><rect width="16" height="16" '
                      f'rx="2" fill="#fff" stroke="{GRAY}"/><path d="M4,8 H12 M8,4 V12" '
                      f'stroke="{GRAY}" stroke-width="1.6"/></g>')
        return s

    def marker_defs(self):
        s = (f'<marker id="ahk" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
             f'markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,1 L10,5 L0,9 z" '
             f'fill="{INK}"/></marker>')
        for c in sorted(self.colors):
            s += (f'<marker id="ah{c[1:]}" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="3.4" '
                  f'markerHeight="3.4" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>')
        return f"<defs>{s}</defs>"

    # ---------- レイヤ ----------
    def visible(self, bid):
        """この図で描くブロックか(トップ=深さ1まで / 詳細図=対象の下2段まで)"""
        b = self.blocks.get(bid)
        if b is None:
            return True
        if b.get("_hidden"):
            return False
        if b["_depth"] <= 1:
            return True
        f_ = getattr(self, "focus", None)
        if not f_:
            return False
        a, d = b, 0
        while a is not None:
            if a["id"] == f_:
                return d <= 2
            a = self.blocks.get(a["_parent"]) if a["_parent"] else None
            d += 1
        return False

    def bus_texts(self, bus):
        """バス名(右寄せ)と性能の文字の位置: ((名前x, 名前y), (性能x, 性能y))。
        端子の記号・CDC/FIFO・アドレスの文字・(動作図があれば)矢印が入る端子の列を避け、
        横の位置 × 高さ(上・中・下)の候補から重なりが最小のものを選ぶ"""
        cache = self.__dict__.setdefault("_bt_cache", {})
        if bus["id"] in cache:
            return cache[bus["id"]]
        bx, by, bw, bh = bus["rect"]
        flows = any(st.get("flows") for st in self.steps)
        occ = []
        for x, y, v, l_, d in self.cdc_points():
            if bx - 2 <= x <= bx + bw + 2 and by - 2 <= y <= by + bh + 2:
                bb = self.mark_box(x, y, l_, v)
                occ.append(bb if "FIFO" not in l_ else (bb[0], bb[1], bb[2] + wlen(l_) * 5.3 + 6, bb[3]))
        cols = []
        for a in bus.get("attach", []):
            if a not in self.anch:
                continue
            px, py, jx, jy, vert = self.stub(bus, a)
            rr = self.attach_addr(bus, a)
            if vert:
                cols.append(jx)
                occ.append((jx - 16, jy - 9, jx + 16, jy + 9))           # 端子の番号
                if flows:
                    occ.append((jx - 6, by, jx + 6, by + bh))            # 矢印が入る列
                if rr:
                    d = 1 if py < jy else -1
                    n_ = 3 if (rr != ["default"] and len(rr) > 1) else 2
                    y1 = jy + d * 24
                    ys = (y1 - 9, y1 + 10 * (n_ - 1) + 2) if d > 0 else (y1 - 10 * (n_ - 1) - 9, y1 + 2)
                    occ.append((jx - 64, ys[0], jx + 34, ys[1]))
            else:
                occ.append((jx - 16, jy - 9, jx + 16, jy + 9))
                if rr:
                    occ.append((jx - 130, py - 20, jx + 130, py - 4) if True else None)
        def score(box):
            return sum(inter(box, o) for o in occ) + (1e6 if box[0] < bx + 4 or box[2] > bx + bw - 4 else 0)
        ys_ = [by + bh / 2 + 4.5, by + 16, by + bh - 7]
        nw = wlen(bus["label"]) * 12.5 * 0.68
        lab = bus_perf_text(bus) or ""
        pw = wlen(lab) * 12.5 * 0.6
        xr_c = [bx + bw - 12] + [x - 14 for x in sorted(cols, reverse=True)]
        xl_c = [bx + 12] + [x + 14 for x in sorted(cols)]
        best = None
        for y in ys_:
            for x in xr_c:
                bb = (x - nw, y - 11, x, y + 3)
                sc = score(bb)
                if best is None or sc < best[0] - 1e-6:
                    best = (sc, x, y, bb)
        _, xr, yr, nb = best
        occ.append((nb[0] - 12, nb[1], nb[2], nb[3]))
        best = None
        if lab:
            for y in ys_:
                for x in xl_c:
                    bb = (x, y - 11, x + pw, y + 3)
                    sc = score(bb)
                    if best is None or sc < best[0] - 1e-6:
                        best = (sc, x, y, bb)
            _, xl, yl, pb = best
        else:
            xl, yl, pb = bx + 12, yr, None
        cache[bus["id"]] = ((xr, yr), (xl, yl))
        self.__dict__.setdefault("_bt_boxes", {})[bus["id"]] = [nb] + ([pb] if pb else [])
        self.__dict__.setdefault("_bt_occ", {})[bus["id"]] = [o for o in occ if o[3] - o[1] < bh - 2]
        return cache[bus["id"]]

    def text_gap(self, bus, tw, start, from_right=False, limit=None):
        """動作の矢印がバスへ入る位置(端子の x)を避けて、幅 tw の文字を置ける位置。
        矢印がない図ではそのまま start を返す"""
        if not any(st.get("flows") for st in self.steps):
            return start
        bx, by, bw, bh = bus["rect"]
        xs = sorted(self.stub(bus, a)[2] for a in bus.get("attach", []) if a in self.anch and self.stub(bus, a)[4])
        if not from_right:
            x0 = start
            for x in xs + [bx + bw + 1e9]:
                if x - 12 >= x0 + tw:
                    return x0 if limit is None or x0 + tw <= limit else None
                if x + 12 > x0:
                    x0 = x + 12
            return start
        x1 = start                      # 右寄せ: 右端 x1 から左へ tw
        for x in sorted(xs, reverse=True) + [-1e9]:
            if x + 12 <= x1 - tw:
                return x1
            if x - 12 < x1:
                x1 = x - 12
        return start

    def flow_cy(self, bus):
        """動作の矢印がバスの中を通る高さ: バス名・性能の文字と重ねない"""
        bx, by, bw, bh = bus["rect"]
        self.bus_texts(bus)
        boxes = self.__dict__.get("_bt_boxes", {}).get(bus["id"], [])
        mid = by + bh / 2
        others = self.__dict__.get("_bt_occ", {}).get(bus["id"], [])
        best = None
        for cy in (mid, min(mid + 16, by + bh - 12), max(mid - 16, by + 12), by + 12, by + bh - 12):
            sc = sum(10 for b_ in boxes if b_[1] - 6 < cy < b_[3] + 6) + \
                 sum(1 for b_ in others if b_[1] - 5 < cy < b_[3] + 5)      # 文字(名前・性能 > アドレス)と重ならない高さ
            if best is None or sc < best[0]:
                best = (sc, cy)
        return best[1]

    def bus_text_y(self, bus):
        """バス名・性能の文字の高さ: アドレス表記が片側だけなら反対側に寄せる(重ならない)"""
        bx, by, bw, bh = bus["rect"]
        sides = set()
        for a in bus.get("attach", []):
            if self.attach_addr(bus, a):
                px, py, jx, jy, vert = self.stub(bus, a)
                if vert:
                    sides.add("top" if abs(jy - by) < 1 else "bot")
        if sides == {"bot"}:
            return by + 16
        if sides == {"top"}:
            return by + bh - 7
        return by + bh / 2 + 4.5

    def base_layer(self, detail=False, interactive=False, open_children=True):
        S = self.s
        self.mark_hosts = set()
        self.cdc_points(detail)   # mark_hosts を確定
        out = []
        cx, cy, cw, ch = S["chip"]["rect"]
        out.append(f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="6" fill="#fcfcfd" '
                   f'stroke="{INK}" stroke-width="2.6"/>')
        out.append(f'<text x="{cx+12}" y="{cy+22}" font-size="15" font-weight="800" fill="{INK}">'
                   f'{esc(S["chip"]["label"])}</text>')
        # バス
        for bus in self.buses.values():
            bx, by, bw, bh = bus["rect"]
            bd = ' stroke-dasharray="6 4"' if bus.get("status") in ("tbd", "assumed") else ""
            bc = self.bus_color[bus["id"]]
            out.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="3" fill="#fff"/>'
                       f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="3" fill="{bc}" fill-opacity=".13" '
                       f'stroke="{bc}" stroke-width="1.8"{bd}/>')
            (xr_, yr_), _ = self.bus_texts(bus)
            out.append(f'<text x="{f(xr_)}" y="{f(yr_)}" font-size="12.5" font-weight="800" '
                       f'fill="{self.bus_color[bus["id"]]}" text-anchor="end" letter-spacing=".5">{esc(bus["label"])}</text>')
            for a in bus.get("attach", []):
                px, py, jx, jy, vert = self.stub(bus, a)
                role = (self.anch[a][4] or {}).get("role")
                arrow = bus.get("arrow", "request")   # request: M→S の向き / both / none
                pts = [(px, py), (jx, jy)]
                if role == "S":
                    pts = pts[::-1]
                virt_ = (self.blocks.get(a.split(".")[0]) or {}).get("virtual")
                if virt_:   # 直接接続: 途中は切らずにつなぎ、矢印は受け側のバスに入る所だけ
                    pts = self.trim(pts, 8, 0) if role == "S" else self.trim(pts, 0, 9)
                    if role == "S":
                        arrow = "none"
                else:
                    pts = self.trim(pts, 8, 9)
                mk = ""
                if arrow == "both":
                    mk = ' marker-start="url(#ahk)" marker-end="url(#ahk)"'
                elif arrow != "none" and role in ("M", "S"):
                    mk = ' marker-end="url(#ahk)"'
                pw = self.port_width(bus, a)
                wide = bus.get("width") and pw and pw != bus.get("width")
                if virt_ and role == "S":   # 直接接続: 線全体を受け側の幅変換の色にそろえる
                    pair = dict(self.virtual_pairs(self.blocks[a.split(".")[0]]))
                    v_ = pair.get(a)
                    bv = next((x for x in self.buses.values() if v_ in x.get("attach", [])), None)
                    if bv:
                        pv = self.port_width(bv, v_)
                        wide = bv.get("width") and pv and pv != bv.get("width")
                col = "#b45309" if wide else "#475569"
                out.append(f'<path d="{self.d(pts)}" fill="none" stroke="{col}" stroke-width="2.4"{mk}/>')
        links = []
        self._routes = self.link_routes(detail)
        for li, ln in enumerate(S.get("links", [])):
            lvl = ln.get("level", 0)
            if lvl > 0 and not detail:
                continue
            ea, eb = self.anch.get(ln["from"]), self.anch.get(ln["to"])
            if (ea is None and ln["from"] not in self.blocks) or (eb is None and ln["to"] not in self.blocks):
                continue   # 端点がこの図に無い(より深い階層)
            if (ea and ea[3] and not self.visible(ea[3])) or (eb and eb[3] and not self.visible(eb[3])):
                continue
            pts = self._routes.get(li) or self.polyline(self.nodes([ln["from"], ln["to"]]))
            d_ = self.link_dir(ln)
            mk = ""
            if d_ == "both":
                mk = ' marker-start="url(#ahk)" marker-end="url(#ahk)"'
                pts = self.trim(pts, 7, 7)
            elif d_ == "fwd":
                mk = ' marker-end="url(#ahk)"'
                pts = self.trim(pts, 0, 7)
            cls = ' class="lvl1"' if lvl > 0 else ""
            links.append(f'<path{cls} d="{self.d(pts)}" fill="none" stroke="#475569" stroke-width="1.6"{mk}/>')
        # ブロック
        def walk_kids(b, inner):
            for c in b.get("children", []):
                if not self.visible(c["id"]):
                    continue
                inner.append(self.block_svg(c, True))
                walk_kids(c, inner)

        kids, top = [], []
        for b in S.get("external", []) + S.get("blocks", []):
            if interactive and b.get("children"):
                top.append(f'<g class="h-open">{self.block_svg(b, True)}</g>'
                           f'<g class="h-closed">{self.block_svg(b, False)}</g>')
            else:
                top.append(self.block_svg(b, open_children))
            if open_children or interactive:
                walk_kids(b, kids)
        out += top
        out += links
        over = []
        # ポート
        port_top, port_kid = [], []
        vis = {a for bus in self.buses.values() for a in bus.get("attach", [])}
        for ln in S.get("links", []):
            if ln.get("level", 0) == 0 or detail:
                vis |= {ln["from"], ln["to"]}
        for key, (x, y, side, bid, p) in self.anch.items():
            if bid is None or (key not in vis and not p.get("show")) or not self.visible(bid):
                continue   # 結線のない端子は描かない(機能だけを示す枠は端子なし)
            if self.blocks[bid].get("virtual"):
                continue      # バスどうしの直接接続: 端子記号はバス側の番号だけ
            top_ = self.blocks[bid]["_depth"] == 0
            (port_top if top_ else port_kid).append(self.port_svg(x, y, p, detail, inner=top_))
        if interactive:
            out.append('<g class="h-open">' + "".join(kids) + "</g>")
            over.append('<g class="h-open">' + "".join(port_kid) + "</g>")
        elif open_children:
            out += kids
            over += port_kid
        over += port_top
        # CDC(非同期の乗り換え)
        cps = self.cdc_points(detail)
        for x, y, tx, ty in self.cdc_leaders:
            over.append(f'<path d="M{f(x)},{f(y)} L{f(tx)},{f(y)} L{f(tx)},{f(ty)}" fill="none" stroke="#dc2626" '
                        f'stroke-width="1" stroke-dasharray="2 2"/>')
        occ_m = self.occupancy("".join(out) + "".join(links) + "".join(over), detail)
        for x, y, vert, lab, _ in cps:   # 記号を先に登録(文字が記号に重ならないように)
            b_ = self.mark_box(x, y, lab, vert)
            occ_m.append(((b_[0] - 3, b_[1] - 3, b_[2] + 3, b_[3] + 3), None))
            j = self._mjunc.get((round(x, 1), round(y, 1)))
            if j and "FIFO" in lab:   # 接点から FIFO を通って先へ続く線
                gw, gh = self.fifo_dims(vert)
                if vert:
                    occ_m.append(((x - 2, min(j[1], y - gh / 2 - 14), x + 2, max(j[1], y + gh / 2 + 14)), None))
                else:
                    occ_m.append(((min(j[0], x - gw / 2 - 14), y - 2, max(j[0], x + gw / 2 + 14), y + 2), None))
        for x, y, vert, lab, _ in cps:
            if "FIFO" in lab:
                host = self._mhost.get((round(x, 1), round(y, 1)))
                skip = set()
                while host:
                    skip.add(host)
                    host = (self.blocks.get(host) or {}).get("_parent")
                over.append(self.fifo_mark(x, y, vert, lab, occ_m, skip))
                continue
            w_, h_ = self.mark_w(lab), 13
            over.append(f'<rect x="{f(x-w_/2)}" y="{f(y-h_/2)}" width="{f(w_)}" height="{h_}" rx="2" fill="#fff" '
                        f'stroke="#dc2626" stroke-width="1.4" stroke-dasharray="3 2"/>'
                        f'<text x="{f(x)}" y="{f(y+3.2)}" font-size="9" font-weight="800" fill="#dc2626" '
                        f'text-anchor="middle">{esc(lab)}</text>')
        # インターコネクト側にアドレス範囲(スレーブへ出るポートの内側)
        for bus in self.buses.values():
            bx, by, bw, bh = bus["rect"]
            for a in bus.get("attach", []):
                rr = self.attach_addr(bus, a)
                if not rr:
                    continue
                if rr == ["default"]:
                    lines = ["その他", "全て"]
                else:
                    lines = [hx(rr[0][0]), "–" + hx(rr[0][1])] + ([f"+{len(rr) - 1}範囲"] if len(rr) > 1 else [])
                r = None if rr == ["default"] else rr[0]
                px, py, jx, jy, vert = self.stub(bus, a)
                if vert:
                    d = 1 if py < jy else -1
                    marked = any(abs(m[0] - jx) < 1 and abs(m[1] - jy) < 60 for m in cps)
                    # 先頭アドレス → 末尾アドレス の順に上から読めるように。CDC/FIFO があれば線の左に寄せる
                    tx, anc = (jx - 17, "end") if marked else (min(max(jx, bx + 34), bx + bw - 34), "middle")
                    y1 = jy + d * 24 + (3 if d > 0 else 0)
                    n_ = len(lines)
                    ys = [y1 + 10 * k for k in range(n_)] if d > 0 else [y1 - 10 * (n_ - 1 - k) for k in range(n_)]
                    for t_, yy in zip(lines, ys):
                        over.append(f'<text x="{f(tx)}" y="{f(yy)}" font-size="8.6" font-weight="700" fill="#334155" '
                                    f'text-anchor="{anc}" font-family="Consolas,Menlo,monospace" paint-order="stroke" '
                                    f'stroke="#e2e8f0" stroke-width="3">{t_}</text>')
                else:
                    tx = jx + (14 if px < jx else -14)
                    over.append(f'<text x="{f(tx)}" y="{f(py - 9)}" font-size="8.6" fill="#334155" '
                                f'text-anchor="{"start" if px < jx else "end"}" font-family="Consolas,Menlo,monospace">'
                                f'{range_text(r) if r else "その他全て"}{(" +" + str(len(rr) - 1)) if r and len(rr) > 1 else ""}</text>')
        # クロスバー側のポート(番号付き)
        for (bid_, a), (brole, lab) in self.bus_ports.items():
            if not lab or a not in self.anch:
                continue
            bx, by, bw, bh = self.buses[bid_]["rect"]
            x, y = self.anch[a][:2]
            if bx <= x <= bx + bw:
                jx, jy = x, (by if y < by else by + bh)
            else:
                jx, jy = (bx if x < bx else bx + bw), y
            w_ = wlen(lab) * 6.4 + 8
            fill, tc = (INK, "#fff") if brole == "M" else ("#fff", INK)
            over.append(f'<rect x="{f(jx-w_/2)}" y="{f(jy-7)}" width="{f(w_)}" height="14" rx="2" fill="{fill}" '
                        f'stroke="{INK}" stroke-width="1.3"/><text x="{f(jx)}" y="{f(jy+3.5)}" font-size="9" '
                        f'font-weight="700" fill="{tc}" text-anchor="middle">{esc(lab)}</text>')
        # パッド
        for p in S.get("pads", []):
            x, y = p["at"]
            pd = ' stroke-dasharray="3 2"' if p.get("status") in ("tbd", "assumed") else ""
            over.append(f'<rect x="{x-8}" y="{y-8}" width="16" height="16" fill="#cbd5e1" stroke="{INK}" stroke-width="1.6"{pd}/>')
            if p["side"] in ("W", "E"):
                over.append(f'<text x="{x}" y="{y-13}" font-size="11.5" font-weight="700" fill="{INK}" text-anchor="middle" '
                            f'paint-order="stroke" stroke="#fff" stroke-width="3">{esc(p["label"])}</text>')
            elif p["side"] == "N":
                over.append(f'<text x="{x}" y="{y-13}" font-size="11.5" font-weight="700" fill="{INK}" text-anchor="middle">{esc(p["label"])}</text>')
            else:
                over.append(f'<text x="{x+14}" y="{y+18}" font-size="11.5" font-weight="700" fill="{INK}" '
                            f'paint-order="stroke" stroke="#fff" stroke-width="3">{esc(p["label"])}</text>')
        return "".join(out), "".join(over)

    def _tag(self, x, y, text, anchor="middle"):
        tw = wlen(text) * 6.1 + 10
        x0 = x - tw / 2 if anchor == "middle" else (x if anchor == "start" else x - tw)
        return (f'<rect x="{f(x0)}" y="{f(y-8.5)}" width="{f(tw)}" height="17" rx="3" fill="#fff" '
                f'stroke="{PERF}" stroke-width="1"/><text x="{f(x0+tw/2)}" y="{f(y+3.8)}" font-size="10.5" '
                f'font-weight="700" fill="{PERF}" text-anchor="middle">{esc(text)}</text>')

    def _ptxt(self, x, y, text, anchor="middle", rot=False, col=None, size=10.5):
        tr = f' transform="rotate(-90 {f(x)} {f(y)})"' if rot else ""
        return (f'<text x="{f(x)}" y="{f(y)}" font-size="{size}" font-weight="700" fill="{col or PERF}" '
                f'text-anchor="{anchor}" paint-order="stroke" stroke="#fff" stroke-width="3"{tr}>{esc(text)}</text>')

    @staticmethod
    def perf_parts(width, clock, bw):
        a = " ".join(x for x in [f"{width}b" if width else None, f"@{clock}" if clock else None] if x)
        return a, bw

    def occupancy(self, svg_parts, detail=False):
        """注記の配置で避ける領域: 既存の文字・端子記号・線・バス・最上位ブロック"""
        occ = []
        for m in TEXT_RX.finditer(svg_parts):
            at, txt = m.group(1), html.unescape(m.group(2))
            g = lambda k, d=None: (re.search(k + r'="([^"]*)"', at) or [None, d])[1]
            try:
                occ.append((text_box(float(g("x")), float(g("y")), float(g("font-size", "12")), txt,
                                     g("text-anchor", "start"), "rotate(-90" in at), None))
            except (TypeError, ValueError):
                pass
        for m in RECT_RX.finditer(svg_parts):   # 小さい矩形 = 端子・番号・CDC/FIFO 記号
            x, y, w, h = map(float, m.groups())
            if w <= 70 and h <= 20:
                occ.append(((x, y, x + w, y + h), None))
        for bus in self.buses.values():
            bx, by, bw, bh = bus["rect"]
            occ.append(((bx, by, bx + bw, by + bh), bus["id"]))
            for a in bus.get("attach", []):
                px, py, jx, jy, v = self.stub(bus, a)
                occ.append(((min(px, jx) - 1.5, min(py, jy) - 1.5, max(px, jx) + 1.5, max(py, jy) + 1.5), None))
        rts = getattr(self, "_routes", {})
        for li, ln in enumerate(self.s.get("links", [])):
            if ln.get("level", 0) > 0 and not detail:
                continue
            if ln["from"] not in self.anch or ln["to"] not in self.anch:
                continue
            pts = rts.get(li) or self.polyline(self.nodes([ln["from"], ln["to"]]))
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                occ.append(((min(x1, x2) - 1.5, min(y1, y2) - 1.5, max(x1, x2) + 1.5, max(y1, y2) + 1.5), None))
        for b in self.blocks.values():
            if b.get("_hidden"):
                continue
            x, y, w, h = b["rect"]
            occ.append(((x, y, x + w, y + h), b["id"]))
        for (bid_, a), (brole, lab) in getattr(self, "bus_ports", {}).items():   # バス側の端子番号(後で描く)
            if lab and a in self.anch and bid_ in self.buses:
                px, py, jx, jy, v = self.stub(self.buses[bid_], a)
                w_ = wlen(lab) * 6.4 + 8
                occ.append(((jx - w_ / 2, jy - 8, jx + w_ / 2, jy + 8), None))
        cx, cy, cw, ch = self.s["chip"]["rect"]   # チップの枠の外(左右上下)には注記を出さない
        occ += [((cx - 400, cy - 400, cx + 2, cy + ch + 400), None), ((cx + cw - 2, cy - 400, cx + cw + 400, cy + ch + 400), None)]
        return occ

    def place(self, cands, occ, skip=(), col=None):
        """候補(文字の並びのリスト)から、重なりが最小のものを選んで occ に登録"""
        best, best_sc = None, None
        for cand in cands:
            boxes = [text_box(x, y, fs, t, anc, rot) for x, y, t, anc, rot, fs, maxlen in cand]
            sc = sum(inter(b_, o) for b_ in boxes for o, tag in occ if tag not in skip)
            sc += sum(1e5 for (x, y, t, anc, rot, fs, maxlen), b_ in zip(cand, boxes)
                      if maxlen and (b_[3] - b_[1] if rot else b_[2] - b_[0]) > maxlen)   # 線より長い
            if best_sc is None or sc < best_sc:
                best, best_sc = cand, sc
            if sc == 0:
                break
        for (x, y, t, anc, rot, fs, maxlen) in best:
            occ.append((text_box(x, y, fs, t, anc, rot), None))
        return "".join(self._ptxt(x, y, t, anc, rot, col, fs) for x, y, t, anc, rot, fs, maxlen in best)

    def perf_layer(self, detail=False, open_children=True, interactive=False, occ=None):
        """クロック周波数・バス幅・帯域の注記(枠なし文字、独立レイヤ)。
        向きは固定しない: 縦(回転)/横、線の左右、ポート寄り などの候補から重ならないものを選ぶ"""
        out, kid = [], []
        occ = list(occ or [])
        for bus in self.buses.values():
            bx, by, bw, bh = bus["rect"]
            lab = bus_perf_text(bus)
            def ext_(x, y, v, l_):   # 記号+その文字の右端
                b_ = self.mark_box(x, y, l_, v)
                return b_[2] + (wlen(l_) * 5.3 + 6 if "FIFO" in l_ else 0)
            lpad = max([12] + [ext_(x, y, v, l_) + 10 - bx for x, y, v, l_, d in self.cdc_points()
                               if not v and by <= y <= by + bh and x < bx + bw / 2])
            if lab:   # バスの枠に直接書く
                _, (xl_, yl_) = self.bus_texts(bus)
                out.append(f'<text x="{f(xl_)}" y="{f(yl_)}" font-size="12.5" font-weight="700" '
                           f'fill="{PERF}">{esc(lab)}</text>')
            # 各接続: ポート側の 幅 @クロック / 帯域
            for a in bus.get("attach", []):
                px, py, jx, jy, vert = self.stub(bus, a)
                p = self.anch[a][4] or {}
                own = self.owner_clock(a) or {}
                width = self.port_width(bus, a)
                clock = p.get("clock") or own.get("clock")
                bwt = p.get("bw") or (fmt_bw(width / 8 * hz(clock)) if width and hz(clock) else None)
                l1, l2 = self.perf_parts(width, clock, bwt)
                if not (l1 or l2):
                    continue
                wide = bus.get("width") and width and width != bus.get("width")
                col = "#b45309" if wide else PERF
                L1 = [x for x in (l1, l2) if x]
                one = " ".join(L1)
                skip = {bus["id"]}
                if vert:
                    d = 1 if jy > py else -1
                    s0, s1 = py + 9 * d, jy - 12 * d
                    my, L = (s0 + s1) / 2, abs(s1 - s0)
                    near = py + 16 * d
                    cands = []
                    if l1 and l2:
                        cands.append([(px - 5, my, l1, "middle", True, 10.5, L), (px + 13, my, l2, "middle", True, 10.5, L)])
                    cands += [
                        [(px + 13, my, one, "middle", True, 10, L)],
                        [(px - 5, my, one, "middle", True, 10, L)],
                        [(px + 7, my - 2 + i * 11, t, "start", False, 9.5, None) for i, t in enumerate(L1)],
                        [(px - 7, my - 2 + i * 11, t, "end", False, 9.5, None) for i, t in enumerate(L1)],
                        [(px + 7, near + d * i * 11, t, "start", False, 9.5, None) for i, t in enumerate(L1)],
                        [(px - 7, near + d * i * 11, t, "end", False, 9.5, None) for i, t in enumerate(L1)],
                        [(px + 7, my + 3, one, "start", False, 9, None)],
                        [(px - 7, my + 3, one, "end", False, 9, None)],
                    ]
                else:
                    mx = (px + jx) / 2
                    cands = [
                        [(mx, py - 19 + i * 11, t, "middle", False, 9.5, None) for i, t in enumerate(L1[-2:])],
                        [(mx, py + 16 + i * 11, t, "middle", False, 9.5, None) for i, t in enumerate(L1)],
                        [(mx, py - 7, one, "middle", False, 9, None)],
                        [(mx, py + 15, one, "middle", False, 9, None)],
                    ]
                out.append(self.place(cands, occ, skip, col))
        for b in self.blocks.values():
            if b.get("clock") and not b["_ext"] and self.visible(b["id"]) and not b.get("virtual") \
                    and not (b.get("_att_of") and self.blocks[b["_att_of"]].get("clock") == b["clock"]):
                x, y, w, h = b["rect"]   # 引っ付けた枠は親と同じクロックなら書かない(親の枠に書く)
                off = 30 if b.get("kind") == "memory" else 7
                ty_ = y + 18 if b.get("attached") else y + h - 7      # 中に I$/D$ があれば左上
                t = (f'<text x="{f(x+off)}" y="{f(ty_)}" font-size="10.5" font-weight="700" fill="{PERF}">'
                     f'{esc(b["clock"])}</text>')
                (kid if b["_depth"] > 0 else out).append(t)
        for ln in self.s.get("links", []):
            if ln.get("level", 0) > 0 and not detail:
                continue
            ea, eb = self.anch.get(ln["from"]), self.anch.get(ln["to"])
            if ea is None or eb is None:
                continue
            if (ea[3] and not self.visible(ea[3])) or (eb[3] and not self.visible(eb[3])):
                continue
            bwt = bw_text(ln)
            l1, l2 = self.perf_parts(ln.get("width"), ln.get("clock"), bwt)
            if ln.get("label"):   # 名前付き接続(例: I-bus)
                l1 = f"{ln['label']} {l1}".strip()
            txt = " ".join(x for x in (l1, l2) if x)
            if not txt:
                continue
            pts = getattr(self, "_routes", {}).get(self.s["links"].index(ln)) or self.polyline(self.nodes([ln["from"], ln["to"]]))
            (x1, y1), (x2, y2) = max(zip(pts, pts[1:]), key=lambda s_: abs(s_[0][0]-s_[1][0]) + abs(s_[0][1]-s_[1][1]))
            vert = abs(x1 - x2) < 1
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            L = abs(y1 - y2) if vert else abs(x1 - x2)
            L1 = [x for x in (l1, l2) if x]
            if vert:
                cands = [[(mx - 5, my, txt, "middle", True, 10, L - 16)], [(mx + 13, my, txt, "middle", True, 10, L - 16)],
                         [(mx + 7, my - 2 + i * 11, t, "start", False, 9.5, None) for i, t in enumerate(L1)],
                         [(mx - 7, my - 2 + i * 11, t, "end", False, 9.5, None) for i, t in enumerate(L1)]]
            else:
                cands = [[(mx, my - 6, txt, "middle", False, 10, L - 16)], [(mx, my + 14, txt, "middle", False, 10, L - 16)],
                         [(mx, my - 17 + i * 11, t, "middle", False, 9.5, None) for i, t in enumerate(L1)],
                         [(mx, my + 13 + i * 11, t, "middle", False, 9.5, None) for i, t in enumerate(L1)]]
            skip = set()
            for e in (ln["from"], ln["to"]):
                bid = self.anch.get(e, (0, 0, 0, None))[3]
                while bid:
                    skip.add(bid)
                    bid = self.blocks[bid]["_parent"]
            (kid if ln.get("level", 0) else out).append(self.place(cands, occ, skip))
        for p in self.s.get("pads", []):
            t = bw_text(p)
            if not t:
                continue
            x, y = p["at"]
            px, py, anc = {"W": (x, y + 22, "middle"), "E": (x, y + 22, "middle"),
                           "S": (x + 14, y + 32, "start"), "N": (x + 14, y - 28, "start")}[p["side"]]
            out.append(self._ptxt(px, py, t, anc))
        if interactive:
            return "".join(out) + '<g class="h-open">' + "".join(kid) + "</g>"
        return "".join(out) + ("".join(kid) if open_children else "")

    def group_layer(self, g, occ=None):
        out, labs = [], []
        for r in g["regions"]:
            c = r["color"]
            for i, (x, y, w, h) in enumerate(r["rects"]):
                out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{c}" fill-opacity=".13" '
                           f'stroke="{c}" stroke-width="1.8" stroke-dasharray="6 4" stroke-opacity=".85"/>')
            x, y, w, h = r["rects"][0]
            cands = [[(x + 8, y + 15, r["label"], "start", False, 12, None)],
                     [(x + w - 8, y + 15, r["label"], "end", False, 12, None)],
                     [(x + 8, y + h - 6, r["label"], "start", False, 12, None)],
                     [(x + w - 8, y + h - 6, r["label"], "end", False, 12, None)],
                     [(x + 2, y - 4, r["label"], "start", False, 12, None)],
                     [(x + w - 2, y - 4, r["label"], "end", False, 12, None)],
                     [(x + 2, y + h + 13, r["label"], "start", False, 12, None)],
                     [(x + w - 2, y + h + 13, r["label"], "end", False, 12, None)]]
            if occ is None:
                labs.append(self._ptxt(*cands[0][0][:5], c, 12))
            else:
                labs.append(self.place(cands, occ, set(), c))
        return "".join(out + labs)

    def depth_of(self, a):
        A = self.anch.get(a)
        if A and A[3]:
            return self.blocks[A[3]]["_depth"]
        return self.blocks[a]["_depth"] if a in self.blocks else 0

    def root_of(self, a):
        bid = self.anch[a][3] if a in self.anch else a
        while self.blocks[bid]["_parent"]:
            bid = self.blocks[bid]["_parent"]
        return bid

    def flow_layer(self, flows, past=False, collapsed=False):
        out, tags = [], []
        if collapsed:   # 折りたたみ時: 子の経路点を落とし、内部だけの動作は親ブロックを強調
            fl2, act = [], {}
            for fl in flows:
                p2 = [a for a in fl["path"] if self.depth_of(a) == 0]
                if len(p2) >= 2:
                    fl2.append(dict(fl, path=p2))
                else:
                    r = self.root_of(fl["path"][0])
                    if fl.get("style") != "control" or r not in act:
                        act[r] = fl.get("color", "#e11d48")
            for bid, col in act.items():
                x, y, w, h = self.blocks[bid]["rect"]
                col = PAST if past else col
                out.append(f'<rect x="{f(x-4)}" y="{f(y-4)}" width="{f(w+8)}" height="{f(h+8)}" rx="7" '
                           f'fill="{col}" fill-opacity="{0.05 if past else 0.10}" stroke="{col}" '
                           f'stroke-width="{2 if past else 4}"/>')
            flows = fl2
        n = len(flows)
        for i, fl in enumerate(flows):
            lane = (i - (n - 1) / 2) * 9
            pts = self.polyline(self.nodes(fl["path"], lane))
            col = PAST if past else fl.get("color", "#e11d48")
            self.colors.add(col)
            ctrl = fl.get("style") == "control"
            sw = (2.2 if past else (2.6 if ctrl else 4.4)) * getattr(self, "flow_scale", 1)   # ストーリーボードでは太く
            dash = ' stroke-dasharray="7 5"' if ctrl else ""
            d = self.d(pts)
            if not past:
                out.append(f'<path d="{d}" fill="none" stroke="#fff" stroke-width="{sw+4}" '
                           f'stroke-linejoin="round" opacity=".9"/>')
                out.append(f'<circle cx="{f(pts[0][0])}" cy="{f(pts[0][1])}" r="{(4.5 if not ctrl else 3.5) * getattr(self, "flow_scale", 1)}" '
                           f'fill="{col}" stroke="#fff" stroke-width="1.5"/>')
            out.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="{sw}" stroke-linejoin="round" '
                       f'stroke-linecap="round"{dash} marker-end="url(#ah{col[1:]})" '
                       f'opacity="{0.6 if past else 0.95}"/>')
            if fl.get("label") and not past:
                segs = list(zip(pts, pts[1:]))
                tw = wlen(fl["label"]) * 6.4 + 12
                occ_ = getattr(self, "_lat_occ", None) or []
                cands_ = []   # 長い線の中点から順に。文字(注記)と重ならない所を選ぶ
                for (x1, y1), (x2, y2) in sorted(segs, key=lambda s: -(abs(s[0][0]-s[1][0]) + abs(s[0][1]-s[1][1]))):
                    L = abs(x1 - x2) + abs(y1 - y2)
                    for t_ in ((0.5, 0.3, 0.7, 0.2, 0.8) if L >= 60 else (0.5,)):
                        mx, my = x1 + (x2 - x1) * t_, y1 + (y2 - y1) * t_
                        if L < 60:   # 短い線は横にずらす
                            mx += (tw / 2 + 10) if abs(x1 - x2) < 1 else 0
                            my += -16 if abs(y1 - y2) < 1 else 0
                        cands_.append((mx, my))
                best_ = None
                for mx, my in cands_:
                    box = (mx - tw / 2, my - 9.5, mx + tw / 2, my + 9.5)
                    sc = sum(inter(box, o) for o, tag in occ_ if tag is None)
                    if best_ is None or sc < best_[0]:
                        best_ = (sc, mx, my)
                    if sc == 0:
                        break
                _, mx, my = best_
                occ_.append(((mx - tw / 2, my - 9.5, mx + tw / 2, my + 9.5), None))
                tags.append(f'<rect x="{f(mx-tw/2)}" y="{f(my-9.5)}" width="{f(tw)}" height="19" rx="9.5" '
                            f'fill="{col}" stroke="#fff" stroke-width="1.5"/>'
                            f'<text x="{f(mx)}" y="{f(my+4)}" font-size="11.5" font-weight="700" fill="#fff" '
                            f'text-anchor="middle">{esc(fl["label"])}</text>')
        for fl in flows:
            if fl.get("_lat") and not past:
                tags.append(self.lat_tags(fl))
        return "".join(out + tags)

    def lat_tags(self, fl):
        """レイテンシの数字: 要素(バス・ブリッジ・スレーブ)ごとに +N、CPU に戻った所に合計"""
        from latency import hop_text, total_text
        r, which = fl["_lat"]
        unit = self.s.get("latency", {}).get("unit", "cpu")
        col = fl.get("color", "#e11d48")
        occ = getattr(self, "_lat_occ", None)
        if occ is None:
            occ = self._lat_occ = []
        def A(k):
            if k in self.anch:
                return self.anch[k][:2]
            x_, y_, w_, h_ = self.blocks[k]["rect"]
            return (x_ + w_ / 2, y_ + h_ / 2)
        def feed(h):     # この要素へ入ってくる端子(要求の向き)
            return h[1] if h[0] in ("src", "dst") else h[3]
        def take(h):
            return h[1] if h[0] in ("src", "dst") else h[2]
        out = []
        def pill(x, y, t, prefer, big=False):
            fs = 11 if big else 9.6
            w, h = wlen(t) * fs * 0.56 + 12, fs + 6
            offs = [(-w / 2 - 9, 0), (0, -h - 3), (w / 2 + 9, 0), (0, h + 3), (-w / 2 - 9, -h - 3),
                    (w / 2 + 9, h + 3), (-w / 2 - 9, h + 3), (w / 2 + 9, -h - 3)]
            offs += [(sx * (w / 2 + 9), sy * k * (h + 3)) for k in (2, 3) for sx in (1, -1) for sy in (1, -1)]
            if prefer > 0:
                offs = offs[2:] + offs[:2]
            best, bs = None, None
            for dx, dy in offs:
                bx, by_ = x + dx - w / 2, y + dy - h / 2
                box = (bx, by_, bx + w, by_ + h)
                sc = sum(inter(box, o) for o, tag in occ if tag is None)
                if bs is None or sc < bs:
                    best, bs = box, sc
                if sc == 0:
                    break
            occ.append((best, None))
            x0, y0, x1, y1 = best
            if getattr(self, "_lat_rec", None) is not None:   # draw.io 出力用に記録
                self._lat_rec.append((x0, y0, x1, y1, t, col, fs))
            return (f'<rect x="{f(x0)}" y="{f(y0)}" width="{f(x1-x0)}" height="{f(y1-y0)}" rx="{f((y1-y0)/2)}" '
                    f'fill="#fff" stroke="{col}" stroke-width="1.6"/>'
                    f'<text x="{f((x0+x1)/2)}" y="{f(y0+(y1-y0)/2+fs*0.36)}" font-size="{fs}" font-weight="800" '
                    f'fill="{col}" text-anchor="middle">{esc(t)}</text>')
        prefer = -1 if which == "req" else 1
        path = r["path"]
        for i, e in enumerate(r["seq"]):
            t = hop_text(r, e, which, unit)
            if not t or e["kind"] == "src":
                continue
            if e["kind"] == "bus":
                (x1, _), (x2, _) = A(feed(path[i - 1])), A(take(path[i + 1]))
                bx, by, bw, bh = self.buses[e["id"]]["rect"]
                x, y = (x1 + x2) / 2, self.flow_cy(self.buses[e["id"]])
            elif e["kind"] == "bridge":
                (x1, y1), (x2, y2) = A(e["s"]), A(e["m"])
                x, y = (x1 + x2) / 2, (y1 + y2) / 2
            elif e["kind"] == "via":
                x, y = A(e["id"])
            else:
                x, y = A(e["key"])
                bx, by, bw, bh = self.blocks[e["id"]]["rect"]
                y = y + (14 if abs(y - by) < 2 else -14)
            out.append(pill(x, y, t, prefer))
        if which == "rsp":
            x, y = A(path[0][1])
            out.append(pill(x, y, total_text(r, unit), 1, big=True))
        return "".join(out)

    # ---------- 出力 ----------
    def svg(self, view=None, title=None, detail=False, groups=(), cur=None, cumulative=True,
            open_children=True, interactive=False, css_class="", perf=True, focus=None):
        hc = not open_children
        self.focus = focus
        vx, vy, vw, vh = view or (0, 0, *self.s["canvas"])
        head = ""
        if title:
            vy -= 36
            vh += 36
            head = (f'<rect x="{f(vx)}" y="{f(vy)}" width="{f(vw)}" height="36" fill="#fff"/>'
                    f'<text x="{f(vx+10)}" y="{f(vy+24)}" font-size="17" font-weight="800" '
                    f'fill="{INK}">{esc(title)}</text>')
        under, over = self.base_layer(detail, interactive, open_children)
        body = [under]
        for g in self.s.get("groups", []):
            if interactive:
                body.append(f'<g class="grp" data-grp="{g["id"]}" style="display:none">{self.group_layer(g, self.occupancy(under + over, detail))}</g>')
            elif g["id"] in groups:
                body.append(self.group_layer(g, self.occupancy(under + over, detail)))
        pf = ""
        if interactive:
            pf = self.perf_layer(detail, open_children, True, self.occupancy(under + over, detail))
            body.append(f'<g class="perf">{pf}</g>')
        elif perf:
            pf = self.perf_layer(detail, open_children, occ=self.occupancy(under + over, detail))
            body.append(pf)
        # レイテンシの数字は、構造の文字・注記を避けて置く
        _occ0 = self.occupancy(under + over + pf, detail) if (self.lat_results or cur or interactive) and self.steps else []
        if interactive:
            for hc in (0, 1):
                for i, st in enumerate(self.steps, 1):
                    self._lat_occ = list(_occ0)
                    body.append(f'<g class="ov past" data-h="{1-hc}" data-step="{i}" style="display:none">'
                                f'{self.flow_layer(st["flows"], True, bool(hc))}</g>')
            for hc in (0, 1):
                for i, st in enumerate(self.steps, 1):
                    self._lat_occ = list(_occ0)
                    body.append(f'<g class="ov cur" data-h="{1-hc}" data-step="{i}" style="display:none">'
                                f'{self.flow_layer(st["flows"], False, bool(hc))}</g>')
        elif cur:
            if cumulative:
                for st in self.steps[:cur - 1]:
                    body.append(self.flow_layer(st["flows"], True, hc))
            self._lat_occ = list(_occ0)
            body.append(self.flow_layer(self.steps[cur - 1]["flows"], False, hc))
        body.append(over)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" class="{css_class}" '
                f'viewBox="{f(vx)} {f(vy)} {f(vw)} {f(vh)}" font-family="{FONT}">'
                f'{self.marker_defs()}<rect x="{f(vx)}" y="{f(vy)}" width="{f(vw)}" height="{f(vh)}" fill="#fff"/>'
                f'{"".join(body)}{head}</svg>')

    def virtual_pairs(self, b):
        ps = {p["id"] for p in b.get("ports", [])}
        return [(f"{b['id']}.{u}", f"{b['id']}.{v}") for u, v in (("a_s", "b_m"), ("b_s", "a_m")) if u in ps and v in ps]

    def virtual_lines(self, b):
        o = ""
        for u, v in self.virtual_pairs(b):
            if u in self.anch and v in self.anch:
                bus_v = next((x for x in self.buses.values() if v in x.get("attach", [])), {})
                pw = self.port_width(bus_v, v) if bus_v else None
                col = "#b45309" if bus_v.get("width") and pw and pw != bus_v.get("width") else "#475569"
                (x1, y1), (x2, y2) = self.anch[u][:2], self.anch[v][:2]
                pts = [(x1, y1), (x2, y2)] if abs(x1 - x2) < 0.6 or abs(y1 - y2) < 0.6 else \
                      [(x1, y1), (x1, (y1 + y2) / 2), (x2, (y1 + y2) / 2), (x2, y2)]
                o += f'<path d="{self.d(pts)}" fill="none" stroke="{col}" stroke-width="2.4"/>'
        return o

    def bus_bar(self, b):
        """所属するバスグループの色を、ブロックの上辺の帯で示す(複数バスにまたがるブリッジは色を並べる)"""
        bs = self.block_buses.get(b["id"], [])
        if b.get("virtual") or not bs or b["_ext"] or not getattr(self, "buses", None) or len(self.buses) < 2:
            return ""
        x, y, w, h = b["rect"]
        o, n = "", len(bs)
        for k, bid in enumerate(bs):
            o += (f'<rect x="{f(x + 1 + (w - 2) * k / n)}" y="{f(y + 1)}" width="{f((w - 2) / n)}" height="5" '
                  f'fill="{self.bus_color[bid]}"/>')
        return o

    def cell_svg(self, b):
        """pack 表のセル: 枠を接して並べる。名前 + 先頭アドレスとサイズ"""
        x, y, w, h = b["rect"]
        fill = "#fff7ed" if b.get("kind") == "memory" else "#ffffff"
        st = "#c2410c" if b.get("kind") == "memory" else "#64748b"
        dash = ' stroke-dasharray="4 3"' if b.get("status") in ("tbd", "assumed") else ""
        o = (f'<rect x="{f(x)}" y="{f(y)}" width="{f(w)}" height="{f(h)}" fill="{fill}" stroke="{st}" '
             f'stroke-width="1"{dash}/>')
        lab = b.get("label", b["id"]) + (f" ×{b['count']}" if int(b.get("count", 1) or 1) > 1 else "")
        r = parse_addr(b.get("addr")) if b.get("addr") else None
        ty = y + h / 2 + (4 if not r else -2)
        o += (f'<text x="{f(x+w/2)}" y="{f(ty)}" font-size="11" font-weight="700" fill="{INK}" '
              f'text-anchor="middle">{esc(lab)}</text>')
        if r:
            o += (f'<text x="{f(x+w/2)}" y="{f(ty+12)}" font-size="8.6" fill="{GRAY}" text-anchor="middle" '
                  f'font-family="Consolas,Menlo,monospace">{hx(r[0])} {fmt_size(r[1] - r[0] + 1)}</text>')
        if b.get("status") in ("tbd", "assumed"):
            o += (f'<text x="{f(x+w-3)}" y="{f(y+10)}" font-size="8.5" font-weight="800" fill="#d97706" '
                  f'text-anchor="end">{"TBD" if b["status"] == "tbd" else "仮"}</text>')
        return o

    def fifo_block(self, b):
        x, y, w, h = b["rect"]
        n = int(b.get("cells", 4))
        along_x = b.get("flow", "x" if w >= h else "y") == "x"
        o = ""
        k_ = int(b.get("count", 1) or 1)
        for k in range(min(k_ - 1, 3), 0, -1):
            o += fifo_cells(x + 5 * k, y - 5 * k, w, h, n, along_x)
        o += fifo_cells(x, y, w, h, n, along_x, sw=1.8)
        lab = b.get("label", "FIFO")
        dep = f"{b['depth']}段" if b.get("depth") else ""
        style = self.s.get("style", {}).get("fifo_label", "over")
        fs = 13 if b["_depth"] else 14
        if style == "over":   # 記号の上に重ねて書く(白ふち)
            ty = y + h / 2 + (fs * 0.35) - (7 if dep else 0)
            o += (f'<text x="{f(x+w/2)}" y="{f(ty)}" font-size="{fs}" font-weight="800" fill="{FIFO_COL}" '
                  f'text-anchor="middle" paint-order="stroke" stroke="#fff" stroke-width="4">{esc(lab)}</text>')
            if dep:
                o += (f'<text x="{f(x+w/2)}" y="{f(ty+16)}" font-size="11.5" font-weight="700" fill="{FIFO_COL}" '
                      f'text-anchor="middle" paint-order="stroke" stroke="#fff" stroke-width="4">{esc(dep)}</text>')
        else:                 # 隣(上)に書く
            o += (f'<text x="{f(x)}" y="{f(y-6)}" font-size="{fs-1}" font-weight="800" fill="{FIFO_COL}">'
                  f'{esc(lab)}{(" " + dep) if dep else ""}</text>')
        if k_ > 1:
            o += (f'<text x="{f(x+w-4)}" y="{f(y+h-5)}" font-size="11" font-weight="800" fill="{INK}" '
                  f'text-anchor="end" paint-order="stroke" stroke="#fff" stroke-width="3">×{k_}</text>')
        return o

    def fifo_mark(self, x, y, vert, lab, occ=None, skip=()):
        """接続上のFIFO: 長辺を接した4つの長方形 + 隣(または上)に「FIFO 16段」。AFIFO は赤破線で囲む"""
        gw, gh = self.fifo_dims(vert)
        x0 = x - gw / 2   # 記号を接続線の上に置き、文字はその隣
        o = ""
        j = self._mjunc.get((round(x, 1), round(y, 1)))
        if j:   # 接続を意識: 接点 → FIFO の入口、出口 → 先へ短く続ける(宙に浮かせない)
            jx, jy = j
            if vert:
                d = 1 if y > jy else -1
                y_in, y_out = y - d * gh / 2, y + d * gh / 2
                o += (f'<line x1="{f(x)}" y1="{f(jy)}" x2="{f(x)}" y2="{f(y_in)}" stroke="#475569" stroke-width="2.4"/>'
                      f'<path d="M{f(x)},{f(y_out)} L{f(x)},{f(y_out+d*12)}" stroke="#475569" stroke-width="2.4" '
                      f'marker-end="url(#ahk)" fill="none"/>')
            else:
                d = 1 if x > jx else -1
                x_in, x_out = x - d * gw / 2, x + d * gw / 2
                o += (f'<line x1="{f(jx)}" y1="{f(y)}" x2="{f(x_in)}" y2="{f(y)}" stroke="#475569" stroke-width="2.4"/>'
                      f'<path d="M{f(x_out)},{f(y)} L{f(x_out+d*12)},{f(y)}" stroke="#475569" stroke-width="2.4" '
                      f'marker-end="url(#ahk)" fill="none"/>')
        o += fifo_cells(x0, y - gh / 2, gw, gh, 4, along_x=not vert)
        box = self.mark_box(x, y, lab, vert)
        if lab.startswith("AFIFO"):   # 非同期FIFO: FIFO記号の横に CDC 枠を並べる
            cx0 = x0 + gw + 3
            if j and not vert:   # 横の流れ: 線の続きと重ならないよう FIFO の上に並べる
                cx0 = x - self.CDC_W / 2
                y = y - gh / 2 - 9
            o += (f'<rect x="{f(cx0)}" y="{f(y-6.5)}" width="{f(self.CDC_W)}" height="13" rx="2" fill="#fff" '
                  f'stroke="#dc2626" stroke-width="1.4" stroke-dasharray="3 2"/>'
                  f'<text x="{f(cx0+self.CDC_W/2)}" y="{f(y+3.2)}" font-size="9" font-weight="800" fill="#dc2626" '
                  f'text-anchor="middle">CDC</text>')
            lab = lab[1:]   # 文字は「FIFO 16段」
        x1 = box[2]
        if self.s.get("style", {}).get("fifo_label") == "over":
            o += (f'<text x="{f(x0+gw/2)}" y="{f(y+3.2)}" font-size="9" font-weight="800" fill="{FIFO_COL}" '
                  f'text-anchor="middle" paint-order="stroke" stroke="#fff" stroke-width="3">{esc(lab)}</text>')
        elif occ is not None:   # 隣接: 右/左/上/下 のうち重ならない所
            cands = [[(x1 + 4, y + 3.2, lab, "start", False, 9, None)],
                     [(x0 - 4, y + 3.2, lab, "end", False, 9, None)],
                     [(x, y - gh / 2 - 5, lab, "middle", False, 9, None)],
                     [(x, y + gh / 2 + 11, lab, "middle", False, 9, None)],
                     [(x1 + 4, y - gh / 2 - 5, lab, "start", False, 9, None)],
                     [(x1 + 4, y + gh / 2 + 11, lab, "start", False, 9, None)],
                     [(x0 - 4, y - gh / 2 - 5, lab, "end", False, 9, None)],
                     [(x0 - 4, y + gh / 2 + 11, lab, "end", False, 9, None)],
                     [(x1 + 22, y + 3.2, lab, "start", False, 9, None)]]
            o += self.place(cands, occ, set(skip), FIFO_COL)
        else:
            o += (f'<text x="{f(x1+4)}" y="{f(y+3.2)}" font-size="9" font-weight="800" fill="{FIFO_COL}" '
                  f'paint-order="stroke" stroke="#fff" stroke-width="3">{esc(lab)}</text>')
        return o

    def attach_addr(self, bus, a):
        """そのポートのデコード範囲 [(lo,hi),...] / ["default"] / None"""
        opt = self.attach_opt.get((bus["id"], a), {})
        v = opt.get("addr") or ((self.anch.get(a) or (0, 0, 0, 0, {}))[4] or {}).get("addr")
        try:
            r = parse_ranges(v) if v else None
            return r or None
        except ValueError as e:
            self.warn.append(f"[アドレス] {a}: {e}")
            return None

    # ---------- 詳細ページ(階層ごとに独立した図) ----------
    def subspec(self, bid):
        """ブロック bid の中身だけの図YAMLを作る: 枠=bid、bid の端子=枠の端子(外側の相手を明記)"""
        import copy
        src = self.s.get("_orig") or self.s
        def find(lst):
            for b in lst:
                if b["id"] == bid:
                    return b
                r = find(b.get("children", []))
                if r:
                    return r
        B = copy.deepcopy(find(src.get("blocks", [])))
        def clean(b):
            for k in [k for k in b if k.startswith("_") or k == "rect"]:   # place/order は詳細ページの配置ヒントとして残す
                b.pop(k, None)
            for p in b.get("ports", []):
                p.pop("side", None); p.pop("at", None)
                for k in [k for k in p if k.startswith("_")]:
                    p.pop(k, None)
            for c in b.get("children", []):
                clean(c)
        for c in B.get("children", []):
            clean(c)
        inside = set()
        def collect(b):
            for c in b.get("children", []):
                inside.add(c["id"])
                collect(c)
        collect(B)
        # 枠の端子: bid の端子 + 外とつながる内部の端子
        pads, links, ext_pads = [], [], {}
        def outer_desc(key):
            for bus in self.buses.values():
                if key in bus.get("attach", []):
                    lab = self.bus_ports.get((bus["id"], key), (None, None))[1]
                    return f"{bus.get('label', bus['id'])}{(' ' + lab) if lab else ''}"
            for ln in self.s.get("links", []):
                for a, o in ((ln["from"], ln["to"]), (ln["to"], ln["from"])):
                    if a == key and o.split(".")[0] not in inside and o.split(".")[0] != bid:
                        ob = self.blocks.get(o.split(".")[0])
                        return ob.get("label", ob["id"]) if ob else o
            return None
        for p in B.get("ports", []):
            key = f"{bid}.{p['id']}"
            d_ = outer_desc(key)
            pads.append({"id": f"io_{p['id']}", "label": f"{p.get('name', p['id'])}" + (f" → {d_}" if d_ else ""),
                         "side": self.anch[key][2] if key in self.anch else "N", "external": "none"})
        for ln in src.get("links", []):
            a, b_ = ln["from"], ln["to"]
            ia, ib = a.split(".")[0] in inside, b_.split(".")[0] in inside
            na = a.startswith(bid + ".") and not ia
            nb_ = b_.startswith(bid + ".") and not ib
            l2 = {k: v for k, v in ln.items() if k != "level"}
            l2.setdefault("dir", self.link_dir(ln))   # 枠の端子(パッド扱い)になっても元の向きを保つ
            if ia and ib:
                links.append(l2)
            elif (ia and nb_) or (ib and na):
                pid = (b_ if nb_ else a).split(".", 1)[1]
                if nb_:
                    l2["to"] = f"io_{pid}"
                else:
                    l2["from"] = f"io_{pid}"
                links.append(l2)
            elif ia or ib:   # 内部と bid の外の直接結線 → 枠に端子を作る
                o = b_ if ia else a
                ob = self.blocks.get(o.split(".")[0])
                pid = f"x_{o.replace('.', '_')}"
                if pid not in ext_pads:
                    ext_pads[pid] = {"id": pid, "label": f"→ {ob.get('label', ob['id']) if ob else o}", "side": "N",
                                     "external": "none"}
                if ia:
                    l2["to"] = pid
                else:
                    l2["from"] = pid
                links.append(l2)
        sub = {"chip": {"label": B.get("label", bid) + (f" ×{B['count']}" if int(B.get("count", 1) or 1) > 1 else "")},
               "clocks": src.get("clocks", {}), "pads": pads + list(ext_pads.values()),
               "blocks": B.get("children", []), "links": links}
        return sub

    def detail_pages(self):
        """[(ファイル名, タイトル, svg)] 子を持つ全ブロック(深さ不問)。自動配置の図のみ"""
        out = []
        if not self.auto:
            return out
        for b in self.blocks.values():
            if b.get("children") and not b["_ext"] and not b.get("pack"):
                try:
                    sc = Chip(self.subspec(b["id"]))
                except Exception as e:   # 詳細ページが作れなくてもトップは出す
                    self.warn.append(f"詳細ページ {b['id']} を作れない: {e}")
                    continue
                for w in sc.warn:
                    if w.startswith("[") and not w.startswith("[perf]"):
                        self.warn.append(f"(詳細 {b['id']}) {w}")
                title = f"{b.get('label', b['id'])} 内部"
                self._sub_chips = getattr(self, "_sub_chips", {})
                self._sub_chips[b["id"]] = sc
                out.append((f"20_detail_{b['id']}.svg", title, sc.svg(title=title)))
        return out

    def detail_view(self, bid, margin=40, top_extra=95):
        x, y, w, h = self.blocks[bid]["rect"]
        return (x - margin, y - top_extra, w + 2 * margin, h + margin + top_extra)


TEXT_RX = re.compile(r'<text ([^>]*)>([^<]*)</text>')
RECT_RX = re.compile(r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"')


def text_box(x, y, fs, txt, anc="start", rot=False):
    """文字の占有矩形 (x0, y0, x1, y1) の概算"""
    w = wlen(txt) * fs * 0.56
    if rot:   # rotate(-90): 下から上へ
        y0 = {"start": y - w, "middle": y - w / 2, "end": y}[anc]
        return (x - fs * 0.8, y0, x + fs * 0.2, y0 + w)
    x0 = {"start": x, "middle": x - w / 2, "end": x - w}[anc]
    return (x0, y - fs * 0.8, x0 + w, y + fs * 0.2)


def inter(a, b):
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return ix * iy if ix > 0 and iy > 0 else 0


def text_overlaps(svg, limit=12):
    """描いたSVGの文字同士の重なりを推定(フォント幅は概算)"""
    import html as _h
    boxes = []
    vb = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    vx, vy, vw, vh = map(float, vb.groups()) if vb else (-1e9, -1e9, 2e9, 2e9)
    for m in TEXT_RX.finditer(svg):
        at, txt = m.group(1), _h.unescape(m.group(2))
        g = lambda k, d=None: (re.search(k + r'="([^"]*)"', at) or [None, d])[1]
        try:
            x, y, fs = float(g("x")), float(g("y")), float(g("font-size", "12"))
        except (TypeError, ValueError):
            continue
        if fs >= 17 or not (vx <= x <= vx + vw and vy <= y <= vy + vh):   # タイトルと表示範囲外は対象外
            continue
        w = wlen(txt) * fs * 0.56
        anc = g("text-anchor", "start")
        x0 = {"start": x, "middle": x - w / 2, "end": x - w}[anc]
        if "rotate(-90" in at:
            y0 = {"start": y - w, "middle": y - w / 2, "end": y}[anc]
            boxes.append((x - fs * 0.8, y0, x + fs * 0.2, y0 + w, txt))
        else:
            boxes.append((x0, y - fs * 0.8, x0 + w, y + fs * 0.2, txt))
    out = []
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            ix = min(a[2], b[2]) - max(a[0], b[0])
            iy = min(a[3], b[3]) - max(a[1], b[1])
            if ix > 1.5 and iy > 1.5:
                small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
                if ix * iy / small > 0.15:
                    out.append(f"「{a[4]}」と「{b[4]}」")
                    if len(out) >= limit:
                        return out
    return out


def fifo_cells(x, y, w, h, n=4, along_x=True, col=FIFO_COL, fill=FIFO_FILL, sw=1.4):
    """(x,y,w,h) の範囲に、長辺を接した長方形を n 個並べる。along_x=True: 横に並ぶ(縦長のセル)"""
    out = []
    for k in range(n):
        if along_x:
            cx, cy, cw, ch = x + w * k / n, y, w / n, h
        else:
            cx, cy, cw, ch = x, y + h * k / n, w, h / n
        out.append(f'<rect x="{f(cx)}" y="{f(cy)}" width="{f(cw)}" height="{f(ch)}" fill="{fill}" '
                   f'stroke="{col}" stroke-width="{sw}"/>')
    return "".join(out)


def to_png(svg_path):
    png = svg_path[:-4] + ".png"
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", "2", svg_path, "-o", png], check=True)
        return png
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=png, scale=2)
        return png
    except Exception:
        return None


def load(path):
    try:
        return yaml.safe_load(open(path, encoding="utf-8"))
    except yaml.YAMLError as e:
        sys.exit(f"YAML構文エラー: {e}\nヒント: ?, :, #, [ を含む値は \"...\" で囲む")


def export_all(spec_path, outdir, png=False):
    spec = load(spec_path)
    os.makedirs(outdir, exist_ok=True)
    c = Chip(spec)
    files = {}
    files["00_structure.svg"] = c.svg(title="構造（トップ）")
    if any(b.get("children") for b in spec.get("blocks", [])):
        files["00_structure_collapsed.svg"] = c.svg(title="構造（トップ・階層を折りたたみ）", open_children=False)
    for g in spec.get("groups", []):
        files[f"01_group_{g['id']}.svg"] = c.svg(title=g["label"], groups=[g["id"]])
    for i, st in enumerate(c.steps, 1):
        files[f"1{i:02d}_step{i}.svg" if len(c.steps) > 9 else f"1{i}_step{i}.svg"] = c.svg(
            title=f"{i}. {st['title']}", cur=i, cumulative=st.get("cumulative", True))
    for name, title, svg in c.detail_pages():
        files[name] = svg
    if c.lat_results:   # レイテンシの内訳表
        from latency import table_md as lat_md
        open(os.path.join(outdir, "latency.md"), "w", encoding="utf-8").write("# レイテンシ目安\n\n" + lat_md(c.lat_results))
    try:   # メモリマップ(アドレス範囲があれば)
        from memmap import pages as mm_pages, table_md
        mps, rs = mm_pages(c)
        for name, title, svg in mps:
            files[name] = svg
        if rs:
            md = table_md(rs)
            if c.buses:
                from reach import reach_table
                md += "\n\n## マスタごとの到達性(○ 全部 / △ 一部 / × 届かない)\n\n" + reach_table(c) + "\n"
            open(os.path.join(outdir, "memmap.md"), "w", encoding="utf-8").write(md)
    except Exception as e:
        c.warn.append(f"[アドレス] メモリマップを作れない: {e}")
    for b in ([] if c.auto else c.blocks.values()):   # 座標指定の図: 同じ座標系のズームで詳細図
        if b.get("children") and not b["_ext"]:
            v = c.detail_view(b["id"])
            files[f"20_detail_{b['id']}.svg"] = c.svg(view=v, title=f"{b['label']} 内部（詳細）", detail=True, focus=b["id"])
            if b["_depth"] == 0:
                for i, st in enumerate(c.steps, 1):
                    if any(c.depth_of(a) > 0 and c.root_of(a) == b["id"] for fl in st["flows"] for a in fl["path"]):
                        files[f"2{i}_detail_{b['id']}_step{i}.svg"] = c.svg(
                            view=v, title=f"{b['label']} 内部 × {i}. {st['title']}", detail=True, cur=i,
                            cumulative=False, focus=b["id"])
    # 規模の検査: 1枚に載るブロック数
    top_n = sum(1 for b in c.blocks.values() if not b["_ext"] and b["_depth"] <= 1 and not b.get("virtual")
                and not (b["_parent"] and c.blocks[b["_parent"]].get("pack")))
    # バスを分けた図はバスごとにまとまって読めるので上限を少し上げる(1 バス 30、バスが増えるごとに +10、上限 60)
    lim = min(60, 30 + 10 * max(0, len(c.buses) - 1))
    if top_n > lim:
        c.warn.append(f"[規模] トップ図に {top_n} ブロック(目安 {lim}) → サブシステムにまとめて(children)トップを粗くする")
    for bid_, bus_ in c.buses.items():
        n_ = len({a.split(".")[0] for a in bus_.get("attach", [])})
        if n_ > 24:
            c.warn.append(f"[規模] {bus_.get('label', bid_)} に {n_} ブロック → バスを分けるか、周辺を pack でまとめる")
    for b in c.blocks.values():
        if len(b.get("children", [])) > 16 and not b.get("pack"):
            c.warn.append(f"[規模] {b['id']} の子が {len(b['children'])} 個 → 中間の階層を作るか count でまとめる")
    pngs = []
    for name, s in files.items():
        ov = text_overlaps(s)
        if ov:
            c.warn.append(f"[重なり] {name}: " + " / ".join(ov[:6]) + (" ..." if len(ov) > 6 else ""))
        path = os.path.join(outdir, name)
        open(path, "w", encoding="utf-8").write(s)
        if png:
            pngs.append(to_png(path))
    if png and not any(pngs):
        c.warn.append("PNG変換ツール(rsvg-convert/cairosvg)がないためPNGは未出力")
    return c, files


def main():
    ap = argparse.ArgumentParser(description="チップ仕様書向けブロック図レンダラ")
    ap.add_argument("cmd", choices=["render", "lint", "anchors", "freeze"])
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    if a.cmd == "render":
        c, files = export_all(a.spec, a.out, a.png)
        print("生成:", ", ".join(files), "→", a.out)
    else:
        spec = load(a.spec)
        c = Chip(spec)
        if a.cmd == "anchors":
            for k, v in c.anch.items():
                p = v[4] or {}
                tag = p.get("role") or p.get("dir") or ""
                print(f"{k:28s} {v[2]} {tag}")
            for b in c.buses:
                print(f"{b:28s} (bus)")
        elif a.cmd == "freeze":
            out = strip_private(spec)
            yaml.safe_dump(out, open(a.out, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False,
                           default_flow_style=None, width=120)
            print("座標を固定:", a.out)
    print("自動配置: あり" if c.auto else "自動配置: なし(座標指定)")
    print("lint:", "OK" if not c.warn else "\n  " + "\n  ".join(c.warn))


if __name__ == "__main__":
    main()
