#!/usr/bin/env python3
"""gatediag — ゲートレベル図(論理ゲート・FF・ラッチ・MUX・クロックゲートの回路図)

  python3 gatediag.py render gate.yaml -o out/ [--png]     # out/<名前>.svg
  python3 gatediag.py lint   gate.yaml
  python3 gatediag.py netlist top.v [-m モジュール] -o gate.yaml   # 構造記述(ゲートのインスタンス)から YAML を作る

gate.yaml:
  title: リセット同期化回路
  ports:
    - {id: clk, dir: in}
    - {id: rst_n_in, dir: in}
    - {id: rst_n_out, dir: out}
  nets: {data: 8}                       # バス幅(省略時 1)
  cells:
    - {id: u_and, type: and, in: [a, b], out: y}           # and/or/nand/nor/xor/xnor/not/buf(入力は 2 本以上も可)
    - {id: u_mux, type: mux, in: [a, b], s: sel, out: y}   # in[0] = S が 0 の時
    - {id: ff1, type: dff, d: "1", ck: clk, rn: rst_n_in, q: s1}   # dff: d ck rn sn q qn、edge: neg(立ち下がり)
                                        # cdc_ok: "理由"(わかって別クロックを論理越しに受ける所。info に残す)
    - {id: lat, type: latch, d: x, g: en, q: y}
    - {id: icg, type: icg, ck: clk, en: en, te: test_en, out: gclk}  # クロックゲート
  groups:                               # 説明用の囲み(任意)
    - {label: 同期化 2 段, cells: [ff1, ff2]}
  "0" / "1" はタイ(定数)。cell に label(表示名)・note(注記)を書ける。

配置: 左から右へ信号の流れ順に列を作り(長い接続は中継点を入れる)、列の間の通路に縦の幹線を引く。戻りの接続は上を回る。
クロックの網は色分け(元の端子ごと)、リセットの網は茶色。
チェック(lint): [未接続] [複数駆動] [ループ](FF を通らない組み合わせのループ)[クロック](FF のクロックを論理で作っている:
  ICG を使う)[CDC](別のクロックの FF から同期化なしで受けている / 論理を通してから同期化している)
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import esc, wlen, INK, to_png
import yaml

GRAY = "#64748b"
CLK_PAL = ["#2563eb", "#9333ea", "#0891b2", "#db2777", "#16a34a", "#ca8a04"]
RST_C = "#92400e"
GATES = {"and", "or", "nand", "nor", "xor", "xnor", "not", "buf"}
CONST = {"0", "1", "1'b0", "1'b1", "0'b0"}


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def is_const(n):
    return str(n) in CONST


def cval(n):
    return "1" if str(n) in ("1", "1'b1") else "0"


class Gate:
    def __init__(self, spec):
        self.s = spec
        self.warn, self.info = [], []
        self.width = {k: int(v) for k, v in (spec.get("nets") or {}).items()}
        self.nodes = {}            # id -> node
        self.build()

    # ---------- ネットリスト ----------
    def build(self):
        N = self.nodes
        for p in self.s.get("ports", []):
            N["port:" + p["id"]] = {"id": "port:" + p["id"], "kind": "pin_" + p.get("dir", "in"), "label": p.get("label", p["id"]),
                                    "pins": ({"y": ("out", p["id"])} if p.get("dir", "in") == "in" else {"a": ("in", p["id"])})}
        for c in self.s.get("cells", []):
            t = c.get("type", "buf").lower()
            pins = {}
            if t in GATES:
                ins = c.get("in") if isinstance(c.get("in"), list) else [c.get("in")]
                for i, n in enumerate(ins):
                    pins[f"i{i}"] = ("in", n)
                pins["y"] = ("out", c.get("out"))
            elif t == "mux":
                for i, n in enumerate(c.get("in", [])):
                    pins[f"i{i}"] = ("in", n)
                pins["s"] = ("in", c.get("s"))
                pins["y"] = ("out", c.get("out"))
            elif t in ("dff", "latch"):
                for k in ("sn", "d", "ck" if t == "dff" else "g", "rn"):
                    if c.get(k) is not None:
                        pins[k] = ("in", c.get(k))
                for k in ("q", "qn"):
                    if c.get(k) is not None:
                        pins[k] = ("out", c.get(k))
            elif t == "icg":
                for k in ("en", "te", "ck"):
                    if c.get(k) is not None:
                        pins[k] = ("in", c.get(k))
                pins["y"] = ("out", c.get("out"))
            else:
                self.warn.append(f"[未定義] {c['id']}: type {t} は使えない(and/or/nand/nor/xor/xnor/not/buf/mux/dff/latch/icg)")
                continue
            N[c["id"]] = {"id": c["id"], "kind": t, "cell": c, "label": c.get("label", c["id"]), "pins": pins}
        # 網: 駆動元と行き先
        self.drv = collections.defaultdict(list)
        self.snk = collections.defaultdict(list)
        for n in N.values():
            for pn, (d, net) in n["pins"].items():
                if net is None:
                    if d == "in":
                        self.warn.append(f"[未接続] {n['label']}.{pn}: 入力がつながっていない")
                    continue
                net = str(net)
                n["pins"][pn] = (d, net)
                if is_const(net):
                    continue
                (self.drv if d == "out" else self.snk)[net].append((n["id"], pn))
        for net in set(self.drv) | set(self.snk):
            if len(self.drv[net]) > 1:
                self.warn.append(f"[複数駆動] {net}: " + ", ".join(f"{a}.{b}" for a, b in self.drv[net]))
            if not self.drv[net] and self.snk[net]:
                self.warn.append(f"[未接続] {net}: 駆動元がない(入力端子かセルの出力にする)")
            if self.drv[net] and not self.snk[net]:
                self.info.append(f"[info] {net}: 使われていない出力")

    def net_of(self, nid, pn):
        return self.nodes[nid]["pins"].get(pn, (None, None))[1]

    # ---------- クロック・リセットの網 ----------
    def classify(self):
        """クロックの網(FF の ck・ラッチの g・ICG の ck へ、buf/not/icg/mux を通って届く)と、元の端子"""
        N = self.nodes
        clk = set()
        for n in N.values():
            for pn in ("ck", "g"):
                if pn in n["pins"] and n["kind"] in ("dff", "latch", "icg"):
                    net = n["pins"][pn][1]
                    if net and not is_const(net):
                        clk.add(net)
        changed = True
        while changed:                                     # 上流へ: クロックを作る buf/not/icg/mux/ゲートの入力も
            changed = False
            for net in list(clk):
                for nid, pn in self.drv.get(net, []):
                    n = N[nid]
                    if n["kind"] in ("buf", "not", "mux"):
                        ins = [v[1] for k, v in n["pins"].items() if v[0] == "in" and not (n["kind"] == "mux" and k == "s")]
                    elif n["kind"] == "icg":
                        ins = [n["pins"]["ck"][1]] if "ck" in n["pins"] else []
                    else:
                        ins = []
                    for x in ins:
                        if x and not is_const(x) and x not in clk:
                            clk.add(x)
                            changed = True
        for p in self.s.get("ports", []):
            if p.get("clock") and p.get("dir", "in") == "in":
                clk.add(p["id"])
        changed = True
        while changed:                                     # 下流へ: クロックが入るゲート・MUX のデータ・ICG の出力もクロック
            changed = False
            for n in N.values():
                if n["kind"] not in GATES | {"mux", "icg"}:
                    continue
                ins = [x for k, (d, x) in n["pins"].items() if d == "in" and k not in ("s", "en", "te")]
                out = n["pins"].get("y", (None, None))[1]
                if out and out not in clk and any(x in clk for x in ins if x):
                    clk.add(out)
                    changed = True
        self.clk = clk
        # 元(クロックの根): 入力端子、またはクロックの網に入っていない所から来る
        roots = []
        for net in sorted(clk):
            d = self.drv.get(net, [])
            if not d or N[d[0][0]]["kind"] == "pin_in" or N[d[0][0]]["kind"] in ("dff", "latch"):
                roots.append(net)
        self.clk_root = {}
        for i, r in enumerate(roots):
            self.clk_root[r] = (r, CLK_PAL[i % len(CLK_PAL)])
        # 各クロックの網の根(下流へ)
        def root(net, seen=()):
            if net in self.clk_root:
                return self.clk_root[net]
            rs = []
            for nid, pn in self.drv.get(net, []):
                n = N[nid]
                for k, (d, x) in n["pins"].items():
                    if d == "in" and x in clk and x not in seen and not (n["kind"] == "mux" and k == "s") and k not in ("en", "te"):
                        rs.append(root(x, seen + (net,)))
            rs = list(dict.fromkeys(rs))
            if len(rs) == 1:
                return rs[0]
            if len(rs) > 1:
                return ("/".join(r[0] for r in rs), "#475569")      # 切替後など、複数のクロックが混ざる
            return (net, GRAY)
        self.cnet = {net: root(net) for net in clk}
        rst = set()
        for n in N.values():
            for pn in ("rn", "sn"):
                if pn in n["pins"]:
                    net = n["pins"][pn][1]
                    if net and not is_const(net):
                        rst.add(net)
        self.rst = rst

    def ff_clock(self, nid):
        n = self.nodes[nid]
        net = n["pins"].get("ck", n["pins"].get("g", (None, None)))[1]
        return self.cnet.get(net, (net, GRAY))[0] if net else None

    # ---------- チェック ----------
    def check(self):
        N = self.nodes
        self.classify()
        # 組み合わせループ(FF/ラッチで切る)
        adj = collections.defaultdict(list)
        for n in N.values():
            if n["kind"] in ("dff", "latch", "pin_in", "pin_out"):
                continue
            for pn, (d, net) in n["pins"].items():
                if d == "out" and net:
                    for s, _ in self.snk.get(net, []):
                        if N[s]["kind"] not in ("dff", "latch", "pin_out"):
                            adj[n["id"]].append(s)
        color = {}
        def dfs(u, st):
            color[u] = 1
            st.append(u)
            for v in adj[u]:
                if color.get(v) == 1:
                    cyc = st[st.index(v):]
                    self.warn.append(f"[ループ] FF を通らない組み合わせのループ: {' → '.join(cyc + [v])}")
                elif not color.get(v):
                    dfs(v, st)
            st.pop()
            color[u] = 2
        for u in list(adj):
            if not color.get(u):
                dfs(u, [])
        # ゲーテッドクロック: FF/ラッチの ck が論理ゲート(AND/OR など)の出力
        for n in N.values():
            if n["kind"] not in ("dff", "latch"):
                continue
            net = n["pins"].get("ck", n["pins"].get("g", (None, None)))[1]
            for nid, _ in self.drv.get(net, []):
                if N[nid]["kind"] in ("and", "or", "nand", "nor", "xor", "xnor") and not N[nid]["cell"].get("clock_ok"):
                    self.warn.append(f"[クロック] {n['label']}: クロックを論理ゲート {N[nid]['label']} で作っている(グリッチの元)。"
                                     f"ICG を使うか、意図的なら clock_ok: true")
        # CDC: D に入る論理の元の FF のクロックが違う
        def cone(net, seen):
            """net の上流を組み合わせ論理だけたどり、元の FF と通った論理セルを返す"""
            ffs, logic = [], []
            for nid, _ in self.drv.get(net, []):
                n = N[nid]
                if nid in seen:
                    continue
                seen.add(nid)
                if n["kind"] in ("dff", "latch"):
                    ffs.append(nid)
                elif n["kind"] == "pin_in":
                    ffs.append(nid)
                else:
                    logic.append(nid)
                    for k, (d, x) in n["pins"].items():
                        if d == "in" and x and not is_const(x) and x not in self.clk:
                            a, b = cone(x, seen)
                            ffs += a
                            logic += b
            return ffs, logic
        async_in = {p["id"] for p in self.s.get("ports", []) if p.get("async")}
        self.sync1 = set()
        for n in N.values():
            if n["kind"] != "dff":
                continue
            ck = self.ff_clock(n["id"])
            dnet = n["pins"].get("d", (None, None))[1]
            if not dnet or is_const(dnet):
                continue
            ffs, logic = cone(dnet, set())
            other = []
            for x in ffs:
                if N[x]["kind"] == "pin_in":
                    pid = x.split(":", 1)[1]
                    if pid in async_in:
                        other.append(("端子 " + N[x]["label"], "非同期"))
                    continue
                cx = self.ff_clock(x)
                if cx and ck and cx != ck:
                    other.append((N[x]["label"], cx))
            if not other:
                continue
            src = ", ".join(f"{a}({b})" for a, b in other)
            if n["cell"].get("cdc_ok"):                   # 設計上わかって受けている(理由を残す)
                self.info.append(f"[info] CDC 確認済み {n['label']}: {n['cell']['cdc_ok']}")
                self.sync1.add(n["id"])
                continue
            if logic:
                self.warn.append(f"[CDC] {n['label']}({ck}): {src} から論理({', '.join(N[x]['label'] for x in logic)})を通って受けている。"
                                 f"組み合わせのグリッチがそのまま取り込まれる。同期化の前は FF の出力を直接つなぐ")
                continue
            # 1 段目: Q が同じクロックの FF の D に直接つながっていれば 2 段同期
            qn = n["pins"].get("q", (None, None))[1]
            nxt = [s for s, pn in self.snk.get(qn, []) if pn == "d" and N[s]["kind"] == "dff" and self.ff_clock(s) == ck]
            if nxt and len(self.snk.get(qn, [])) == 1:
                self.sync1.add(n["id"])
                self.sync1.add(nxt[0])
            else:
                why = "Q が他の論理にも分かれている" if nxt else "次の FF(同じクロック)がない"
                self.warn.append(f"[CDC] {n['label']}({ck}): {src} を 1 段で受けている({why})。2 段以上の同期化 FF にする")
        return self.warn

    # ---------- 形 ----------
    def geom(self, n):
        """(幅, 高さ, {ピン: (dx, dy, 側)})"""
        k = n["kind"]
        P = {}
        if k == "pin_in":
            w, h = wlen(n["label"]) * 6.6 + 26, 20
            P["y"] = (w, 10, "R")
        elif k == "pin_out":
            w, h = wlen(n["label"]) * 6.6 + 26, 20
            P["a"] = (0, 10, "L")
        elif k in ("not", "buf"):
            w, h = 40, 26
            P["i0"] = (0, 13, "L")
            P["y"] = (w, 13, "R")
        elif k in GATES:
            ni = len([p for p in n["pins"] if p.startswith("i")])
            h = max(32, ni * 14 + 6)
            w = 52 if k in ("xor", "xnor") else 48
            if k in ("nand", "nor", "xnor"):
                w += 6
            for i in range(ni):
                P[f"i{i}"] = (0, h / 2 + (i - (ni - 1) / 2) * 14, "L")
            P["y"] = (w, h / 2, "R")
        elif k == "mux":
            ni = len([p for p in n["pins"] if p.startswith("i")])
            h = max(46, ni * 16 + 26)
            w = 34
            for i in range(ni):
                P[f"i{i}"] = (0, 12 + i * 16, "L")
            P["s"] = (0, h - 8, "L")
            P["y"] = (w, h / 2 - 4, "R")
        elif k in ("dff", "latch"):
            w, h = 60, 72
            P["sn"] = (0, 10, "L")
            P["d"] = (0, 24, "L")
            P["ck" if k == "dff" else "g"] = (0, 44, "L")
            P["rn"] = (0, 62, "L")
            P["q"] = (w, 24, "R")
            P["qn"] = (w, 52, "R")
        elif k == "icg":
            w, h = 64, 62
            P["en"] = (0, 14, "L")
            P["te"] = (0, 30, "L")
            P["ck"] = (0, 48, "L")
            P["y"] = (w, 31, "R")
        elif k == "dummy":
            w, h = 0, 0
            P["i"] = (0, 0, "L")
            P["o"] = (0, 0, "R")
        else:
            w, h = 40, 30
        return w, h, P

    # ---------- 配置 ----------
    def layout(self):
        N = self.nodes
        # 接続(ノード単位): 網ごとに駆動元 → 行き先
        succ = collections.defaultdict(set)
        for net, ds in self.drv.items():
            for a, _ in ds:
                for b, _ in self.snk.get(net, []):
                    if a != b:
                        succ[a].add(b)
        # 戻りの辺を DFS で見つけて除く(入力端子から)
        order = [n for n in N if N[n]["kind"] == "pin_in"] + [n for n in N if N[n]["kind"] != "pin_in"]
        color, back = {}, set()
        def dfs(u):
            color[u] = 1
            for v in sorted(succ[u], key=order.index):
                if color.get(v) == 1:
                    back.add((u, v))
                elif not color.get(v):
                    dfs(v)
            color[u] = 2
        sys.setrecursionlimit(10000)
        for u in order:
            if not color.get(u):
                dfs(u)
        lvl = {u: 0 for u in N}
        indeg = collections.Counter()
        for u in N:
            for v in succ[u]:
                if (u, v) not in back:
                    indeg[v] += 1
        q = [u for u in order if indeg[u] == 0]
        topo = []
        while q:
            u = q.pop(0)
            topo.append(u)
            for v in sorted(succ[u], key=order.index):
                if (u, v) in back:
                    continue
                lvl[v] = max(lvl[v], lvl[u] + 1)
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        for u in N:
            if N[u]["kind"] == "pin_in":
                lvl[u] = 0
            elif lvl[u] == 0:
                lvl[u] = 1                                 # 定数だけで動くセルは 1 列目
        L = max([lvl[u] for u in N if N[u]["kind"] != "pin_out"] + [0]) + 1
        for u in N:
            if N[u]["kind"] == "pin_out":
                lvl[u] = L
        self.back = back
        self.lvl = lvl
        # 中継点(網ごと・列ごとに 1 つ)
        self.dummies = {}
        for net, ds in self.drv.items():
            if not ds:
                continue
            a = ds[0][0]
            far = [lvl[b] for b, _ in self.snk.get(net, []) if lvl[b] > lvl[a] + 1]
            for c in range(lvl[a] + 1, max(far) if far else 0):
                did = f"~{net}~{c}"
                N[did] = {"id": did, "kind": "dummy", "label": "", "pins": {"i": ("in", net), "o": ("out", net)}, "net": net}
                lvl[did] = c
                self.dummies[(net, c)] = did
        cols = collections.defaultdict(list)
        for u in order + [d for d in N if N[d]["kind"] == "dummy"]:
            if u in N:
                cols[lvl[u]].append(u)
        self.ncol = L + 1
        # 並び: 隣の列の位置の平均(バリセンタ)で数回並べ替え
        def neigh(u, side):
            out = []
            for pn, (d, net) in N[u]["pins"].items():
                if not net or is_const(net):
                    continue
                if side == "L" and d == "in":
                    for a, _ in self.drv.get(net, []):
                        da = self.dummies.get((net, lvl[u] - 1))
                        out.append(da if da else a)
                if side == "R" and d == "out":
                    for b, _ in self.snk.get(net, []):
                        db = self.dummies.get((net, lvl[u] + 1))
                        out.append(db if db and lvl[b] > lvl[u] + 1 else b)
            return [x for x in out if x in lvl and abs(lvl[x] - lvl[u]) == 1]
        pos = {}
        for c in range(self.ncol):
            for i, u in enumerate(cols[c]):
                pos[u] = i
        for it in range(6):
            rng = range(1, self.ncol) if it % 2 == 0 else range(self.ncol - 2, -1, -1)
            for c in rng:
                side = "L" if it % 2 == 0 else "R"
                def bc(u):
                    nb = neigh(u, side)
                    return sum(pos[x] for x in nb) / len(nb) if nb else pos[u]
                cols[c].sort(key=bc)
                for i, u in enumerate(cols[c]):
                    pos[u] = i
        self.cols = cols
        # 縦位置: つながるピンの高さにそろえる
        G = {u: self.geom(N[u]) for u in N}
        self.G = G
        Y = {}
        GAP, DGAP = 30, 12
        def gap(a, b):                                    # 下に来るのがセルなら名前の分をあける
            if N[b]["kind"] == "dummy":
                return DGAP if N[a]["kind"] == "dummy" else 16
            return GAP
        for c in range(self.ncol):
            y = 0
            for u in cols[c]:
                Y[u] = y
                y += G[u][1] + DGAP
        def pin_y(u, pn):
            return Y[u] + G[u][2][pn][1]
        def want(u, side_cols):
            ds = []
            for pn, (d, net) in N[u]["pins"].items():
                if not net or is_const(net) or pn not in G[u][2]:
                    continue
                my = G[u][2][pn][1]
                if d == "in":
                    for a, apn in self.drv.get(net, []):
                        da = self.dummies.get((net, lvl[u] - 1))
                        a2, ap2 = (da, "o") if da else (a, apn)
                        if lvl.get(a2) in side_cols and ap2 in G[a2][2]:
                            ds.append(pin_y(a2, ap2) - my)
                else:
                    for b, bpn in self.snk.get(net, []):
                        db = self.dummies.get((net, lvl[u] + 1))
                        b2, bp2 = (db, "i") if db and lvl[b] > lvl[u] + 1 else (b, bpn)
                        if lvl.get(b2) in side_cols and bp2 in G[b2][2]:
                            ds.append(pin_y(b2, bp2) - my)
            return sum(ds) / len(ds) if ds else None
        for it in range(8):
            rng = list(range(1, self.ncol)) if it % 2 == 0 else list(range(self.ncol - 2, -1, -1))
            for c in rng:
                sc = {c - 1} if it % 2 == 0 else {c + 1}
                w = {u: want(u, sc) for u in cols[c]}
                prev = None
                for u in cols[c]:
                    t = w[u] if w[u] is not None else Y[u]
                    Y[u] = t if prev is None else max(t, Y[prev] + G[prev][1] + gap(prev, u))
                    prev = u
        mn = min(Y.values()) if Y else 0
        for u in Y:
            Y[u] -= mn
        self.Y = Y

    # ---------- 配線(列の間の通路) ----------
    def route(self):
        N, G, Y, lvl = self.nodes, self.G, self.Y, self.lvl
        colw = [max([G[u][0] for u in self.cols[c]] + [0]) for c in range(self.ncol)]
        parts = collections.defaultdict(lambda: collections.defaultdict(lambda: {"src": [], "dst": []}))  # ch -> net -> 点
        nback = 0
        self.lanes = {}
        for net, ds in self.drv.items():
            if not ds:
                continue
            a, apn = ds[0]
            la = lvl[a]
            sinks = self.snk.get(net, [])
            fwd = [(b, bpn) for b, bpn in sinks if lvl[b] > la]
            bwd = [(b, bpn) for b, bpn in sinks if lvl[b] <= la]
            if fwd or bwd:
                parts[la][net]["src"].append((a, apn))
            for b, bpn in fwd:
                if lvl[b] == la + 1:
                    parts[la][net]["dst"].append((b, bpn))
                else:
                    parts[la][net]["dst"].append((self.dummies[(net, la + 1)], "i"))
            for c in range(la + 1, max([lvl[b] for b, _ in fwd] + [0])):
                d = self.dummies.get((net, c))
                if d:
                    parts[c][net]["src"].append((d, "o"))
                    for b, bpn in fwd:
                        if lvl[b] == c + 1:
                            parts[c][net]["dst"].append((b, bpn))
                        elif lvl[b] > c + 1 and (self.dummies.get((net, c + 1)), "i") not in parts[c][net]["dst"]:
                            parts[c][net]["dst"].append((self.dummies[(net, c + 1)], "i"))
            if bwd:                                       # 戻り: 上の通り道(レーン)を回る
                k = nback
                nback += 1
                self.lanes[net] = k
                parts[la][net]["dst"].append(("lane", k))
                for b, bpn in bwd:
                    parts[lvl[b] - 1][net]["src"].append(("lane", k))
                    parts[lvl[b] - 1][net]["dst"].append((b, bpn))
        self.nlanes = nback
        # 通路ごとの幹線の割り当て(区間が重ならないように)
        LANE0 = -40
        def py(pt):
            u, pn = pt
            if u == "lane":
                return LANE0 - pn * 14
            return Y[u] + G[u][2][pn][1]
        tracks, chw = {}, []
        for c in range(self.ncol):
            items = []
            for net, pp in parts[c].items():
                ys = [py(p) for p in pp["src"] + pp["dst"]]
                if not ys:
                    continue
                items.append((min(ys), max(ys), net))
            # 上から下へ流れる網を左、下から上へを右に置くと交差が減る
            items.sort(key=lambda t: (t[0], t[1]))
            ends = []
            for lo, hi, net in items:
                for i, e in enumerate(ends):
                    if e < lo - 8:
                        ends[i] = hi
                        tracks[(c, net)] = i
                        break
                else:
                    ends.append(hi)
                    tracks[(c, net)] = len(ends) - 1
            chw.append(max(44, len(ends) * 11 + 30))
        X, x = [], 30
        for c in range(self.ncol):
            X.append(x)
            x += colw[c] + (chw[c] if c < self.ncol - 1 else 0)
        self.X, self.colw, self.chw, self.tracks, self.parts = X, colw, chw, tracks, parts
        self.LANE0 = LANE0
        self.Wtot = x + 30
        self.py = py

    def px(self, u, pn):
        if u == "lane":
            return None
        c = self.lvl[u]
        w = self.G[u][0]
        x0 = self.X[c] + (self.colw[c] - w if self.nodes[u]["kind"] == "pin_out" else (0 if self.nodes[u]["kind"] == "dummy" else (self.colw[c] - w) / 2))
        if self.nodes[u]["kind"] == "dummy":
            return self.X[c] if pn == "i" else self.X[c] + self.colw[c]
        return x0 + self.G[u][2][pn][0]

    def ox(self, u):
        c = self.lvl[u]
        w = self.G[u][0]
        if self.nodes[u]["kind"] == "pin_out":
            return self.X[c] + self.colw[c] - w
        if self.nodes[u]["kind"] == "pin_in":
            return self.X[c]
        return self.X[c] + (self.colw[c] - w) / 2

    # ---------- 色 ----------
    def ncol_of(self, net):
        if net in getattr(self, "clk", ()):
            return self.cnet.get(net, (net, GRAY))[1]
        if net in getattr(self, "rst", ()):
            return RST_C
        return INK

    # ---------- 記号 ----------
    def sym(self, u):
        n = self.nodes[u]
        k = n["kind"]
        w, h, P = self.G[u]
        x, y = self.ox(u), self.Y[u] + self.top
        o = []
        S = f'fill="#fff" stroke="{INK}" stroke-width="1.6"'
        def bubble(cx, cy):
            return f'<circle cx="{f(cx)}" cy="{f(cy)}" r="3.6" {S}/>'
        if k == "pin_in":
            o.append(f'<path d="M{f(x)},{f(y)} L{f(x + w - 10)},{f(y)} L{f(x + w)},{f(y + h / 2)} L{f(x + w - 10)},{f(y + h)} L{f(x)},{f(y + h)} Z" '
                     f'fill="#f1f5f9" stroke="{INK}" stroke-width="1.4"/><text x="{f(x + 8)}" y="{f(y + 14)}" font-size="11" '
                     f'font-weight="700" fill="{INK}">{esc(n["label"])}</text>')
        elif k == "pin_out":
            o.append(f'<path d="M{f(x)},{f(y + h / 2)} L{f(x + 10)},{f(y)} L{f(x + w)},{f(y)} L{f(x + w)},{f(y + h)} L{f(x + 10)},{f(y + h)} Z" '
                     f'fill="#f1f5f9" stroke="{INK}" stroke-width="1.4"/><text x="{f(x + 16)}" y="{f(y + 14)}" font-size="11" '
                     f'font-weight="700" fill="{INK}">{esc(n["label"])}</text>')
        elif k in ("not", "buf"):
            tw = w - (8 if k == "not" else 0)
            o.append(f'<path d="M{f(x)},{f(y)} L{f(x + tw)},{f(y + h / 2)} L{f(x)},{f(y + h)} Z" {S}/>')
            if k == "not":
                o.append(bubble(x + tw + 3.6, y + h / 2))
        elif k in ("and", "nand"):
            bw = w - (8 if k == "nand" else 0)
            r = h / 2
            o.append(f'<path d="M{f(x)},{f(y)} L{f(x + bw - r)},{f(y)} A{f(r)},{f(r)} 0 0 1 {f(x + bw - r)},{f(y + h)} L{f(x)},{f(y + h)} Z" {S}/>')
            if k == "nand":
                o.append(bubble(x + bw + 3.6, y + h / 2))
        elif k in ("or", "nor", "xor", "xnor"):
            off = 6 if k in ("xor", "xnor") else 0
            bw = w - (8 if k in ("nor", "xnor") else 0)
            x0 = x + off
            o.append(f'<path d="M{f(x0)},{f(y)} Q{f(x0 + 12)},{f(y + h / 2)} {f(x0)},{f(y + h)} Q{f(x0 + (bw - off) * .62)},{f(y + h)} '
                     f'{f(x + bw)},{f(y + h / 2)} Q{f(x0 + (bw - off) * .62)},{f(y)} {f(x0)},{f(y)} Z" {S}/>')
            if off:
                o.append(f'<path d="M{f(x)},{f(y)} Q{f(x + 12)},{f(y + h / 2)} {f(x)},{f(y + h)}" fill="none" stroke="{INK}" stroke-width="1.6"/>')
            if k in ("nor", "xnor"):
                o.append(bubble(x + bw + 3.6, y + h / 2))
        elif k == "mux":
            o.append(f'<path d="M{f(x)},{f(y)} L{f(x + w)},{f(y + 10)} L{f(x + w)},{f(y + h - 18)} L{f(x)},{f(y + h)} Z" {S}/>')
            ni = len([p for p in P if p.startswith("i")])
            for i in range(ni):
                o.append(f'<text x="{f(x + 4)}" y="{f(y + P[f"i{i}"][1] + 3.5)}" font-size="9" fill="{GRAY}">{i}</text>')
            o.append(f'<text x="{f(x + 4)}" y="{f(y + P["s"][1] + 3.5)}" font-size="9" fill="{GRAY}">S</text>')
        elif k in ("dff", "latch"):
            o.append(f'<rect x="{f(x)}" y="{f(y)}" width="{w}" height="{h}" rx="2" {S}/>')
            used = n["pins"]
            for pn, lab in (("d", "D"), ("q", "Q"), ("qn", "QN"), ("g", "G")):
                if pn in used or pn in ("d", "q"):
                    dx, dy, sd = P[pn]
                    o.append(f'<text x="{f(x + dx + (5 if sd == "L" else -5))}" y="{f(y + dy + 3.5)}" font-size="9.5" fill="{INK}" '
                             f'text-anchor="{"start" if sd == "L" else "end"}">{lab}</text>')
            if k == "dff":                               # クロックの三角(FF のクロック端子)
                cy_ = y + P["ck"][1]
                o.append(f'<path d="M{f(x)},{f(cy_ - 6)} L{f(x + 9)},{f(cy_)} L{f(x)},{f(cy_ + 6)}" fill="none" stroke="{INK}" stroke-width="1.3"/>')
                if n["cell"].get("edge") == "neg":
                    o.append(f'<circle cx="{f(x - 3.6)}" cy="{f(cy_)}" r="3.4" {S}/>')
            for pn, lab in (("rn", "RN"), ("sn", "SN")):
                if pn in used:
                    dy = P[pn][1]
                    o.append(f'<text x="{f(x + 6)}" y="{f(y + dy + 3.5)}" font-size="9" fill="{RST_C}">{lab}</text>'
                             f'<circle cx="{f(x - 3.6)}" cy="{f(y + dy)}" r="3.4" {S}/>')
            if n["cell"].get("edge") == "neg" and k == "dff":
                pass
            o.append(f'<text x="{f(x + w / 2)}" y="{f(y + h - 4)}" font-size="8.5" fill="{GRAY}" text-anchor="middle">'
                     f'{"FF" if k == "dff" else "LAT"}</text>')
        elif k == "icg":
            o.append(f'<rect x="{f(x)}" y="{f(y)}" width="{w}" height="{h}" rx="3" {S}/>'
                     f'<text x="{f(x + w / 2 + 4)}" y="{f(y + h / 2 + 4)}" font-size="11" font-weight="800" fill="{INK}" text-anchor="middle">ICG</text>')
            for pn, lab in (("en", "EN"), ("te", "TE"), ("ck", "CK")):
                if pn in P:
                    o.append(f'<text x="{f(x + 5)}" y="{f(y + P[pn][1] + 3.5)}" font-size="8.5" fill="{GRAY}">{lab}</text>')
            o.append(f'<text x="{f(x + w - 4)}" y="{f(y + P["y"][1] - 5)}" font-size="8.5" fill="{GRAY}" text-anchor="end">GCK</text>')
        if k not in ("pin_in", "pin_out", "dummy"):
            nm = n["label"]
            o.append(f'<text class="h" x="{f(x + w / 2)}" y="{f(y - 5)}" font-size="9.5" fill="{GRAY}" text-anchor="middle">{esc(nm)}</text>')
            if n["cell"].get("note"):
                o.append(f'<text class="h" x="{f(x + w / 2)}" y="{f(y + h + 13)}" font-size="9.5" fill="#7c3aed" text-anchor="middle">'
                         f'{esc(n["cell"]["note"])}</text>')
            # タイ(定数)
            for pn, (d, net) in n["pins"].items():
                if d == "in" and net and is_const(net) and pn in P:
                    dx, dy, _ = P[pn]
                    o.append(f'<path d="M{f(x + dx - 14)},{f(y + dy)} L{f(x + dx)},{f(y + dy)}" stroke="{INK}" stroke-width="1.4"/>'
                             f'<text x="{f(x + dx - 16)}" y="{f(y + dy + 3.5)}" font-size="10" font-weight="700" fill="{INK}" '
                             f'text-anchor="end">{cval(net)}</text>')
        return "".join(o)

    # ---------- 図 ----------
    def svg(self):
        self.check()
        self.layout()
        self.route()
        N, Y = self.nodes, self.Y
        ttl = self.s.get("title", "ゲートレベル図")
        self.top = 70 + (22 if self.s.get("note") else 0) + (16 if self.s.get("groups") else 0) + self.nlanes * 14 + (20 if self.nlanes else 0)
        H = max([Y[u] + self.G[u][1] for u in N] + [0]) + self.top + 80
        W = max(self.Wtot, wlen(ttl) * 8.6 + 40, wlen(self.s.get("note", "")) * 5.8 + 40, 820)
        groups = self.s.get("groups", [])
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" font-family="\'Noto Sans JP\',sans-serif">'
             f'<style>.h{{paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}}</style>'
             f'<rect width="100%" height="100%" fill="#fff"/><text x="14" y="28" font-size="16" font-weight="800" fill="{INK}">{esc(ttl)}</text>']
        if self.s.get("note"):
            o.append(f'<text x="14" y="48" font-size="11" fill="{GRAY}">{esc(self.s["note"])}</text>')
        # 囲み
        for g in groups:
            us = [u for u in g.get("cells", []) if u in N]
            if not us:
                continue
            x0 = min(self.ox(u) for u in us) - 14
            x1 = max(self.ox(u) + self.G[u][0] for u in us) + 14
            y0 = min(Y[u] for u in us) + self.top - 24
            y1 = max(Y[u] + self.G[u][1] for u in us) + self.top + 20
            col = g.get("color", "#16a34a")
            o.append(f'<rect x="{f(x0)}" y="{f(y0)}" width="{f(x1 - x0)}" height="{f(y1 - y0)}" rx="6" fill="{col}" fill-opacity=".05" '
                     f'stroke="{col}" stroke-width="1.3" stroke-dasharray="6 3"/><text x="{f(x0 + 6)}" y="{f(y1 - 6)}" font-size="10.5" '
                     f'font-weight="700" fill="{col}">{esc(g.get("label", ""))}</text>')
        # 配線
        top = self.top
        lane_base = top - 30                               # 戻りのレーンは図の上(セルより上)
        def Yp(pt):
            if pt[0] == "lane":
                return lane_base - pt[1] * 14
            return self.py(pt) + top
        wires = []
        for c, nets in self.parts.items():
            for net, pp in nets.items():
                pts = pp["src"] + pp["dst"]
                if not pts:
                    continue
                t = self.tracks[(c, net)]
                tx = self.X[c] + self.colw[c] + 16 + t * 11
                col = self.ncol_of(net)
                bw = self.width.get(net, 1)
                sw = 3 if bw > 1 else 1.5
                ys = [Yp(p) for p in pts]
                path = ""
                for p in pp["src"]:
                    if p[0] == "lane":
                        continue
                    x0 = self.px(*p)
                    path += f"M{f(x0)},{f(Yp(p))} L{f(tx)},{f(Yp(p))} "
                for p in pp["dst"]:
                    if p[0] == "lane":
                        continue
                    x1 = self.px(*p)
                    path += f"M{f(tx)},{f(Yp(p))} L{f(x1)},{f(Yp(p))} "
                if max(ys) - min(ys) > .5:
                    path += f"M{f(tx)},{f(min(ys))} L{f(tx)},{f(max(ys))}"
                wires.append(f'<path d="{path}" fill="none" stroke="{col}" stroke-width="{sw}" stroke-linejoin="round"/>')
                if len(pts) > 2:
                    for yv in ys:
                        if min(ys) + .5 < yv < max(ys) - .5 or ys.count(yv) > 1:
                            wires.append(f'<circle cx="{f(tx)}" cy="{f(yv)}" r="2.8" fill="{col}"/>')
                # 戻りのレーン
                for p in pp["dst"]:
                    if p[0] == "lane":
                        self._lane_x = getattr(self, "_lane_x", {})
                        self._lane_x.setdefault(net, []).append(tx)
                for p in pp["src"]:
                    if p[0] == "lane":
                        self._lane_x = getattr(self, "_lane_x", {})
                        self._lane_x.setdefault(net, []).append(tx)
        for net, xs in getattr(self, "_lane_x", {}).items():
            yl = Yp(("lane", self.lanes[net]))
            col = self.ncol_of(net)
            wires.append(f'<path d="M{f(min(xs))},{f(yl)} L{f(max(xs))},{f(yl)}" fill="none" stroke="{col}" '
                         f'stroke-width="{3 if self.width.get(net, 1) > 1 else 1.5}"/>')
        # 中継点はまっすぐ通す
        for (net, c), d in self.dummies.items():
            y_ = Y[d] + top
            col = self.ncol_of(net)
            wires.append(f'<path d="M{f(self.X[c])},{f(y_)} L{f(self.X[c] + self.colw[c])},{f(y_)}" stroke="{col}" '
                         f'stroke-width="{3 if self.width.get(net, 1) > 1 else 1.5}"/>')
        o += wires
        # 網の名前とバス幅(駆動元の出力の右)
        for net, ds in self.drv.items():
            if not ds or not self.snk.get(net):
                continue
            a, apn = ds[0]
            if N[a]["kind"] == "pin_in":
                continue
            x0, y0 = self.px(a, apn), Y[a] + self.G[a][2][apn][1] + top
            col = self.ncol_of(net)
            o.append(f'<text class="h" x="{f(x0 + 4)}" y="{f(y0 - 4)}" font-size="9.5" fill="{col}">{esc(net)}</text>')
            bw = self.width.get(net, 1)
            if bw > 1:
                xm = x0 + 10
                o.append(f'<path d="M{f(xm - 4)},{f(y0 + 6)} L{f(xm + 4)},{f(y0 - 6)}" stroke="{col}" stroke-width="1.4"/>'
                         f'<text x="{f(xm + 2)}" y="{f(y0 + 14)}" font-size="9.5" font-weight="700" fill="{col}">{bw}</text>')
        for u in N:
            if N[u]["kind"] != "dummy":
                o.append(self.sym(u))
        # 凡例: クロック
        lx, ly = 14, H - 40
        items = [(r, c_) for r, c_ in {v for v in self.clk_root.values()}]
        legend = []
        for r, c_ in sorted(items):
            legend.append(f'<tspan fill="{c_}" font-weight="700">━ クロック {esc(r)}</tspan>')
        if any(v[1] == "#475569" for v in self.cnet.values()):
            legend.append('<tspan fill="#475569" font-weight="700">━ 複数のクロックが合流(切替後など)</tspan>')
        if self.rst:
            legend.append(f'<tspan fill="{RST_C}" font-weight="700">━ リセット</tspan>')
        legend.append(f'<tspan fill="{INK}">━ データ(太線 = バス、斜線と数字 = 幅)</tspan>')
        o.append(f'<text x="{lx}" y="{f(ly)}" font-size="11">' + "  ".join(legend) + '</text>')
        o.append(f'<text x="{lx}" y="{f(ly + 18)}" font-size="10.5" fill="{GRAY}">FF の三角 = クロック端子(丸付き = 立ち下がり)/ 丸 = 負論理 / '
                 f'ICG = クロックゲート(EN をラッチして CK と AND、TE = テスト時に開ける)/ 黒丸 = 分岐 / 上を回る線 = 戻りの接続</text>')
        o.append("</svg>")
        return "".join(o)


# ---------- 構造記述(ゲートのインスタンス)から ----------
CELLMAP = [(r"^(?:AN|AND)\d", "and"), (r"^(?:ND|NAND)\d", "nand"), (r"^(?:OR)\d", "or"), (r"^(?:NR|NOR)\d", "nor"),
           (r"^(?:XOR|XO)\d", "xor"), (r"^(?:XNOR|XN)\d", "xnor"), (r"^(?:INV|IV|NOT)", "not"), (r"^(?:BUF|BF|CKBUF)", "buf"),
           (r"^(?:MUX|MX)2", "mux"), (r"^(?:CKLN|ICG|CKGT|LATCHGATE)", "icg"), (r"^(?:LAT|DL)", "latch"), (r"^(?:DFF|DF|SDF)", "dff")]
PINMAP = {"D": "d", "CK": "ck", "CLK": "ck", "CP": "ck", "RN": "rn", "CDN": "rn", "RESETN": "rn", "SN": "sn", "SDN": "sn",
          "Q": "q", "QN": "qn", "G": "g", "E": "en", "EN": "en", "TE": "te", "SE": "te", "S": "s", "S0": "s",
          "Z": "out", "Y": "out", "ZN": "out", "Q_GCK": "out", "GCK": "out", "ECK": "out"}


def from_netlist(path, module=None):
    t = open(path, encoding="utf-8", errors="ignore").read()
    t = re.sub(r"//.*", "", t)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    mods = re.findall(r"\bmodule\s+(\w+)(.*?)\bendmodule", t, flags=re.S)
    if not mods:
        sys.exit("module が見つからない")
    name, body = next(((m, b) for m, b in mods if m == module), mods[-1])
    ports, nets, seen = [], {}, set()
    decl = r"\b(input|output|inout|wire)\b\s*(?:wire|reg|logic)?\s*(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*([\w\s,]*?)(?=\binput\b|\boutput\b|\binout\b|\bwire\b|;|\)|$)"
    for d, hi, lo, ids in re.findall(decl, body):
        for i in [x.strip() for x in ids.split(",") if x.strip()]:
            if hi:
                nets[i] = abs(int(hi) - int(lo)) + 1
            if d != "wire" and i not in seen:
                seen.add(i)
                ports.append({"id": i, "dir": "in" if d == "input" else "out"})
    cells, skipped = [], []
    for ct, inst, conns in re.findall(r"\b([A-Za-z_]\w*)\s+(?:#\(.*?\)\s*)?([A-Za-z_]\w*)\s*\(\s*(\..*?)\)\s*;", body, flags=re.S):
        typ = next((v for p, v in CELLMAP if re.match(p, ct.upper())), None)
        if typ is None:
            skipped.append(f"{inst}({ct})")
            continue
        pins = dict((a.upper(), b.strip()) for a, b in re.findall(r"\.(\w+)\s*\(\s*([^()]*?)\s*\)", conns))
        pins = {k: re.sub(r"^1'b", "", v) if re.match(r"^1'b[01]$", v) else v for k, v in pins.items()}
        c = {"id": inst, "type": typ, "label": f"{inst}"}
        if typ in GATES:
            c["in"] = [v for k, v in sorted(pins.items()) if k not in PINMAP or PINMAP[k] not in ("out",)]
            c["out"] = next((v for k, v in pins.items() if PINMAP.get(k) == "out"), None)
        elif typ == "mux":
            c["in"] = [pins.get("I0", pins.get("A", pins.get("D0"))), pins.get("I1", pins.get("B", pins.get("D1")))]
            c["s"] = pins.get("S", pins.get("S0", pins.get("SEL")))
            c["out"] = next((v for k, v in pins.items() if PINMAP.get(k) == "out"), None)
        else:
            for k, v in pins.items():
                m = PINMAP.get(k)
                if m:
                    c[m] = v
            if typ == "dff" and re.search(r"N$|NEG", ct.upper()):
                c["edge"] = "neg"
        cells.append(c)
    return {"title": f"{name}(ネットリストから)", "ports": ports, "nets": nets, "cells": cells}, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["render", "lint", "netlist"])
    ap.add_argument("src")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("-m", "--module")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    if a.cmd == "netlist":
        spec, skipped = from_netlist(a.src, a.module)
        open(a.out, "w", encoding="utf-8").write(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
        print(f"生成: {a.out}(セル {len(spec['cells'])})")
        if skipped:
            print("  [info] 対応表にないセル(手で type を足す): " + ", ".join(skipped))
        return
    spec = yaml.safe_load(open(a.src, encoding="utf-8"))
    g = Gate(spec)
    if a.cmd == "lint":
        g.check()
    else:
        svg = g.svg()
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, os.path.splitext(os.path.basename(a.src))[0] + ".svg")
        open(p, "w", encoding="utf-8").write(svg)
        if a.png:
            to_png(p)
        print(f"生成: {p}")
    for w in g.info:
        print("  " + w)
    print("lint:", "OK" if not g.warn else "")
    for w in g.warn:
        print("  " + w)


if __name__ == "__main__":
    main()
