"""自動配置: 座標(rect/at)のない図YAMLに座標を与える。

配置ヒント
  block.place : N|S|W|E   (N=バスの上の行, S=バスの下の行, W/E=左右の列)
  block.order : 行内の並び順(小さい順)
  pad.side    : N|S|W|E   (チップのどの辺から入るか)
ヒントがない場合:
  - パッドに接続したブロック → パッドの辺の行/列
  - バスにMだけで接続 → 上の行 / それ以外 → 下の行
バスは1本(先頭)を水平に置く。2本目以降はブロックとして扱わず警告する。
"""
import math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GAP = 34          # ブロック間(詰める)
STUB = 125        # 既定のブロックとバスの距離(注記の長さから autolayout で詰め直す)
CGAP_X, CGAP_Y = 24, 28   # 子ブロックの間隔
PAD_X, PAD_TOP, PAD_BOT = 14, 32, 14   # 親の内側の余白(上は親ラベル分)
SHADOW = 5        # count>1 の重ね表示のずれ
MAX_DEPTH = 1     # この図で配置する階層の深さ(それより深い中身は各ブロックの詳細ページで別に配置)
ROW_MAX = 6       # 1行の最大ブロック数(バスなし時)
EXT_W, EXT_H = 110, 64
OPP = {"N": "S", "S": "N", "W": "E", "E": "W"}


def wlen(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s or ""))


def needs_layout(spec):
    return any("rect" not in b for b in spec.get("blocks", []))


class _G:
    """接続関係の索引"""
    def __init__(self, spec):
        self.pads = {p["id"]: p for p in spec.get("pads", [])}
        self.ext = {e["id"]: e for e in spec.get("external", [])}
        buses = spec.get("buses", [])
        self.buses = buses
        self.bus = buses[0] if buses else None
        self.bus_of = {}   # "blk.port" -> bus id
        for b in buses:
            for a in b.get("attach", []):
                self.bus_of[a["port"] if isinstance(a, dict) else a] = b["id"]
        self.attach = set(self.bus_of)
        self.nb = {}
        self.to_ends = {ln["to"] for ln in spec.get("links", [])}
        self.mark_hosts = set()   # 中に CDC/FIFO 記号を置くブロック → 寸法に余裕を取る
        for ln in spec.get("links", []):
            if ln.get("fifo") or ln.get("cdc"):
                self.mark_hosts.add((ln["from"] if ln.get("cdc_in") == "from" else ln["to"]).split(".")[0])
        for b in buses:
            for a in b.get("attach", []):
                if isinstance(a, dict) and a.get("cdc_in") == "block" and (a.get("fifo") or a.get("cdc")):
                    self.mark_hosts.add(a["port"].split(".")[0])
        for ln in spec.get("links", []):
            self.nb.setdefault(ln["from"], []).append(ln["to"])
            self.nb.setdefault(ln["to"], []).append(ln["from"])

    def others(self, key):
        return self.nb.get(key, [])


def _is_mp(b, g):
    """複数のバスにスレーブとしてだけつながる(マスタ端子なし)= 複数ポートのスレーブ"""
    ks = [f"{b['id']}.{p['id']}" for p in b.get("ports", []) if f"{b['id']}.{p['id']}" in g.bus_of]
    if any(p.get("role") == "M" for p in b.get("ports", []) if f"{b['id']}.{p['id']}" in g.bus_of):
        return False
    return len({g.bus_of[k] for k in ks}) >= 2 and not b.get("_ext")


def _ports(b):
    return b.setdefault("ports", [])


def _fifo_side(c, key, g):
    """FIFOブロックの端子: 入ってくる接続=流れの入口(W)、出ていく接続=出口(E)"""
    if c.get("kind") != "fifo":
        return None
    flow = c.get("flow", "x")
    return ("W" if key in g.to_ends else "E") if flow == "x" else ("N" if key in g.to_ends else "S")


def _assign_child_sides(b, g):
    """子ブロックのポートの辺を決める(親ポートへ→その辺、兄弟へ→相対位置)"""
    kids = {c["id"]: c for c in b.get("children", [])}
    pside = {f"{b['id']}.{p['id']}": p.get("side", "N") for p in _ports(b)}
    for c in kids.values():
        for p in _ports(c):
            if p.get("side"):
                continue
            key = f"{c['id']}.{p['id']}"
            side = "N"
            if _fifo_side(c, key, g):
                p["side"] = _fifo_side(c, key, g)
                continue
            for o in g.others(key):
                if o in pside:
                    side = pside[o]
                    break
                ob = o.split(".")[0]
                rel = _rel_side(c, ob)
                if rel:
                    side = rel
                    break
            p["side"] = side


_BY = {}   # id -> ブロック(子も含む)。autolayout で作る


def _chain(bid):
    out = []
    while bid in _BY:
        out.append(bid)
        bid = _BY[bid].get("_par")
    return out


def _rel_side(c, ob):
    """c から見た相手 ob の向き。兄弟でなくても、共通の親の中で並ぶ位置(ずれ)から決める"""
    ca, oa = _chain(c["id"]), _chain(ob)
    if not oa:
        return None
    for i, x in enumerate(ca):        # c 側の祖先 x と、相手側の祖先 y が兄弟になる所
        for y in oa:
            if x != y and _BY[x].get("_par") is not None and _BY[x].get("_par") == _BY[y].get("_par") \
                    and "_grid" in _BY[x] and "_grid" in _BY[y]:
                r1, c1 = _BY[x]["_grid"]
                r2, c2 = _BY[y]["_grid"]
                return ("S" if r2 > r1 else "N") if r1 != r2 else ("E" if c2 > c1 else "W")
    x, y = ca[-1], oa[-1]                # 最上位どうし: 行/列の並びから
    if x != y and x in _TOP_ORDER and y in _TOP_ORDER:
        return _top_side(_TOP_ORDER[x], _TOP_ORDER[y])
    return None


_TOP_ORDER = {}


def _top_side(a, b):
    (s1, i1), (s2, i2) = a, b
    if s1 == s2 and isinstance(s1, int):
        return "E" if i2 > i1 else "W"
    if s1 == s2:
        return "S" if i2 > i1 else "N"
    if isinstance(s1, int) and isinstance(s2, int):
        return "S" if s2 > s1 else "N"
    if s1 in ("W", "E"):
        return "E" if s1 == "W" else "W"
    return "W" if s2 == "W" else "E"


def _ypos(b, y, hmax, align_bottom):
    """行の中の y: 上に積む物があれば下へ、下に積む物があれば上へ寄せる"""
    sh = b.get("_stack_h", 0)
    if align_bottom:
        return y + hmax - b["_h"] - (0 if b.get("_stack_up", True) else sh)
    return y + _ex(b) + (sh if b.get("_stack_up") else 0)


def _ex(b):
    """count>1 の重ね表示が右上に張り出す量"""
    return SHADOW * min(int(b.get("count", 1)) - 1, 3) if int(b.get("count", 1)) > 1 else 0


def _order_kids(b, g):
    """子の並び: 親の端子につながるものを先頭行へ、兄弟で結線のあるものを隣へ(順序指定 order があれば優先)"""
    kids = b.get("children", [])
    if any("order" in c for c in kids):
        kids.sort(key=lambda c: c.get("order", 0))
        return
    ids = {c["id"] for c in kids}
    pk = {f"{b['id']}.{p['id']}" for p in _ports(b)}
    def nbrs(c):
        out = []
        for p in _ports(c):
            for o in g.others(f"{c['id']}.{p['id']}"):
                ob = o.split(".")[0]
                if ob in ids and ob != c["id"] and ob not in out:
                    out.append(ob)
        return out
    def to_parent(c):
        return any(o in pk for p in _ports(c) for o in g.others(f"{c['id']}.{p['id']}"))
    byid = {c["id"]: c for c in kids}
    order, seen = [], set()
    starts = [c["id"] for c in kids if to_parent(c)] + [c["id"] for c in kids]
    for st in starts:
        if st in seen:
            continue
        stack = [st]
        while stack:   # 深さ優先で連結成分をつなげて並べる
            cid = stack.pop(0)
            if cid in seen:
                continue
            seen.add(cid)
            order.append(cid)
            stack = [n for n in nbrs(byid[cid]) if n not in seen] + stack
    b["children"] = [byid[i] for i in order]


def _cell_size(c):
    from addrmap import parse_addr
    a = 0
    if c.get("addr"):
        a = 9 * 5.2 + 8   # 「3086_0000」+ サイズ
    return max(wlen(c.get("label")) * 7.2 + 14, a + 44, 70), 38


def _size_pack(b):
    """pack: 子(周辺マクロ)を枠を接して並べた表にする"""
    kids = b.get("children", [])
    n = len(kids)
    cols = int(b.get("cols") or min(n, max(3, math.ceil(n / 3))))
    cw = max(_cell_size(c)[0] for c in kids)
    ch = max(_cell_size(c)[1] for c in kids)
    rows = math.ceil(n / cols)
    for i, c in enumerate(kids):
        c["_grid"] = (i // cols, i % cols)
        c["_w"], c["_h"] = round(cw), round(ch)
        c["_cell"] = True
    b["_cw"], b["_rh"], b["_gx"], b["_gy"] = [cw] * cols, [ch] * rows, 0, 0
    return cols * cw, rows * ch


def _size(b, g):
    if b.get("virtual"):          # バスどうしの直接接続(箱は描かない): 端子2組ぶんの幅だけ
        b["_w"], b["_h"] = 96, 4        # 端子の間隔 32px(バス側の番号 M12/S3 が並んで読める)
        return
    if b.get("pack") and b.get("children") and b["_depth"] < MAX_DEPTH:
        iw, ih = _size_pack(b)
        cnt = sum(1 for p in _ports(b) if p.get("side") in ("N", "S"))
        b["_w"] = round(max(iw + 2 * PAD_X, wlen(b.get("label")) * 8.4 + 40, (cnt + 1) * 30))
        b["_padtop"] = PAD_TOP + (22 if b["id"] in g.mark_hosts else 0)   # 中の CDC/FIFO 記号の分を上に空ける
        b["_h"] = round(ih + b["_padtop"] + PAD_BOT)
        return
    if b.get("children") and b["_depth"] < MAX_DEPTH:
        _order_kids(b, g)
    kids = b.get("children", []) if b["_depth"] < MAX_DEPTH else []
    if kids:
        n = len(kids)
        cols = int(b.get("cols") or (n if n <= 3 else math.ceil(math.sqrt(n))))   # cols: 子の列数(配置ヒント)
        for i, c in enumerate(kids):
            c["_grid"] = (i // cols, i % cols)
            c["_par"] = b["id"]
        rows = math.ceil(n / cols)
        # 外(親の外)とつながる子が複数あり、その相手が左右にあるとき: その辺の側に縦に並べる
        if not b.get("cols"):
            inside = {c["id"] for c in kids}
            def out_side(c):
                for p in _ports(c):
                    for o in g.others(f"{c['id']}.{p['id']}"):
                        ob = o.split(".")[0]
                        if ob in inside or ob == b["id"] or b["id"] in _chain(ob):
                            continue
                        return _rel_side(b, ob)
                return None
            sides = {c["id"]: out_side(c) for c in kids}
            fw = [c for c in kids if sides[c["id"]] == "W"]
            fe = [c for c in kids if sides[c["id"]] == "E"]
            if len(fw) >= 2 or len(fe) >= 2:
                mid = [c for c in kids if c not in fw and c not in fe]
                rows = max(len(fw), len(fe), 1)
                mcols = math.ceil(len(mid) / rows) if mid else 0
                k = 0
                for i, c in enumerate(fw):
                    c["_grid"] = (i, 0)
                for i, c in enumerate(mid):
                    c["_grid"] = (i % rows, (1 if fw else 0) + i // rows)
                for i, c in enumerate(fe):
                    c["_grid"] = (i, (1 if fw else 0) + mcols)
                cols = (1 if fw else 0) + mcols + (1 if fe else 0)
        _assign_child_sides(b, g)
        for c in kids:
            _size(c, g)
        cw = [max((c["_w"] + _ex(c) for c in kids if c["_grid"][1] == j), default=0) for j in range(cols)]
        rh = [max((c["_h"] + _ex(c) for c in kids if c["_grid"][0] == i), default=0) for i in range(rows)]
        b["_cw"], b["_rh"] = cw, rh
        # 向かい合う左右の端子名が入る間隔(詳細図で端子名を出すため)
        nm = max([len(str(p.get("name", ""))) for c in kids for p in _ports(c) if p.get("side") in ("E", "W")] + [0])
        b["_gx"] = max(CGAP_X, nm * 6.2 * 2 + 18)
        inner_w = sum(cw) + b["_gx"] * (cols - 1)
        inner_h = sum(rh) + CGAP_Y * (rows - 1)
    cnt = {s: 0 for s in "NSWE"}
    for p in _ports(b):
        cnt[p.get("side", "N")] += 1
    lab_w = wlen(b.get("label")) * (7.6 if b["_depth"] else 8.4)
    sub_w = wlen(b.get("sub")) * 6.4
    icon = 26 if b.get("kind") == "memory" else 0          # メモリ記号(左のワード線)の分
    # 横幅は中身が入る最小に(R45): 名前・補足・下辺のクロックと ×N・バス側の端子番号・端子の間隔
    lab_w = wlen(b.get("label")) * (8.2 if b["_depth"] else 9.0)
    n_ = int(b.get("count", 1) or 1)
    foot = (wlen(str(b.get("clock") or "")) * 6.6 + 14 if b.get("clock") else 0) + (22 if n_ > 1 else 0)
    nb_ = [p for p in _ports(b) if f"{b['id']}.{p['id']}" in g.bus_of and p.get("side") in ("N", "S", None)]
    plab = max([40 + (26 if n_ > 1 else 0)] if nb_ else [0])          # バス側の番号(PSEL12-14 等)が隣と離れる幅
    nps = max(cnt["N"], cnt["S"])
    w = max(64 if b["_depth"] else 72, lab_w + 22 + icon, sub_w + 18 + icon, foot + 10 + icon,
            plab + 8 if nps <= 1 else 0, (nps + 1) * 30 if nps > 1 else 0)
    if b.get("_att_of"):                                  # コアの中の I$/D$: 文字が入る最小の大きさ
        w = max(64, lab_w + 20, sub_w + 16, (max(cnt["N"], cnt["S"]) + 1) * 24)
    padp = {s_: sum(1 for p in _ports(b) if p.get("side") == s_ and any(o in g.pads for o in g.others(f"{b['id']}.{p['id']}")))
            for s_ in "WE"}
    h = max(46 if b["_depth"] else 60, (max(cnt["W"], cnt["E"]) + 1) * 22,
            (max(padp["W"], padp["E"]) + 1) * 52 if max(padp["W"], padp["E"]) else 0)
    if b.get("sub") and b.get("clock"):
        h = max(h, 66)
    if b["id"] in g.mark_hosts and not b.get("_mp"):   # 記号(FIFO等)と名前・段数を、ラベルと重ねずに置ける余裕
        w, h = w + 56, max(h, 82)
    if kids:
        w = max(w, inner_w + 2 * PAD_X, wlen(b.get("label")) * 8.4 + 40)
        h = max(h, inner_h + PAD_TOP + PAD_BOT)
    b["_w"], b["_h"] = round(w), round(h)


def _place_children(b):
    kids = b.get("children", []) if b["_depth"] < MAX_DEPTH else []
    if not kids:
        return
    x0, y0 = b["rect"][0] + PAD_X, b["rect"][1] + b.get("_padtop", PAD_TOP)
    for c in kids:
        r, k = c["_grid"]
        x = x0 + sum(b["_cw"][:k]) + b.get("_gx", CGAP_X) * k
        y = y0 + sum(b["_rh"][:r]) + b.get("_gy", CGAP_Y) * r + _ex(c)
        c["rect"] = [round(x), round(y), c["_w"], c["_h"]]
        _place_children(c)


def _perf_chars(spec, g):
    """バス接続の注記(幅 @クロック)の最大文字数 → スタブ長を決める"""
    doms = {d["id"]: d for d in spec.get("clocks", {}).get("domains", [])}
    blocks = {b["id"]: b for b in spec.get("blocks", [])}
    busd = {b["id"]: b for b in g.buses}
    m = 0
    for a, bid_ in g.bus_of.items():
        bus = busd[bid_]
        bid, _, pid = a.partition(".")
        b = blocks.get(bid, {})
        p = next((q for q in b.get("ports", []) if q["id"] == pid), {})
        w = p.get("width") or bus.get("width")
        ck = p.get("clock") or doms.get(p.get("clock_domain"), {}).get("freq") or b.get("clock") or \
            doms.get(b.get("clock_domain"), {}).get("freq")
        l1 = (f"{w}b " if w else "") + (f"@{ck}" if ck else "")
        m = max(m, len(l1), 8 if (w and ck) else 0)
    return m


def _distribute_ports(b, fixed=None):
    """辺ごとにポートを等間隔に並べる。fixed: {port_id: 絶対座標}"""
    fixed = fixed or {}
    for s in "NSWE":
        ps = [p for p in _ports(b) if p.get("side") == s and "at" not in p and p["id"] not in fixed and p.get("_conn", True)]
        for p in _ports(b):   # 結線のない端子は描かれないので位置を占有させない
            if not p.get("_conn", True) and "at" not in p:
                p["at"] = 0.5
        k = len(ps)
        for i, p in enumerate(ps):
            p["at"] = round((i + 1) / (k + 1), 3)
    for pid, v in fixed.items():
        for p in _ports(b):
            if p["id"] == pid:
                p["at"] = round(v, 1)
    for c in (b.get("children", []) if b["_depth"] < MAX_DEPTH else []):
        _distribute_ports(c)


def _port_abs(b, p):
    x, y, w, h = b["rect"]
    t = p["at"]
    if p["side"] in ("N", "S"):
        return (x + w * t if t <= 1 else t, y if p["side"] == "N" else y + h)
    return (x if p["side"] == "W" else x + w, y + h * t if t <= 1 else t)


def autolayout(spec, warn=None):
    warn = warn if warn is not None else []
    g = _G(spec)
    blocks = spec.get("blocks", [])
    _BY.clear()
    def _reg(b):
        _BY[b["id"]] = b
        for c in b.get("children", []):
            c["_par"] = b["id"]
            _reg(c)
    for b in blocks:
        _reg(b)
    for b in blocks:
        b["_depth"] = 0
        def mark(x, d):
            for c in x.get("children", []):
                c["_depth"] = d
                mark(c, d + 1)
        mark(b, 1)
    # --- 0. バスの段(階層): 主バスから、両方に接続するブロック(ブリッジ)を辿る ---
    bus_ids = [b["id"] for b in g.buses]
    lvl = {}
    if bus_ids:
        main = next((b["id"] for b in g.buses if b.get("level") == 0), bus_ids[0])
        lvl[main] = 0
        q = [main]
        def bset(b):
            return {g.bus_of[f"{b['id']}.{p['id']}"] for p in _ports(b) if f"{b['id']}.{p['id']}" in g.bus_of}
        while q:
            cur = q.pop(0)
            for b in blocks:
                if _is_mp(b, g):      # 複数ポートのスレーブはバスの段を決めない(ブリッジではない)
                    continue
                bs = bset(b)
                if cur in bs:
                    for o in bs:
                        if o not in lvl:
                            lvl[o] = lvl[cur] + 1
                            q.append(o)
        for b_ in g.buses:
            if "level" in b_:
                lvl[b_["id"]] = b_["level"]
        for i_, bid_ in enumerate(bus_ids):
            lvl.setdefault(bid_, max(lvl.values(), default=-1) + 1)
        # 複数ポートのスレーブ(DRAM コントローラ等)につながるバスが同じ段にあれば、バスを上から1本ずつ積む
        #   → 右端の縦長の枠から各バスへ横に接続できる
        mps_ = [b for b in blocks if _is_mp(b, g)]
        if any(len({lvl[x] for x in bset(b)}) < len(bset(b)) for b in mps_) and not any("level" in b_ for b_ in g.buses):
            order_ = sorted(bus_ids, key=lambda x: (lvl[x], bus_ids.index(x)))
            lvl = {x: i_ for i_, x in enumerate(order_)}
    nb = (max(lvl.values()) + 1) if lvl else 0
    def ports_bus(b):
        return [(p, g.bus_of[f"{b['id']}.{p['id']}"]) for p in _ports(b) if f"{b['id']}.{p['id']}" in g.bus_of]

    # --- 1. 行/列: 行 r は「バス r とバス r+1 の間」(r=-1 は最上段) ---
    for b in blocks:
        if b.get("_att_of"):              # 親の枠に引っ付ける枠(I$/D$ 等): 親と一緒に置く
            b["_row"], b["_r"] = "A", None
            continue
        side = b.get("place")
        if _is_mp(b, g) and side not in ("N", "S"):
            side = side or "E"            # 複数バスから入るスレーブ(DRAM コントローラ等): 右端に縦長の枠
            b["_mp"] = True
        if not side:
            for p in _ports(b):
                for o in g.others(f"{b['id']}.{p['id']}"):
                    if o in g.pads and g.pads[o].get("side") in ("W", "E"):
                        side = g.pads[o]["side"]
                        break
                if side:
                    break
        pb = ports_bus(b)
        ls = sorted({lvl[x] for _, x in pb})
        if b.get("virtual") and len(ls) >= 2 and ls[-1] - ls[0] > 1:
            b["_row"], b["_r"] = "X", None   # 離れた段のバスの直接接続: 間を避けて縦にそのまま(後で置く)
            up = min(pb, key=lambda t: lvl[t[1]])[1]
            for p, x in pb:
                p["side"] = "N" if x == up else "S"
            continue
        if len(ls) >= 2 and ls[-1] - ls[0] > 1 and not b.get("_mp") and side not in ("N", "S"):
            side = side or "W"           # 離れた段のバスをつなぐブリッジ: 左端に縦長の枠(間のバスを横切らない)
            b["_mp"] = True
        if side in ("W", "E"):
            b["_row"], b["_r"] = side, None
            b["_cbus"] = min(ls) if ls else 0
            continue
        b["_row"] = "R"
        if not ls:
            r = 0 if nb else 0
            for p in _ports(b):   # パッドだけにつながる(下辺/上辺)
                for o in g.others(f"{b['id']}.{p['id']}"):
                    if o in g.pads and g.pads[o].get("side") == "N":
                        r = -1
            b["_r"] = r if nb else 0
        elif len(ls) >= 2:                      # ブリッジ: 上のバスの下の行
            b["_r"] = ls[0]
        else:
            k = ls[0]
            roles = [p.get("role") for p, _ in pb]
            has_m = "M" in roles     # マスタ(設定用のスレーブ端子があっても)はバスの上、スレーブだけのものは下
            if side == "N" or (side != "S" and has_m):
                b["_r"] = k - 1                 # バス k の上
            else:
                b["_r"] = k                     # バス k の下
        if not nb:
            b["_r"] = 0
    # メモリ: バス/パッドに直接つながらず特定ブロックとだけ結線 → そのブロックの行・隣
    bid = {b["id"]: b for b in blocks}
    partner = {}
    for b in blocks:
        if b.get("kind") != "memory" or b.get("place") or b.get("_att_of"):
            continue
        keys = [f"{b['id']}.{p['id']}" for p in _ports(b)]
        if any(k in g.attach for k in keys):
            continue
        for k in keys:
            for o in g.others(k):
                ob = o.split(".")[0]
                if ob in bid and ob != b["id"]:
                    partner[b["id"]] = ob
                    break
    for m, ob in partner.items():
        if bid[ob]["_row"] == "R":
            bid[m]["_row"], bid[m]["_r"] = "R", bid[ob]["_r"]
    def pad_side_of(b):
        for p in _ports(b):
            for o in g.others(f"{b['id']}.{p['id']}"):
                if o in g.pads:
                    return g.pads[o].get("side")
        return None
    for b in blocks:   # 並び順の既定: 下辺/上辺のパッドへ出るものは端、ブリッジは右端(下の段をその下へ)
        if "order" not in b and b["_row"] == "R":
            nbus = len({x for _, x in ports_bus(b)})
            ps_ = pad_side_of(b)
            b["_ord"] = {"W": -2, "S": -1, "N": -1, "E": 2}.get(ps_, 1 if nbus >= 2 else 0)
    # バスに出ていないブロックの鎖(例 CPU コア[I$ D$] → L2$ → L3$ → バス)を、バスに出るブロック(L3$)の上に積む
    #   (バスが下なら上へ、上なら下へ)。各段は1つ上の段とだけ結線でつながるもの
    byid_ = {b["id"]: b for b in blocks}
    atts = {}
    for b in blocks:
        if b.get("_att_of"):
            atts.setdefault(b["_att_of"], []).append(b)
    def owner_(k):
        bid = k.split(".")[0]
        return byid_[bid].get("_att_of", bid) if bid in byid_ else None
    def keys_(blk):
        return [f"{x['id']}.{p['id']}" for x in [blk] + atts.get(blk["id"], []) for p in _ports(x)]
    def linked(blk):
        out = {}
        for k in keys_(blk):
            for o in g.others(k):
                ob = owner_(o)
                if ob and ob != blk["id"]:
                    out.setdefault(ob, []).append((k, o))
        return out
    used = set()
    for A in blocks:
        if A.get("_row") != "R" or not ports_bus(A) or A.get("_att_of") or A["id"] in used:
            continue
        chain, cur, seen_ = [], A, {A["id"]}
        while True:
            cand = [ob for ob in linked(cur) if ob not in seen_ and ob not in used and ob in byid_
                    and not ports_bus(byid_[ob]) and byid_[ob].get("_row") == "R" and not byid_[ob].get("_att_of")]
            if len(cand) != 1:
                break
            nb_ = byid_[cand[0]]
            chain.append(nb_)
            seen_.add(nb_["id"])
            cur = nb_
        if not chain:
            continue
        up = any(lvl[x] == A["_r"] + 1 for _, x in ports_bus(A))   # バスが下 → 上に積む
        A["_stack"], A["_stack_up"] = [x["id"] for x in chain], up
        used |= {x["id"] for x in chain} | {A["id"]}
        lv = [A] + chain
        for i, x in enumerate(lv):
            x["_row"] = "R" if i == 0 else "S"
            ups = {lv[i + 1]["id"]} if i + 1 < len(lv) else set()
            dns = {lv[i - 1]["id"]} if i > 0 else set()
            for k in keys_(x):
                bid_, pid_ = k.split(".")
                p = next(q for q in _ports(byid_[bid_]) if q["id"] == pid_)
                oo = {owner_(o) for o in g.others(k)}
                if oo & ups:
                    p.setdefault("side", "N" if up else "S")
                elif oo & dns:
                    p.setdefault("side", "S" if up else "N")
    rkeys = sorted({b["_r"] for b in blocks if b["_row"] == "R"})
    rows = {}
    for rk in rkeys:
        bidx = {b_["id"]: i_ for i_, b_ in enumerate(g.buses)}
        def grp(b):   # 下のバス(この行の下の段)ごとにまとめる。上のバスだけにつながるものは先頭
            below = [x for _, x in ports_bus(b) if lvl[x] == rk + 1]
            return bidx[below[0]] + 1 if below else 0
        for b in blocks:
            if b["_row"] == "R" and b["_r"] == rk:
                b["_grp"] = grp(b)
        base = sorted([b for b in blocks if b["_row"] == "R" and b["_r"] == rk and b["id"] not in partner],
                      key=lambda b: (b["_grp"], b.get("order", b.get("_ord", 0))))
        out_ = []
        for b in base:
            out_.append(b)
            out_ += [bid[m] for m, ob in partner.items() if ob == b["id"] and bid[m]["_r"] == rk]
        out_ += [bid[m] for m, ob in partner.items() if bid[m]["_row"] == "R" and bid[m]["_r"] == rk and bid[m] not in out_]
        rows[rk] = out_
    cols = {s_: sorted([b for b in blocks if b["_row"] == s_], key=lambda b: b.get("order", 0)) for s_ in "WE"}
    order = {}
    for rk, bl in rows.items():
        for i_, b in enumerate(bl):
            order[b["id"]] = (rk, i_)
    for s_, bl in cols.items():
        for i_, b in enumerate(bl):
            order[b["id"]] = (s_, i_)

    _TOP_ORDER.clear()
    _TOP_ORDER.update(order)
    # --- 2. ポートの辺 ---
    for b in blocks:
        for p in _ports(b):
            if p.get("side"):
                continue
            key = f"{b['id']}.{p['id']}"
            side = None
            if _fifo_side(b, key, g) and key not in g.bus_of:
                p["side"] = _fifo_side(b, key, g)
                continue
            if key in g.bus_of:
                L = lvl[g.bus_of[key]]
                if b["_row"] in ("W", "E"):
                    side = "E" if b["_row"] == "W" else "W"
                else:
                    side = "N" if L <= b["_r"] else "S"
            else:
                for o in g.others(key):
                    if o in g.pads:
                        side = g.pads[o].get("side") or ("W" if b["_row"] != "E" else "E")
                        g.pads[o].setdefault("side", side)
                        break
                    ob = o.split(".")[0]
                    if ob not in order and _chain(ob):
                        ob = _chain(ob)[-1]          # 子の端子 → その最上位ブロックの位置で
                    if ob in order and ob != b["id"]:
                        (s1, i1), (s2, i2) = order[b["id"]], order[ob]
                        if s1 == s2 and isinstance(s1, int):
                            side = "E" if i2 > i1 else "W"
                        elif s1 == s2:
                            side = "S" if i2 > i1 else "N"
                        elif isinstance(s1, int) and isinstance(s2, int):
                            side = "S" if s2 > s1 else "N"
                        elif s1 in ("W", "E"):
                            side = "E" if s1 == "W" else "W"
                        else:
                            side = "W" if s2 == "W" else "E"
                        break
            p["side"] = side or "N"
    for b in blocks:
        _size(b, g)
    VG = 84     # 積んだ段どうしの間: 結線の幅・周波数が書ける長さ
    for b in blocks:
        if b.get("_att_of"):
            b["_h"] = 40
    for b in blocks:                      # I$/D$ はコアの枠の中(下側)に並べる
        at = atts.get(b["id"], [])
        if at:
            iw = sum(a["_w"] for a in at) + 8 * (len(at) - 1)
            b["_w"] = max(b["_w"], iw + 20)
            b["_h"] = max(b["_h"], 44 + max(a["_h"] for a in at) + 8)   # 上に名前・×N・クロック
    for A in blocks:
        if not A.get("_stack"):
            continue
        lv = [byid_[x] for x in A["_stack"]]
        W = max([A["_w"]] + [x["_w"] for x in lv])     # 段の幅をそろえ、結線を縦にまっすぐ
        for x in [A] + lv:
            if x.get("kind") == "memory" or x is A or not atts.get(x["id"]):
                x["_w"] = W
        A["_stack_h"] = sum(x["_h"] + _ex(x) + VG for x in lv)
        A["_stack_w"] = max(x["_w"] + _ex(x) for x in lv)

    # --- 3. 外部要素の寸法と余白 ---
    ext_side = {}
    for e in g.ext.values():
        e.setdefault("ports", [])
        side = None
        for p in e["ports"]:
            for o in g.others(f"{e['id']}.{p['id']}"):
                if o in g.pads:
                    side = g.pads[o].get("side", "W")
        ext_side[e["id"]] = side or "W"
        e["_w"] = 170 if e.get("kind") == "memory" else EXT_W
        e["_h"] = EXT_H
    def ext_extent(s, dim):
        v = [e[dim] for e in g.ext.values() if ext_side[e["id"]] == s]
        return max(v) + 45 if v else 0
    padN = any(p.get("side") == "N" for p in g.pads.values())
    cx = 20 + ext_extent("W", "_w") + (25 if any(p.get("side") == "W" for p in g.pads.values()) else 0)
    cy = 20 + ext_extent("N", "_h") + (28 if padN else 0)

    # --- 4. 配置: 段ごとに [行] [バス] [行] [バス] ... ---
    # 共有バス(APB/AHB)にだけぶら下がる周辺は、枠を隣と接して並べる(図を小さく。R40)
    shared_ids = {b_["id"] for b_ in g.buses if b_.get("type") == "shared"
                  or any(k in str(b_.get("label", "")).upper() + str(b_.get("protocol", "")).upper() for k in ("APB", "AHB"))}
    def abut_bus(b):
        """同じバスにだけつながるブロックは、行の中で枠を接して並べる(図を小さく。R40/R44)。
        共有バス(APB/AHB)は既定で接する。クロスバー/NoC も style.abut(既定 true)で接する。
        横に出る結線(パッド等)があるブロックは間を空ける"""
        st_ = spec.get("style", {})
        bs = {x for _, x in ports_bus(b)}
        if len(bs) != 1:
            return None
        x = next(iter(bs))
        bus_ = next((b_ for b_ in g.buses if b_["id"] == x), {})
        if bus_.get("abut") is False:
            return None
        if x in shared_ids:
            if st_.get("abut_shared", True) is False or any(p.get("role") == "M" for p, _ in ports_bus(b)):
                return None
        elif st_.get("abut", True) is False:
            return None
        if any(p.get("side") in ("W", "E") for p in _ports(b) if f"{b['id']}.{p['id']}" not in g.bus_of):
            return None
        return x
    def gap(a, b):
        ka, kb = abut_bus(a), abut_bus(b)
        return 0 if ka and ka == kb else GAP
    def row_w(bl):
        return sum(b["_w"] + _ex(b) for b in bl) + sum(gap(a, b) for a, b in zip(bl, bl[1:]))
    pc = _perf_chars(spec, g)
    stub = max(56, pc * 6.2 + 34) if pc else 56
    side_gap = max(64, pc * 5.8 + 26) if pc else 64
    side_gap_pre = max(64, _perf_chars(spec, g) * 5.8 + 26) if _perf_chars(spec, g) else 64
    _mpw = [b["_w"] for b in cols["W"] if b.get("_mp")]
    _rw = [b["_w"] for b in cols["W"] if not b.get("_mp")]
    left_w = (max(_mpw) + (side_gap_pre + 40 + max(_rw) if _rw else 0)) if _mpw else max(_rw, default=0)
    _fifo_w = any(k.split(".")[0] in {b["id"] for b in cols["W"]} for bb_ in g.buses for k in bb_.get("_fifo_ports", []))
    # 左の列から入る接続に FIFO があれば、段数の文字のぶん行の先頭を右へ
    x_in = cx + 30 + (left_w + side_gap + 70 + (90 if _fifo_w else 0) if cols["W"] else 0)
    fixed = {}
    y = cy + 38 + (15 if padN else 0)
    bus_by_lvl = {}
    for b_ in g.buses:
        bus_by_lvl.setdefault(lvl[b_["id"]], []).append(b_)
    def place_row(bl, y, align_bottom, x_start=None):
        xx = x_in if x_start is None else x_start
        hmax = max([b["_h"] + _ex(b) + b.get("_stack_h", 0) for b in bl] + [0])
        for i_, b in enumerate(bl):
            yy = _ypos(b, y, hmax, align_bottom)
            b["rect"] = [round(xx), round(yy), b["_w"], b["_h"]]
            xx += b["_w"] + _ex(b) + (gap(b, bl[i_ + 1]) if i_ + 1 < len(bl) else GAP)
        return y + hmax
    inner = max([row_w(bl) for bl in rows.values()] + [320])

    # 下の段にバスが並ぶとき: 上の行のグループ(そのバスへのブリッジ・マスタ)と下の行を同じ列幅(セグメント)にそろえる
    seg_x = {}
    def lower_of(L, key):
        return [b for b in rows.get(L, []) if any(x == key for _, x in ports_bus(b) if lvl[x] == L)]
    # 離れた段のバスを直接つなぐ線を通す道(左端): その間の行は右へずらして空ける
    span_rows = set()
    for b in blocks:
        if b.get("_row") == "X":
            ls_ = sorted({lvl[x] for _, x in ports_bus(b)})
            span_rows |= set(range(ls_[0], ls_[-1]))
    CH = 120
    def rx(rk):
        return x_in + (CH if rk in span_rows else 0)
    def place_row_seg(rk, y, align_bottom):
        bl = rows[rk]
        below = bus_by_lvl.get(rk + 1, [])
        if rk < 0 or not below:
            return place_row(bl, y, align_bottom, rx(rk))
        hmax = max([b["_h"] + _ex(b) + b.get("_stack_h", 0) for b in bl] + [0])
        bidx = [b_["id"] for b_ in g.buses]
        cur_u, cur_l = rx(rk), rx(rk + 1)   # 上の行 / 下の行 のカーソル(下の行は空いている左側から詰める)
        for gk in sorted({b["_grp"] for b in bl}):
            part = [b for b in bl if b["_grp"] == gk]
            key = bidx[gk - 1] if gk else None
            low = lower_of(rk + 1, key) if key else []
            pu, pl = row_w(part), row_w(low)
            if key and pl:
                # 下のまとまりは、上のグループ(ブリッジ・マスタ)の右端にそろえて左の空きへ広げる
                #  → 次のバスのまとまりをすぐ右に置ける(横幅を詰める)
                ux = max(cur_u, cur_l + max(0, pl - pu))
                lx = max(cur_l, ux + pu - pl)
                seg_x[key] = (lx, pl)
                cur_l = lx + pl + GAP * 2 + 70           # バス帯どうしが触れない間隔
            else:
                ux = cur_u
            xx = ux
            for i_, b in enumerate(part):
                yy = _ypos(b, y, hmax, align_bottom)
                b["rect"] = [round(xx), round(yy), b["_w"], b["_h"]]
                xx += b["_w"] + _ex(b) + (gap(b, part[i_ + 1]) if i_ + 1 < len(part) else GAP)
            cur_u = ux + pu + (GAP * 2 if key else GAP)
        return y + hmax

    if nb:
        if -1 in rows:
            y = place_row_seg(-1, y, True)
        else:
            y -= stub - 20
        for L in range(nb):
            y += stub
            for b_ in bus_by_lvl.get(L, []):   # 同じ段のバスは横に並べる(稀)
                shared = b_.get("type") == "shared" or any(k in str(b_.get("label", "")).upper() for k in ("APB", "AHB"))
                b_["_shared"] = shared
                k_lr = max([sum(1 for p, x in ports_bus(b) if x == b_["id"]) for b in cols["W"] + cols["E"]] + [0])
                has_addr = b_.get("_has_addr") or any(isinstance(a, dict) and a.get("addr") for a in b_.get("attach", []))
                flows_ = bool(spec.get("scenario", {}).get("steps") or spec.get("latency", {}).get("accesses"))
                b_["_h"] = max((76 if has_addr else (60 if flows_ else 40)) if shared else (112 if has_addr else 64),
                               16 * k_lr + 14)   # 動作の矢印を通すときは共有バスも文字と矢印が並ぶ高さに
                b_["_y"] = y
            hb = max(b_["_h"] for b_ in bus_by_lvl.get(L, [{"_h": 64}]))
            y += hb
            if L in rows:
                y += stub
                if L >= 1:   # 下の段: バスごとのまとまりを、そのバスにつながる上の行(ブリッジ・マスタ)の下へ
                    segs = {}
                    for b in rows[L]:
                        mine = [x for _, x in ports_bus(b) if lvl[x] == L]
                        segs.setdefault(mine[0] if mine else None, []).append(b)
                    cursor, ybot = rx(L), y
                    for key in sorted(segs, key=lambda k: -1 if k is None else [b_["id"] for b_ in g.buses].index(k)):
                        bl = segs[key]
                        anchors = [b for b in blocks if b["_row"] == "R" and "rect" in b and b["_r"] == L - 1
                                   and any(x == key for _, x in ports_bus(b))]
                        xs = cursor
                        if key in seg_x:
                            sx, sw = seg_x[key]
                            xs = max(cursor, sx + (sw - row_w(bl)) / 2)
                        elif anchors:   # 上のブリッジの右端にそろえ、左の空きへ広げる(横幅を詰める)
                            ar_ = max(b["rect"][0] + b["_w"] for b in anchors)
                            xs = max(cursor, ar_ - row_w(bl))
                        yb = place_row(bl, y, False, xs)
                        ybot = max(ybot, yb)
                        cursor = xs + row_w(bl) + GAP * 2
                    y = ybot
                else:
                    y = place_row_seg(L, y, False)
        inner = max([inner] + [b["rect"][0] + b["_w"] + _ex(b) - x_in for b in blocks if b["_row"] == "R" and "rect" in b])
        # バスの横幅: 接続するブロックの範囲(左右の列につながるなら端まで)
        for b_ in g.buses:
            xs = []
            for b in blocks:
                for p, x in ports_bus(b):
                    if x == b_["id"] and b["_row"] == "R":
                        xs += [b["rect"][0] + b["_w"] * 0.5 - 40, b["rect"][0] + b["_w"] * 0.5 + 40,
                               b["rect"][0], b["rect"][0] + b["_w"]]
            if any(x == b_["id"] for b in cols["W"] for _, x in ports_bus(b)) or lvl[b_["id"]] == 0 \
                    or any(o.get("_row") == "X" and any(x == b_["id"] for _, x in ports_bus(o)) for o in blocks):
                xs.append(x_in)
            if any(x == b_["id"] for b in cols["E"] for _, x in ports_bus(b)) or lvl[b_["id"]] == 0:
                xs.append(x_in + inner)
            x0 = min(xs + [x_in]) if lvl[b_["id"]] == 0 else min(xs or [x_in])
            x1 = max(xs or [x_in + 320])
            if any(x == b_["id"] for b in cols["W"] for _, x in ports_bus(b)):
                x0 -= 70   # 左から入る接続の CDC/FIFO 記号(と段数の文字)を置く余白
            if any(x == b_["id"] for b in cols["E"] for _, x in ports_bus(b)):
                x1 += 70
            x1 = max(x1, x0 + 240)
            b_["rect"] = [round(x0 - 15), round(b_["_y"]), round(x1 - x0 + 30), b_["_h"]]
        # 離れた段のバスどうしの直接接続: 間のバス・ブロックを避けた x で縦にまっすぐ
        for b in blocks:
            if b.get("_row") != "X":
                continue
            bs_ = sorted({x for _, x in ports_bus(b)}, key=lambda x: lvl[x])
            A, B = next(x for x in g.buses if x["id"] == bs_[0]), next(x for x in g.buses if x["id"] == bs_[-1])
            ya, yb = A["rect"][1] + A["rect"][3], B["rect"][1]
            obs = [(x["rect"][0], x["rect"][0] + x["rect"][2]) for x in g.buses
                   if lvl[x["id"]] not in (lvl[A["id"]], lvl[B["id"]]) and ya < x["rect"][1] < yb]
            obs += [(o["rect"][0] - 20, o["rect"][0] + o["rect"][2] + 20) for o in blocks
                    if o is not b and "rect" in o and o.get("_row") == "R"
                    and o["rect"][1] < yb and o["rect"][1] + o["_h"] > ya]
            lo = max(A["rect"][0], B["rect"][0]) + 40
            hi = min(A["rect"][0] + A["rect"][2], B["rect"][0] + B["rect"][2]) - 40
            xc = None
            for x in range(int(lo), int(hi) + 1, 6):
                if all(x + 40 < o0 or x - 40 > o1 for o0, o1 in obs):
                    xc = x
                    break
            if xc is None:
                xc = lo
                warn.append(f"[貫通] {b['label']}: 間のバス/ブロックを避ける場所がない → order/place で空ける")
            top, bot = ya + stub, yb - stub
            b["rect"] = [round(xc - 30), round(top), 60, round(max(4, bot - top))]
            b["_h"] = b["rect"][3]
            # a_s と b_m(A→B)、b_s と a_m(B→A)を同じ x に
            col = {"a_s": xc - 16, "b_m": xc - 16, "b_s": xc + 16, "a_m": xc + 16}
            for p in _ports(b):
                p["at"] = col.get(p["id"], xc)
        # 左右の列: つながるバスの高さに中心をそろえる
        for s_, x0 in (("W", cx + 30), ("E", x_in + inner + side_gap + 70)):
            yy = None
            mps = [b for b in cols[s_] if b.get("_mp")]
            rest = [b for b in cols[s_] if not b.get("_mp")]
            # 複数ポートのスレーブ: つながるバスの上端〜下端の高さの縦長の枠。端子は各バスの高さに横向き
            #   (同じ列の他のブロックより外側に置き、横の接続がそれらを横切らないように)
            xm = x0 + (max((b["_w"] for b in rest), default=0) + side_gap + 40 if rest else 0)
            if s_ == "W" and mps:
                xm = x0
                x0 = x0 + max(b["_w"] for b in mps) + side_gap + 40 if rest else x0
            for b in mps:
                bl_ = [x for x in g.buses if any(y == x["id"] for _, y in ports_bus(b))]
                top = min(x["rect"][1] for x in bl_) - 10
                bot = max(x["rect"][1] + x["rect"][3] for x in bl_) + 10
                b["_h"] = max(b["_h"], round(bot - top))
                b["rect"] = [round(xm), round(top), b["_w"], b["_h"]]
                fx = {}
                for x in bl_:
                    att = [p for p, y in ports_bus(b) if y == x["id"]]
                    bcy = x["rect"][1] + x["rect"][3] / 2
                    for j_, p in enumerate(att):
                        fx[p["id"]] = bcy + (j_ - (len(att) - 1) / 2) * 16
                fixed[b["id"]] = fx
                xm += b["_w"] + 40
            for b in sorted(rest, key=lambda b: -len(ports_bus(b))):
                bb = next((x for x in g.buses if lvl[x["id"]] == b["_cbus"]), g.buses[0])
                bcy = bb["rect"][1] + bb["rect"][3] / 2
                if yy is None:
                    yy = bcy - b["_h"] / 2
                b["rect"] = [round(x0), round(yy), b["_w"], b["_h"]]
                att = [p for p, x in ports_bus(b) if x == bb["id"]]
                fixed[b["id"]] = {p["id"]: bcy + (j_ - (len(att) - 1) / 2) * 16 for j_, p in enumerate(att)} \
                    if att and abs(yy - (bcy - b["_h"] / 2)) < 1 else {}
                yy += b["_h"] + 40
        # 積んだ段(L2$・コア)を置き、コアの中に I$/D$。結線の列(x)を上の段から下へ受け渡す
        for A in blocks:
            if not A.get("_stack") or "rect" not in A:
                continue
            lv = [A] + [byid_[x] for x in A["_stack"]]
            up = A.get("_stack_up")
            ax, ay, aw_, ah_ = A["rect"]
            acx = ax + aw_ / 2
            yy_ = ay
            for x in lv[1:]:
                if up:
                    yy_ = yy_ - VG - x["_h"]
                    x["rect"] = [round(acx - x["_w"] / 2), round(yy_), x["_w"], x["_h"]]
                else:
                    prev = lv[lv.index(x) - 1]
                    yy_ = prev["rect"][1] + prev["_h"] + _ex(prev) + VG
                    x["rect"] = [round(acx - x["_w"] / 2), round(yy_), x["_w"], x["_h"]]
            for x in lv:
                at = atts.get(x["id"], [])
                if at:
                    X, Y, Wd, Ht = x["rect"]
                    tw = sum(a["_w"] for a in at) + 8 * (len(at) - 1)
                    xx = X + Wd / 2 - tw / 2
                    for a_ in at:
                        ty = Y + Ht - a_["_h"] - 8 if up else Y + 8
                        a_["rect"] = [round(xx), round(ty), a_["_w"], a_["_h"]]
                        xx += a_["_w"] + 8
            # 列の受け渡し: 一番上(コアの I$/D$)の端子の x を基準に、下の段の端子を真下へ
            pos_ = {}
            for x in reversed(lv):
                for k in keys_(x):
                    bid_, pid_ = k.split(".")
                    p = next(q for q in _ports(byid_[bid_]) if q["id"] == pid_)
                    if byid_[bid_].get("_att_of") and p.get("side") in ("N", "S"):
                        r_ = byid_[bid_]["rect"]
                        pos_[k] = r_[0] + r_[2] / 2
                i = lv.index(x)
                nxt = lv[i - 1] if i > 0 else None
                if nxt is None:
                    continue
                # 上の段の端子 → 下の段の、つながる端子を同じ x に
                fx_n = {}
                for k in keys_(x):
                    if k not in pos_:
                        continue
                    for o in g.others(k):
                        if owner_(o) == nxt["id"]:
                            fx_n[o] = pos_[k]
                # 下の段: 入ってくる端子の列を、出ていく端子にも順に引き継ぐ
                ins = sorted(fx_n.values())
                outs = []
                for k in keys_(nxt):
                    bid_, pid_ = k.split(".")
                    p = next(q for q in _ports(byid_[bid_]) if q["id"] == pid_)
                    if k not in fx_n and p.get("side") == ("S" if up else "N") and k.split(".")[0] == nxt["id"]:
                        outs.append(k)
                if outs and len(outs) == len(ins):
                    for k, xv in zip(outs, ins):
                        fx_n[k] = xv
                elif len(outs) == 1 and ins:
                    fx_n[outs[0]] = sum(ins) / len(ins)
                for k, xv in fx_n.items():
                    pos_[k] = xv
                    bid_, pid_ = k.split(".")
                    if bid_ == nxt["id"]:
                        fixed.setdefault(bid_, {})[pid_] = xv
        right = max([b_["rect"][0] + b_["rect"][2] for b_ in g.buses] + [b["rect"][0] + b["_w"] for b in blocks])

    else:
        main = rows.get(0, [])
        y = cy + 60 + (15 if padN else 0)
        first_row_cy = None
        for r in range(0, len(main), ROW_MAX):
            chunk = main[r:r + ROW_MAX]
            xx = x_in
            for b in chunk:
                b["rect"] = [round(xx), round(y), b["_w"], b["_h"]]
                xx += b["_w"] + GAP + 30
            hmax = max(b["_h"] for b in chunk)
            if first_row_cy is None:
                first_row_cy = y + hmax / 2
            y += hmax + 90
        first_row_cy = first_row_cy or cy + 120
        inner = max([b["rect"][0] + b["_w"] for b in main] + [x_in + 300]) - x_in
        for s_, x0 in (("W", cx + 45), ("E", x_in + inner + 75)):
            yy = first_row_cy - (cols[s_][0]["_h"] / 2 if cols[s_] else 0)
            for b in cols[s_]:
                b["rect"] = [round(x0), round(yy), b["_w"], b["_h"]]
                yy += b["_h"] + 40
        right = max([b["rect"][0] + b["_w"] for b in blocks] + [x_in + 300])
    bottom = max(b["rect"][1] + b["_h"] for b in blocks) if blocks else cy + 300
    chip_w = right + 30 - cx
    chip_h = bottom + 30 - cy
    spec.setdefault("chip", {})["rect"] = [round(cx), round(cy), round(chip_w), round(chip_h)]
    conn = set(g.attach) | set(g.nb)
    def mark_conn(b):
        for p in _ports(b):
            p["_conn"] = f"{b['id']}.{p['id']}" in conn
        for c in b.get("children", []):
            mark_conn(c)
    for b in blocks:
        mark_conn(b)
    for b in blocks:
        _place_children(b)
        _distribute_ports(b, fixed.get(b["id"]))

    # --- 5. パッド ---
    bmap = {}
    def reg(b):
        bmap[b["id"]] = b
        for c in b.get("children", []):
            reg(c)
    for b in blocks:
        reg(b)
    x0, y0, x1, y1 = cx, cy, cx + chip_w, cy + chip_h
    free = {s: [] for s in "NSWE"}
    for p in g.pads.values():
        p.setdefault("side", "W")
        if "at" in p:
            continue
        pos = None
        for o in g.others(p["id"]):
            bid, _, pid = o.partition(".")
            if bid in bmap:
                port = next((q for q in bmap[bid].get("ports", []) if q["id"] == pid), None)
                if port:
                    pos = _port_abs(bmap[bid], port)
                    break
        if pos is None:
            free[p["side"]].append(p)
            continue
        px, py = pos
        p["at"] = {"W": [x0, py], "E": [x1, py], "N": [px, y0], "S": [px, y1]}[p["side"]]
    for s, ps in free.items():
        for i, p in enumerate(ps):
            t = (i + 1) / (len(ps) + 1)
            p["at"] = {"W": [x0, y0 + chip_h * t], "E": [x1, y0 + chip_h * t],
                       "N": [x0 + chip_w * t, y0], "S": [x0 + chip_w * t, y1]}[s]
    for p in g.pads.values():
        p["at"] = [round(v, 1) for v in p["at"]]

    # --- 6. チップ外要素 ---
    for e in g.ext.values():
        if "rect" in e:
            continue
        s = ext_side[e["id"]]
        pad = None
        for p in e["ports"]:
            for o in g.others(f"{e['id']}.{p['id']}"):
                if o in g.pads:
                    pad = g.pads[o]
            p.setdefault("side", OPP[s])
            p.setdefault("at", 0.5)
        ax, ay = pad["at"] if pad else ((x0 + x1) / 2, y1)
        w, h = e["_w"], e["_h"]
        e["rect"] = {"W": [x0 - 45 - w, ay - h / 2], "E": [x1 + 45, ay - h / 2],
                     "N": [ax - w / 2, y0 - 45 - h], "S": [ax - w / 2, y1 + 45]}[s] + [w, h]
        e["rect"] = [round(v) for v in e["rect"]]
    rights = [e["rect"][0] + e["rect"][2] for e in g.ext.values()] + [x1]
    bottoms = [e["rect"][1] + e["rect"][3] for e in g.ext.values()] + [y1]
    spec["canvas"] = [round(max(rights) + 20), round(max(bottoms) + 20)]
    return warn


def strip_private(o):
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o
