#!/usr/bin/env python3
"""rtldb — RTL の接続をデータベース(SQLite)にし、クロックドメインと非同期の乗り換え(CDC・RDC)を検出する
  python3 rtldb.py build   -o design.db --top TOP [-I incdir] [-D NAME=VAL] [-f files.f] a.v b.sv ...
  python3 rtldb.py analyze design.db [-o out/] [--static 正規表現] [--async-in 正規表現]   # → cdc.md / cdc_map.svg / cdc_hier.svg
  python3 rtldb.py path    design.db --src 正規表現 --dst 正規表現   # 送り側から受け側まで、階層をまたぐ信号の並び
  python3 rtldb.py iface   design.db --inst top.u_x -o iface.yaml   # ブロック間の接続図(ifdiag.py)用の YAML
  python3 rtldb.py selftest                                         # 答えのわかる RTL(examples/rtl/cdc_expected.yaml)で確かめる
  python3 rtldb.py stats   design.db
方式(詳しくは reference/rtldb.md。図は cdcdiag.py と diagrams/cdc/GUIDE.md)
  1. slang(pyslang)でエラボレートする: パラメータ・generate・マクロ・インスタンス配列を展開した後の階層を得る。
  2. 階層を歩き、信号(端子・ネット・変数)を「階層パス」で一意な節点にし、駆動の関係を辺にする。
     節点の単位: 構造体はメンバごと、構造体の配列は要素ごと、ベクタは定数の添字・範囲で読み書きされた所だけ一部の節点(part)。
       comb: assign / always @* / ゲート       seq: クロックで叩く代入(dst は FF)     conn: インスタンスの端子接続
       arst: 非同期リセット                     role: data(右辺)/ ctrl(if・case の条件)  direct: 論理を通らない(名前・選択・反転だけ)
  3. クロック: FF のクロック端子から駆動を逆にたどり、源(トップ端子・ブラックボックスの出力・FF の出力=分周・
     複数クロックの合流=切替)を求める。ゲートされたクロックは元のドメインに含める。分周は元と同じ「系」(同期)とする。
  4. CDC: FF ごとに入力の論理の円錐を FF まで逆にたどり、系の違う FF があれば乗り換え。同期化の鎖を先に全部見つけてから分類する。
       同期化あり: sync / sync_gray / sync_pulse(エッジ検出)/ fifo(非同期 FIFO のデータ)/ handshake(同期化した条件で取り込む)
       要確認:     gated(同期化した信号と論理で合わせる)/ sync_bus / data_enable / mem / reconv(再収束)/ clk_as_data
       要対策:     sync_logic(同期化の前に論理)/ sync_tap(1 段目を使用)/ no_sync
  5. RDC: FF の非同期リセットを逆にたどる: rst_sync / rst_ok / rst_input / rst_logic / rst_cross
"""
import os, sys, re, json, argparse, sqlite3, collections, time
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE instance(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, module TEXT, parent INTEGER, depth INTEGER,
                      blackbox INTEGER, file TEXT, line INTEGER);
CREATE TABLE signal(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, inst INTEGER, width INTEGER,
                    kind TEXT,      -- port / net / var / pin(ブラックボックスの端子)/ pseudo
                    dir TEXT,       -- in / out / inout(端子のみ)
                    is_top INTEGER, is_mem INTEGER, file TEXT, line INTEGER);
CREATE TABLE edge(id INTEGER PRIMARY KEY, dst INTEGER, src INTEGER,
                  kind TEXT,        -- comb / seq / conn / arst
                  role TEXT,        -- data / ctrl
                  direct INTEGER,   -- 1 = 論理を通らない
                  inst INTEGER, line INTEGER);
CREATE TABLE ff(sig INTEGER PRIMARY KEY, clk INTEGER, edge TEXT, domain INTEGER);
CREATE TABLE clock(id INTEGER PRIMARY KEY, root INTEGER, name TEXT,
                   kind TEXT,       -- primary / blackbox / generated(分周)/ mux(切替)/ undriven
                   family INTEGER,  -- 同期とみなす系(源のドメイン)
                   parent TEXT, n_ff INTEGER, n_gated INTEGER);
CREATE TABLE cdc(id INTEGER PRIMARY KEY, src INTEGER, dst INTEGER, src_clk INTEGER, dst_clk INTEGER,
                 class TEXT, stages INTEGER, logic INTEGER, width INTEGER,
                 via INTEGER);     -- via: 条件・ポインタを同期化している FF(1 段目)。handshake / gated / fifo / reconv
CREATE TABLE rdc(id INTEGER PRIMARY KEY, src INTEGER, dst_clk INTEGER, class TEXT, n_ff INTEGER, example INTEGER);
CREATE INDEX e_dst ON edge(dst); CREATE INDEX e_src ON edge(src); CREATE INDEX s_inst ON signal(inst);
CREATE VIEW conn AS   -- 兄弟ブロック間・上位との接続(図を描く時の入口)
  SELECT e.id, si.path AS src_inst, s.name AS src_port, di.path AS dst_inst, d.name AS dst_port, d.width
  FROM edge e JOIN signal s ON s.id=e.src JOIN signal d ON d.id=e.dst
  JOIN instance si ON si.id=s.inst JOIN instance di ON di.id=d.inst WHERE e.kind='conn';
"""
OK_CLASSES = ("sync", "sync_gray", "sync_pulse", "fifo", "handshake")
INFO = ("static",)
REVIEW = ("gated", "sync_bus", "data_enable", "mem", "reconv", "clk_as_data")
BAD = ("no_sync", "sync_logic", "sync_tap")
CLASS_JA = {"sync": "同期化あり(2 段以上)", "sync_gray": "同期化あり(グレイコード)", "sync_pulse": "同期化 + エッジ検出(パルス/トグル)",
            "fifo": "非同期 FIFO のデータ(両向きのポインタ同期あり)", "handshake": "同期化した制御で取り込むデータ(ハンドシェイク)",
            "gated": "同期化した信号と論理で合わせて使用(ゲートの形・要確認)", "sync_bus": "多ビットを FF で同期化(要確認)", "data_enable": "受け側の条件で取り込むデータ(要確認)",
            "mem": "別クロックで書くメモリ(要確認)", "reconv": "別々に同期化した信号の再収束(要確認)",
            "clk_as_data": "クロックをデータとして使用(要確認)", "static": "準静的と宣言(設定レジスタ)",
            "sync_logic": "同期化の前に論理(グリッチ)", "sync_tap": "1 段目の出力を論理で使用", "no_sync": "同期化なし"}
RDC_JA = {"rst_sync": "リセット同期化回路(非同期アサート・同期解除)", "rst_ok": "同じドメインの FF が出すリセット",
          "rst_input": "外部入力のリセットを直接使用(解除が非同期・要確認)", "rst_logic": "複数の FF の論理で作るリセット(グリッチ・要確認)",
          "rst_cross": "別ドメインの FF が出すリセット(要対策)"}


# ================= 1. RTL → DB =================
class Builder:
    def __init__(self):
        import pyslang as ps
        self.ps, self.A = ps, ps.ast
        self.insts, self.sig, self.sigs, self.edges, self.ffs = [], {}, [], [], {}
        self.bbconn = []
        self.leaf, self.kids, self.nosplit, self.nhub, self.dim = {}, {}, False, 0, {}
        self.nwarn = collections.Counter()
    def compile(self, files, top, incs, defs, extra=""):
        ps = self.ps
        d = ps.driver.Driver()
        d.addStandardArgs()
        cmd = ["slang", "--error-limit", "0", "--ignore-unknown-modules", "--allow-use-before-declare", "--relax-enum-conversions",
               "-Wnone"]
        if top:
            cmd += ["--top", top]
        for i in incs:
            cmd += ["-I", i]
        for x in defs:
            cmd += ["-D", x]
        cmd += extra.split() + list(files)
        if not d.parseCommandLine(" ".join(cmd)) or not d.processOptions() or not d.parseAllSources():
            sys.exit("slang: ソースを読めない")
        self.comp = d.createCompilation()
        self.sm = self.comp.sourceManager
        self.root = self.comp.getRoot()
        self.errors = [x for x in self.comp.getAllDiagnostics() if x.isError()]
        return self.root.topInstances
    # ---- 節点 ----
    def loc(self, l):
        try:
            return os.path.basename(str(self.sm.getFileName(l))), self.sm.getLineNumber(l)
        except Exception:
            return "", 0
    def leaves(self, sym, inst=None):
        """信号の節点。構造体はメンバごとに分ける(配列の添字はまとめる)→ [(メンバ名の並び, 節点の番号)]"""
        p = sym.hierarchicalPath
        lv = self.leaf.get(p)
        if lv is not None:
            return lv
        A = self.A
        fl = []

        def walk(t, pre):
            ct = t.canonicalType
            et, mem = ct, False
            try:
                while et.isArray and len(fl) < 3000:
                    if et.hasFixedRange:
                        el = et.elementType.canonicalType
                        while el.isArray:
                            el = el.elementType.canonicalType
                        rg = et.fixedRange
                        if "StructType" in str(el.kind) and abs(rg.upper - rg.lower) < 256:
                            for i in range(min(rg.lower, rg.upper), max(rg.lower, rg.upper) + 1):
                                walk(et.elementType, pre + (f"[{i}]",))      # 構造体の配列は要素ごとに分ける
                            return
                    mem = mem or bool(et.isUnpackedArray)
                    et = et.elementType.canonicalType
                if "StructType" in str(et.kind):
                    for f in et:
                        if f.kind == A.SymbolKind.Field:
                            walk(f.type, pre + (f.name,))
                    return
                w = int(ct.elementType.bitWidth if ct.isUnpackedArray else ct.bitWidth)
            except Exception:
                w = 1
            dm = None
            try:
                if not mem and ct.hasFixedRange and "StructType" not in str(ct.kind):
                    dm = (min(ct.fixedRange.lower, ct.fixedRange.upper), abs(ct.fixedRange.upper - ct.fixedRange.lower) + 1,
                          ct.fixedRange.left >= ct.fixedRange.right)
            except Exception:
                dm = None
            fl.append((pre, max(w, 1), int(mem), dm))
        try:
            walk(sym.type, ())
        except Exception:
            fl = []
        if not fl or len(fl) >= 3000:
            fl = [((), 1, 0, None)]
        f, ln = self.loc(sym.location)
        if inst is None:
            inst = self.owner(sym)
        lv = []
        for pre, w, mem, dm in fl:
            i = len(self.sigs)
            if dm:
                self.dim[i] = dm
            suf = "".join(x if x.startswith("[") else "." + x for x in pre)
            self.sig[p + suf] = i
            self.sigs.append(dict(path=p + suf, name=sym.name + suf, inst=inst, width=w,
                                  kind="net" if sym.kind == A.SymbolKind.Net else "var", dir=None, is_top=0,
                                  is_mem=mem, file=f, line=ln))
            lv.append((pre, i))
        self.leaf[p] = lv
        return lv

    def sub(self, sym, path):
        """sym のうち、メンバ・添字の並び path より下の節点 → [(path からの相対の名前, 番号)]"""
        lv = self.leaves(sym)
        if not path:
            return lv
        out = []
        for pre, i in lv:
            n, ok = 0, True
            for comp in path:
                idx = isinstance(comp, tuple) or comp.startswith("[")
                if idx:
                    if n < len(pre) and pre[n].startswith("["):
                        if (pre[n] in comp) if isinstance(comp, tuple) else (pre[n] == comp):
                            n += 1
                        else:
                            ok = False
                            break
                    # 添字で分けていない配列: まとめたまま
                else:
                    while n < len(pre) and pre[n].startswith("["):      # 定数でない添字: どの要素も
                        n += 1
                    if n < len(pre) and pre[n] == comp:
                        n += 1
                    elif n < len(pre):
                        ok = False
                        break
            if ok:
                out.append((pre[n:], i))
        return out or lv

    def const(self, e):
        try:
            c = e.constant
            return None if c is None else int(str(c))
        except Exception:
            return None

    def is_sarr(self, t):
        """構造体の配列か"""
        try:
            t = t.canonicalType
            if not t.isArray:
                return False
            while t.isArray:
                t = t.elementType.canonicalType
            return "StructType" in str(t.kind)
        except Exception:
            return False

    def kid(self, b, lo, hi):
        """信号 b の一部(添字 lo..hi)の節点。定数の添字で読み書きされたときだけ作る"""
        d = self.kids.setdefault(b, {})
        i = d.get((lo, hi))
        if i is None:
            sg = self.sigs[b]
            suf = f"[{lo}]" if lo == hi else f"[{hi}:{lo}]"
            i = len(self.sigs)
            self.sig[sg["path"] + suf] = i
            self.sigs.append(dict(sg, path=sg["path"] + suf, name=sg["name"] + suf, width=hi - lo + 1, kind="part",
                                  dir=None, is_top=0))
            d[(lo, hi)] = i
        return i

    def nodes(self, r):
        sym, path, part = r
        lv = self.sub(sym, path)
        if part is None or len(lv) != 1 or self.sigs[lv[0][1]]["is_mem"] or self.nosplit:
            return [i for _, i in lv]
        return [self.kid(lv[0][1], *part)]

    def chain(self, e):
        """名前・メンバ・添字だけの式 → (信号, メンバ・添字の並び, 一部の範囲 or None)。それ以外は None"""
        K = self.A.ExpressionKind
        k = e.kind
        if k in (K.NamedValue, K.HierarchicalValue):
            s = e.symbol
            if s.kind == self.A.SymbolKind.Net or (s.kind == self.A.SymbolKind.Variable and
                                                   not str(s.lifetime).endswith("Automatic")):
                return (s, (), None)
            return None
        if k == K.MemberAccess:
            r = self.chain(e.value)
            return None if r is None else (r[0], r[1] + (e.member.name,), r[2])
        if k in (K.ElementSelect, K.RangeSelect):
            r = self.chain(e.value)
            if r is None or r[2] is not None:
                return r
            if k == K.ElementSelect:
                lo = hi = self.const(e.selector)
            else:
                a, b2 = self.const(e.left), self.const(e.right)
                sk = str(e.selectionKind).split(".")[-1]
                if a is None or b2 is None:
                    lo = hi = None
                elif sk == "IndexedUp":
                    lo, hi = a, a + b2 - 1
                elif sk == "IndexedDown":
                    lo, hi = a - b2 + 1, a
                else:
                    lo, hi = min(a, b2), max(a, b2)
            if lo is None or hi - lo > 4096:
                return r
            try:
                vt = e.value.type.canonicalType
                if self.is_sarr(vt):                      # 構造体の配列: 要素ごとの節点へ
                    comp = f"[{lo}]" if k == K.ElementSelect else tuple(f"[{i}]" for i in range(lo, hi + 1))
                    return (r[0], r[1] + (comp,), None)
                if "StructType" in str(vt.kind) or vt.isUnpackedArray:
                    return r
                return (r[0], r[1], (lo, hi))             # ベクタの一部
            except Exception:
                return r
        if k == K.Conversion:
            return self.chain(e.operand)
        return None

    def whole(self, e):
        """ペアにできる式(メンバごとに 1 対 1 でつなげる)の節点の並び。だめなら None"""
        K = self.A.ExpressionKind
        x = e
        while x.kind in (K.MemberAccess, K.ElementSelect, K.RangeSelect, K.Conversion):
            x = x.operand if x.kind == K.Conversion else x.value
        if x.kind not in (K.NamedValue, K.HierarchicalValue):
            return None
        r = self.chain(e)
        return self.sub(r[0], r[1]) if r and r[2] is None else None

    @staticmethod
    def pairs(L, R):
        """構造体どうしの対応 → [(左の節点の組, 右の節点の組)]。添字は順番で、メンバは名前で対応させる"""
        nm = lambda X: [tuple(c for c in a if not c.startswith("[")) for a, _ in X]
        if not L or not R:
            return None
        nl, nr = nm(L), nm(R)
        if len(L) > 1 and len(L) == len(R) and nl == nr:
            return [([a], [b]) for (_, a), (_, b) in zip(L, R)]
        dl, dr = collections.defaultdict(list), collections.defaultdict(list)
        for n, (_, i) in zip(nl, L):
            dl[n].append(i)
        for n, (_, i) in zip(nr, R):
            dr[n].append(i)
        if len(dl) > 1 and set(dl) == set(dr):            # 添字が定数でない配列: 同じ名前のメンバどうし
            return [(dl[n], dr[n]) for n in dl]
        return None

    def fan(self, dsts, srcs, kind, role, direct, ln):
        """srcs のすべて → dsts のすべて。本数が多いときは中継の節点を 1 つはさむ(辺の数を積にしない)"""
        dsts, srcs = set(dsts), set(srcs)
        if len(dsts) * len(srcs) <= 48 or min(len(dsts), len(srcs)) < 3:
            for t in dsts:
                for r in srcs:
                    self.edge(t, r, kind, role, direct, ln)
            return
        self.nhub += 1
        h = self.pseudo(f"{self.insts[self.cur]['path']}.$j{self.nhub}", "$j", "hub")
        for r in srcs:
            self.edge(h, r, "comb", "data", direct, ln)
        for t in dsts:
            self.edge(t, h, kind, role, 1, ln)

    def pseudo(self, path, name, kind="pseudo"):
        i = self.sig.get(path)
        if i is None:
            i = len(self.sigs)
            self.sig[path] = i
            self.sigs.append(dict(path=path, name=name, inst=self.cur, width=1, kind=kind, dir=None, is_top=0, is_mem=0,
                                  file="", line=0))
        return i
    def edge(self, dst, src, kind, role="data", direct=0, line=0):
        if dst != src or kind == "seq":
            self.edges.append((dst, src, kind, role, int(direct), self.cur, line))
    # ---- 式 ----
    def refs(self, e, out):
        """式に出てくる信号(節点の番号)を out に集める"""
        if e is None:
            return out
        K = self.A.ExpressionKind
        k = e.kind
        if k in (K.NamedValue, K.HierarchicalValue, K.MemberAccess, K.ElementSelect, K.RangeSelect):
            r = self.chain(e)
            if r is not None:
                out += self.nodes(r)
                x = e
                while x.kind in (K.MemberAccess, K.ElementSelect, K.RangeSelect, K.Conversion):
                    if x.kind == K.ElementSelect:
                        self.refs(x.selector, out)
                    elif x.kind == K.RangeSelect:
                        self.refs(x.left, out); self.refs(x.right, out)
                    x = x.operand if x.kind == K.Conversion else x.value
                return out
            if k in (K.NamedValue, K.HierarchicalValue):
                return out
        for a in ("left", "right", "operand", "value", "selector", "pred", "predicate"):
            x = getattr(e, a, None)
            if x is not None and hasattr(x, "kind"):
                self.refs(x, out)
        for a in ("operands", "arguments", "elements", "conditions", "rangeList"):
            xs = getattr(e, a, None)
            if xs is not None:
                for x in xs:
                    x = getattr(x, "expr", x)
                    if hasattr(x, "kind"):
                        self.refs(x, out)
        if k == K.Replication:
            self.refs(e.concat, out)
        if k == K.Streaming:
            for st in e.streams:
                self.refs(st.operand, out)
        return out

    def owner(self, sym):
        """信号が属するインスタンス(generate ブロックの中は外側のインスタンス)"""
        try:
            b = sym.parentScope.containingInstance
            return self.ipath.get(b.parentInstance.hierarchicalPath, self.cur)
        except Exception:
            return self.cur

    def lhs(self, e, tg, deps):
        """左辺: 代入先の信号を tg に、添字に出てくる信号を deps に"""
        K = self.A.ExpressionKind
        k = e.kind
        if k in (K.NamedValue, K.HierarchicalValue, K.MemberAccess, K.ElementSelect, K.RangeSelect):
            r = self.chain(e)
            if r is not None:
                tg += self.nodes(r)
                x = e
                while x.kind in (K.MemberAccess, K.ElementSelect, K.RangeSelect, K.Conversion):
                    if x.kind == K.ElementSelect:
                        self.refs(x.selector, deps)
                    elif x.kind == K.RangeSelect:
                        self.refs(x.left, deps); self.refs(x.right, deps)
                    x = x.operand if x.kind == K.Conversion else x.value
            return
        if k == K.Concatenation:
            for o in e.operands:
                self.lhs(o, tg, deps)
        elif k == K.Conversion:
            self.lhs(e.operand, tg, deps)
        elif k == K.Assignment:
            self.lhs(e.left, tg, deps)
        else:
            self.refs(e, tg)

    def plain(self, e):
        """論理を通らない式か(名前・選択・連接・幅の変換だけ)"""
        K = self.A.ExpressionKind
        k = e.kind
        if k in (K.NamedValue, K.HierarchicalValue):
            return True
        if k in (K.ElementSelect, K.RangeSelect, K.MemberAccess):
            sel = getattr(e, "selector", None)
            if sel is not None and self.refs(sel, []):
                return False
            return self.plain(e.value)
        if k == K.Conversion:
            return self.plain(e.operand)
        if k == K.UnaryOp:                                # 反転だけ(1 入力)はグリッチを作らない
            op = str(e.op).split(".")[-1]
            if op == "BitwiseNot" or (op == "LogicalNot" and e.operand.type.bitWidth == 1):
                return self.plain(e.operand)
            return False
        if k == K.Concatenation:
            return all(self.plain(o) or o.kind in (K.IntegerLiteral, K.UnbasedUnsizedIntegerLiteral) for o in e.operands)
        return False
    def line(self, e):
        try:
            return self.sm.getLineNumber(e.sourceRange.start)
        except Exception:
            return 0
    # ---- 文 ----
    def assign(self, e, kind, conds, clk=None, arst=()):
        ln = self.line(e)
        K = self.A.ExpressionKind
        R = e.right
        try:
            while R.kind == K.Conversion and R.operand.type.bitWidth == R.type.bitWidth:
                R = R.operand
            if R.kind == K.Concatenation and len(R.operands) >= 2 and not self.nosplit:
                rl = self.chain(e.left)
                lt = e.left.type.canonicalType
                lv = self.sub(rl[0], rl[1]) if rl and rl[2] is None else []
                if (len(lv) == 1 and not self.sigs[lv[0][1]]["is_mem"] and lt.hasFixedRange and not lt.isUnpackedArray
                        and "StructType" not in str(lt.kind) and lt.elementType.canonicalType.bitWidth == 1
                        and lt.fixedRange.left >= lt.fixedRange.right
                        and sum(o.type.bitWidth for o in R.operands) == lt.bitWidth):
                    lo = lt.fixedRange.right                  # {a, b, c} の代入: ビットの範囲ごとに分けてつなぐ
                    for o in reversed(list(R.operands)):
                        w = o.type.bitWidth
                        if w:
                            self.emit([self.kid(lv[0][1], lo, lo + w - 1)], self.refs(o, []), self.plain(o), [], None,
                                      kind, conds, clk, arst, ln)
                            lo += w
                    return
        except Exception:
            pass
        tg, deps = [], []
        self.lhs(e.left, tg, deps)
        rs = self.refs(e.right, [])
        d = self.plain(e.right) and not deps
        pr = self.pairs(self.whole(e.left), self.whole(e.right))      # 構造体どうし: メンバごとに 1 対 1
        self.emit(tg, rs, d, deps, pr, kind, conds, clk, arst, ln)

    def emit(self, tg, rs, d, deps, pr, kind, conds, clk, arst, ln):
        tg = set(tg)
        if pr is not None:
            for L, R in pr:
                self.fan(L, R, kind, "data", d, ln)
        else:
            self.fan(tg, rs, kind, "data", d, ln)
        cd = {}
        for r, pl in conds:
            cd[r] = cd.get(r, True) and pl
        for r in set(deps):
            cd[r] = False
        self.fan(tg, [r for r, pl in cd.items() if pl and r not in arst], kind, "ctrl", 1, ln)
        self.fan(tg, [r for r, pl in cd.items() if not pl and r not in arst], kind, "ctrl", 0, ln)
        if kind == "seq":
            for t in tg:
                if not rs and not conds and not deps:
                    self.edge(t, t, "seq", "data", 1, ln)        # 定数だけの FF も残す
                c0 = self.ffs.setdefault(t, clk)
                if c0 != clk:
                    self.nwarn["1 つの信号を複数のクロックで代入"] += 1
                for r in arst:
                    self.edge(t, r, "arst", "ctrl", 1, ln)

    def stmt(self, s, kind, conds, clk=None, arst=()):
        if s is None:
            return
        K = self.A.StatementKind
        k = s.kind
        if k == K.List:
            for x in s.list:
                self.stmt(x, kind, conds, clk, arst)
        elif k == K.Block:
            self.stmt(s.body, kind, conds, clk, arst)
        elif k == K.ExpressionStatement:
            e = s.expr
            if e.kind == self.A.ExpressionKind.Assignment:
                self.assign(e, kind, conds, clk, arst)
        elif k == K.Conditional:
            c = list(conds)
            for cd in s.conditions:
                e = cd.expr                              # if (x) / if (!x) は論理なし(1 本の信号そのまま)
                while e.kind == self.A.ExpressionKind.UnaryOp or (e.kind == self.A.ExpressionKind.Conversion):
                    e = e.operand
                pl = self.plain(e)
                c += [(r, pl) for r in self.refs(cd.expr, [])]
            self.stmt(s.ifTrue, kind, c, clk, arst)
            self.stmt(s.ifFalse, kind, c, clk, arst)
        elif k in (K.Case, K.PatternCase):
            c = list(conds) + [(r, False) for r in self.refs(s.expr, [])]
            for it in s.items:
                for x in getattr(it, "expressions", []):
                    c += [(r, False) for r in self.refs(x, [])]
            for it in s.items:
                self.stmt(it.stmt, kind, c, clk, arst)
            self.stmt(s.defaultCase, kind, c, clk, arst)
        elif k in (K.ForLoop, K.WhileLoop, K.DoWhileLoop, K.RepeatLoop, K.ForeachLoop, K.ForeverLoop):
            self.stmt(s.body, kind, conds, clk, arst)
        elif k == K.Timed:
            self.stmt(s.stmt, kind, conds, clk, arst)
    def proc(self, pb):
        A = self.A
        body = pb.body
        pk = str(pb.procedureKind)
        if pk.endswith("Initial") or pk.endswith("Final"):
            return
        evs = []
        if body.kind == A.StatementKind.Timed:
            t = body.timing
            lst = t.events if t.kind == A.TimingControlKind.EventList else [t]
            for ev in lst:
                if ev.kind == A.TimingControlKind.SignalEvent and str(ev.edge).split(".")[-1] in ("PosEdge", "NegEdge", "BothEdges"):
                    evs.append((str(ev.edge).split(".")[-1], ev.expr))
            body = body.stmt
        if not evs:
            return self.stmt(body, "comb", [])
        # 非同期リセット = イベントにあって、先頭の if の条件に出てくる信号。残りがクロック
        first = body
        while first is not None and first.kind in (A.StatementKind.Block, A.StatementKind.List):
            first = first.body if first.kind == A.StatementKind.Block else (first.list[0] if len(first.list) else None)
        cond0 = set()
        if first is not None and first.kind == A.StatementKind.Conditional:
            for cd in first.conditions:
                cond0 |= set(self.refs(cd.expr, []))
        cks, rst = [], []
        for edge, ex in evs:
            r = self.refs(ex, [])
            if len(evs) > 1 and r and set(r) <= cond0:
                rst += r
            else:
                cks.append((edge, ex, r))
        if not cks:
            cks, rst = [(evs[0][0], evs[0][1], self.refs(evs[0][1], []))], []
        edge, ex, r = cks[0]
        if len(r) == 1 and self.plain(ex):
            clk = r[0]
        else:                                             # posedge (a & b) のような式: 仮の節点を置く
            clk = self.pseudo(f"{pb.hierarchicalPath or self.insts[self.cur]['path']}.$clk{self.line(ex)}", "$clk")
            for x in r:
                self.edge(clk, x, "comb", "data", 0, self.line(ex))
        self.edgekind = edge
        self.stmt(body, "seq", [], (clk, edge), tuple(set(rst)))
    # ---- 階層 ----
    def scope(self, sc):
        A = self.A
        SK = A.SymbolKind
        for m in sc:
            k = m.kind
            if k == SK.Instance:
                self.instance(m)
            elif k == SK.InstanceArray:
                self.scope(m)
            elif k == SK.GenerateBlock:
                if not m.isUninstantiated:
                    self.scope(m)
            elif k == SK.GenerateBlockArray:
                self.scope(m)
            elif k in (SK.Net, SK.Variable):
                if not (k == SK.Variable and str(m.lifetime).endswith("Automatic")):
                    ts = self.leaves(m, self.cur)
                    ini = m.initializer if k == SK.Net else None      # wire x = 式;
                    if ini is not None:
                        d = self.plain(ini)
                        pr = self.pairs(ts, self.whole(ini))
                        for L, R in (pr if pr is not None else [([t for _, t in ts], self.refs(ini, []))]):
                            self.fan(L, R, "comb", "data", d, self.line(ini))
            elif k == SK.ContinuousAssign:
                self.assign(m.assignment, "comb", [])
            elif k == SK.ProceduralBlock:
                self.proc(m)
            elif k == SK.PrimitiveInstance:
                cs = list(m.portConnections)
                if cs:
                    tg, dp = [], []
                    self.lhs(cs[0], tg, dp)
                    rs = []
                    for c in cs[1:]:
                        self.refs(c, rs)
                    for t in tg:
                        for r in set(rs):
                            self.edge(t, r, "comb", "data", len(cs) == 2 and m.primitiveType.name in ("buf", "not"), 0)
            elif k == SK.UninstantiatedDef:
                self.blackbox(m)
    def blackbox(self, m):
        par = self.cur
        f, ln = self.loc(m.location)
        iid = len(self.insts)
        path = m.hierarchicalPath
        self.insts.append(dict(path=path, name=m.name, module=m.definitionName, parent=par, depth=self.insts[par]["depth"] + 1,
                               blackbox=1, file=f, line=ln))
        names = list(getattr(m, "portNames", []) or [])
        for j, ex in enumerate(getattr(m, "portConnections", []) or []):
            pn = names[j] if j < len(names) and names[j] else f"p{j}"
            e = getattr(ex, "expr", ex)
            if not hasattr(e, "kind"):
                continue
            self.cur = iid
            pin = self.pseudo(f"{path}.{pn}", pn, "pin")
            self.cur = par
            for r in set(self.refs(e, [])):
                self.bbconn.append((pin, r, int(self.plain(e))))
    def instance(self, inst):
        A = self.A
        par = self.cur
        f, ln = self.loc(inst.location)
        iid = len(self.insts)
        path = inst.hierarchicalPath
        self.ipath[path] = iid
        self.insts.append(dict(path=path, name=inst.name, module=inst.definition.name, parent=par,
                               depth=0 if par is None else self.insts[par]["depth"] + 1, blackbox=0, file=f, line=ln))
        # 端子: 中の信号を節点にし、上位の式とつなぐ
        self.cur = iid
        conns = []
        for pc in inst.portConnections:
            p = pc.port
            if p.kind != A.SymbolKind.Port or p.internalSymbol is None:
                continue
            lv = self.leaves(p.internalSymbol, iid)
            d = str(p.direction).split(".")[-1]
            d = {"In": "in", "Out": "out", "InOut": "inout"}.get(d, "inout")
            for _, s in lv:
                self.sigs[s].update(kind="port", dir=d, is_top=int(par is None))
            if pc.expression is not None:
                conns.append((lv, d, pc.expression))
        if par is None:
            for p in inst.body.portList:
                if p.kind == A.SymbolKind.Port and p.internalSymbol is not None:
                    d = {"In": "in", "Out": "out"}.get(str(p.direction).split(".")[-1], "inout")
                    for _, s in self.leaves(p.internalSymbol, iid):
                        self.sigs[s].update(kind="port", dir=d, is_top=1)
        self.cur = par if par is not None else iid
        for lv, d, ex in conns:
            e = ex.left if ex.kind == A.ExpressionKind.Assignment else ex
            ln2 = self.line(e)
            pr = self.pairs(lv, self.whole(e))            # 構造体の端子: メンバごとに 1 対 1
            if d in ("in", "inout"):
                pl = self.plain(e)
                for L, R in (pr if pr is not None else [([s for _, s in lv], self.refs(e, []))]):
                    self.fan(L, R, "conn", "data", pl, ln2)
            if d in ("out", "inout"):
                tg, dp = [], []
                self.lhs(e, tg, dp)
                for L, R in (pr if pr is not None else [([s for _, s in lv], tg)]):
                    self.fan(R, L, "conn", "data", 1, ln2)
        self.cur = iid
        self.scope(inst.body)
        self.cur = par
    def build(self, tops):
        self.ipath, self.cur = {}, None
        for t in tops:
            self.instance(t)
        driven = {e[0] for e in self.edges}
        self.bbpin = set()
        for pin, r, d in self.bbconn:                     # ブラックボックス: ほかに駆動のない信号は出力とみなす
            if r in driven:
                self.edges.append((pin, r, "conn", "data", d, self.sigs[pin]["inst"], 0))
            else:
                self.edges.append((r, pin, "conn", "data", 1, self.sigs[pin]["inst"], 0))
        self.parts()
    def parts(self):
        """一部だけ読み書きされた信号: 一部(part)と全体をつなぐ。
        全体 B への書き込み → 各 part。part への書き込み → 重なる part と「全体の読み出し」B$all"""
        fin, fout = collections.defaultdict(list), collections.defaultdict(list)
        for n, ed in enumerate(self.edges):
            fin[ed[0]].append(n)
            fout[ed[1]].append(n)
        add = []
        # 1) そのままの受け渡し(Y = X、端子の接続)をさかのぼって、一部の範囲を元の信号まで運ぶ
        kidset = {i for kd in self.kids.values() for i in kd.values()}
        def copy_of(y):
            if y in self.ffs or y not in self.dim or any(fin[i] for i in self.kids.get(y, {}).values() if i not in via):
                return None
            es = [self.edges[n] for n in fin[y] if self.edges[n][1] != y]
            if len({e[1] for e in es}) != 1 or any(e[2] not in ("comb", "conn") or e[3] != "data" or not e[4] for e in es):
                return None
            x = es[0][1]
            if x in kidset or x not in self.dim or self.dim[x][1:] != self.dim[y][1:] or self.sigs[x]["width"] != self.sigs[y]["width"]:
                return None
            return x
        via, copied, work, nprop = set(), set(), list(self.kids), 0
        while work and nprop < 400000:
            y = work.pop()
            x = copy_of(y)
            if x is None:
                continue
            copied.add(y)
            off = self.dim[x][0] - self.dim[y][0]
            new = False
            for (lo, hi), i in list(self.kids[y].items()):
                if i in via:
                    continue
                via.add(i)
                had = (lo + off, hi + off) in self.kids.get(x, {})
                xk = self.kid(x, lo + off, hi + off)
                kidset.add(xk)
                new = new or not had
                add.append((i, xk, "conn", "data", 1, self.sigs[y]["inst"], 0))
                nprop += 1
            if new:
                work.append(x)
        # 2) 一部と全体をつなぐ
        for b, kd in list(self.kids.items()):
            if b in copied:
                continue
            sg = self.sigs[b]
            wr = [n for n in fin[b] if self.edges[n][1] != b]
            W = bool(wr) or (sg["kind"] == "port" and sg["dir"] in ("in", "inout")) or b in self.bbpin
            KW = {rg: i for rg, i in kd.items() if fin[i]}
            for rg, i in kd.items():
                if rg in KW:
                    if i in self.ffs and b in self.ffs:           # FF の全体への代入は、一部の FF への代入でもある
                        for n in wr:
                            ed = self.edges[n]
                            add.append((i,) + ed[1:])
                    elif i not in self.ffs and W:
                        add.append((i, b, "conn", "data", 1, sg["inst"], 0))
                else:
                    cov, pos = sorted(r for r in KW if r[0] <= rg[1] and r[1] >= rg[0]), rg[0]
                    for lo, hi in cov:
                        if lo > pos:
                            break
                        pos = max(pos, hi + 1)
                    if W and pos <= rg[1]:                        # 一部の代入で覆われていない → 全体から
                        add.append((i, b, "conn", "data", 1, sg["inst"], 0))
            ks = sorted(kd.items())
            for x, (rx, ix) in enumerate(ks):                     # 重なる part どうし
                for ry, iy in ks[x + 1:]:
                    if ry[0] > rx[1]:
                        break
                    if rx in KW:
                        add.append((iy, ix, "conn", "data", 1, sg["inst"], 0))
                    if ry in KW:
                        add.append((ix, iy, "conn", "data", 1, sg["inst"], 0))
            if KW:
                self.cur = sg["inst"]
                br = self.pseudo(sg["path"] + "$all", sg["name"], "all")
                self.sigs[br].update(is_top=sg["is_top"], width=sg["width"], file=sg["file"], line=sg["line"])
                kset = set(kd.values())
                for n in fout[b]:                         # 全体を読んでいた辺は B$all から読む
                    ed = self.edges[n]
                    if ed[0] not in kset and ed[0] != b:
                        self.edges[n] = (ed[0], br) + ed[2:]
                if W or b in self.ffs:
                    add.append((br, b, "conn", "data", 1, sg["inst"], 0))
                for i in KW.values():
                    add.append((br, i, "conn", "data", 1, sg["inst"], 0))
        self.edges += add

    def save(self, path, meta):
        if os.path.exists(path):
            os.remove(path)
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO meta VALUES(?,?)", list(meta.items()))
        db.executemany("INSERT INTO instance VALUES(?,?,?,?,?,?,?,?,?)",
                       [(i, x["path"], x["name"], x["module"], x["parent"], x["depth"], x["blackbox"], x["file"], x["line"])
                        for i, x in enumerate(self.insts)])
        db.executemany("INSERT INTO signal VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       [(i, s["path"], s["name"], s["inst"], s["width"], s["kind"], s["dir"], s["is_top"], s["is_mem"], s["file"],
                         s["line"]) for i, s in enumerate(self.sigs)])
        db.executemany("INSERT INTO edge(dst,src,kind,role,direct,inst,line) VALUES(?,?,?,?,?,?,?)", list(dict.fromkeys(self.edges)))
        db.executemany("INSERT INTO ff(sig,clk,edge) VALUES(?,?,?)", [(s, c[0], c[1]) for s, c in self.ffs.items()])
        db.commit()
        return db

# ================= 2. DB → クロック・CDC =================
class Analyzer:
    def __init__(self, db):
        self.db = db
        self.S = {r[0]: dict(zip(("id", "path", "name", "inst", "width", "kind", "dir", "is_top", "is_mem"), r))
                  for r in db.execute("SELECT id,path,name,inst,width,kind,dir,is_top,is_mem FROM signal")}
        self.fin = collections.defaultdict(list)      # dst -> [(src, kind, role, direct)]
        self.fout = collections.defaultdict(list)
        for d, s, k, r, di in db.execute("SELECT dst,src,kind,role,direct FROM edge"):
            self.fin[d].append((s, k, r, di))
            self.fout[s].append((d, k, r, di))
        self.ffclk = {r[0]: r[1] for r in db.execute("SELECT sig,clk FROM ff")}
        sys.setrecursionlimit(100000)
    def name(self, s):
        return self.S[s]["path"]
    # ---- クロックの源 ----
    def clock_roots(self):
        """各クロック信号 → (ドメインの源の信号, 種類)。ゲート(クロック 1 本 + イネーブル)は元のドメインのまま"""
        memo, busy = {}, set()
        used = collections.Counter(self.ffclk.values())
        direct_root = set()                               # 論理なしでクロック端子に届く源(クロックらしい信号)
        for c in used:
            x, seen = c, set()
            while x not in seen:
                seen.add(x)
                srcs = {s for s, k, r, d in self.fin[x] if k != "arst"}
                if x in self.ffclk or len(srcs) != 1:
                    break
                x = next(iter(srcs))
            direct_root.add(x)
        self.gated = collections.Counter()
        def clocky(x):
            return x in direct_root or re.search(r"(clk|clock|ck)", self.S[x]["name"], re.I)
        def root(x):
            if x in memo:
                return memo[x]
            if x in busy:
                return (x, "loop")
            busy.add(x)
            srcs = {s for s, k, r, d in self.fin[x] if k != "arst"}
            if x in self.ffclk:
                res = (x, "generated")
            elif not srcs:
                sg = self.S[x]
                res = (x, "primary" if sg["is_top"] else ("blackbox" if sg["kind"] == "pin" else "undriven"))
            elif len(srcs) == 1:
                res = root(next(iter(srcs)))
            else:
                rs = {root(s) for s in srcs}
                ck = {r for r in rs if r[0] in direct_root or
                      (r[1] in ("primary", "blackbox", "mux", "undriven") and clocky(r[0]))}
                if len(ck) == 1:
                    res = next(iter(ck))                  # クロック 1 本 + イネーブル = ゲート
                    self.gate_pts.add(x)
                elif len(ck) >= 2:
                    res = (x, "mux")
                    self.mux_src[x] = sorted(r[0] for r in ck)
                elif len(rs) == 1:
                    res = next(iter(rs))
                else:
                    res = (x, "generated")                # FF の出力の組み合わせで作るクロック
                    self.gen_src[x] = sorted(r[0] for r in rs)
            busy.discard(x)
            memo[x] = res
            return res
        self.gate_pts, self.mux_src, self.gen_src = set(), {}, {}
        self.croot = {c: root(c) for c in used}
        return self.croot
    def domains(self):
        self.clock_roots()
        self.dom = {f: self.croot[c][0] for f, c in self.ffclk.items()}      # FF → ドメイン(源の信号)
        kinds = {r[0]: r[1] for r in self.croot.values()}
        fam = {}
        def family(dm, seen=()):
            if dm in fam:
                return fam[dm]
            k = kinds.get(dm)
            res = dm
            if k == "generated" and dm not in seen:
                ps = {self.dom[s] for s in ([dm] if dm in self.dom else self.gen_src.get(dm, [])) if s in self.dom}
                ps |= {self.dom[s] for s in self.gen_src.get(dm, []) if s in self.dom}
                fs = {family(p, seen + (dm,)) for p in ps}
                if len(fs) == 1:
                    res = next(iter(fs))
            fam[dm] = res
            return res
        self.kinds = kinds
        self.fam = {dm: family(dm) for dm in set(self.dom.values())}
        return self.dom
    # ---- 論理の円錐 ----
    def terminal(self, x):
        return x in self.ffclk or not any(k != "arst" for s, k, r, d in self.fin[x])
    def cone(self, x, memo, busy):
        """組み合わせだけを逆にたどり、行き着く終点(FF・入力)→ 途中に論理があるか"""
        if x in memo:
            return memo[x]
        if x in busy:
            return {}
        busy.add(x)
        out = {}
        for s, k, r, d in self.fin[x]:
            if k == "arst":
                continue
            if self.terminal(s):
                out[s] = out.get(s, False) or not d
            else:
                for t, lg in self.cone(s, memo, busy).items():
                    out[t] = out.get(t, False) or lg or not d
        busy.discard(x)
        memo[x] = out
        return out
    def ff_inputs(self, f, memo):
        data, ctrl = {}, {}
        for s, k, r, d in self.fin[f]:
            if k != "seq":
                continue
            tgt = data if r == "data" else ctrl
            if s == f:
                continue
            if self.terminal(s):
                tgt[s] = tgt.get(s, False) or not d
            else:
                for t, lg in self.cone(s, memo, set()).items():
                    if t != f:                            # 自分自身の保持(q <= en ? d : q)は入力に数えない
                        tgt[t] = tgt.get(t, False) or lg or not d
        return data, ctrl
    def next_stage(self, f, memo):
        """f を論理なしでそのまま受ける同じドメインの FF(2 段目)と、それ以外で使われているか"""
        nxt, other = [], False
        seen, st = set(), [(f, True)]
        while st:
            x, direct = st.pop()
            for d, k, r, di in self.fout[x]:
                if k == "arst":
                    other = True
                    continue
                if d in self.ffclk and k == "seq":
                    dd, dc = self.inp[d] if d in self.inp else self.ff_inputs(d, memo)
                    if direct and di and r == "data" and self.dom.get(d) == self.dom.get(f) and set(dd) <= {f} and not \
                            any(self.fam.get(self.dom.get(t)) != self.fam.get(self.dom.get(f)) for t in dc if t in self.dom):
                        nxt.append(d)
                    elif d != f:
                        other = True
                elif (d, direct and bool(di)) not in seen:
                    seen.add((d, direct and bool(di)))
                    if not self.fout[d] and self.S[d]["is_top"]:
                        other = True                       # そのまま出力端子へ
                    st.append((d, direct and bool(di)))
        return nxt, other
    def anc(self, i):
        out = []
        while i is not None:
            out.append(i)
            i = self.ipar.get(i)
        return out

    def cdc(self, static=(), async_in=()):
        self.domains()
        self.ipar = {r[0]: r[1] for r in self.db.execute("SELECT id,parent FROM instance")}
        S, dom, fam = self.S, self.dom, self.fam
        for p in [re.compile(x) for x in async_in]:       # 非同期と宣言した入力端子: それぞれ別のドメイン
            for i, sg in S.items():
                if sg["is_top"] and sg["dir"] == "in" and not self.fin[i] and p.search(sg["path"]) and i not in self.kinds:
                    dom[i] = i; fam[i] = i; self.kinds[i] = "async_in"
        roots = {d for d, k in self.kinds.items() if k in ("primary", "blackbox", "mux", "undriven")}
        roots |= {x for xs in self.mux_src.values() for x in xs if x not in self.ffclk}      # 切替の元のクロックも
        memo = {}
        self.inp = {f: self.ff_inputs(f, memo) for f in self.ffclk}
        pats = [re.compile(p) for p in static]
        stat = {f for f in self.ffclk if any(p.search(S[f]["path"]) for p in pats)}
        wmem = {x for x in self.ffclk if S[x]["is_mem"]}
        nsc = {}

        def nstage(x):
            if x not in nsc:
                nsc[x] = self.next_stage(x, memo)
            return nsc[x]

        def cross(f, excl):
            data, ctrl = self.inp[f]
            fm = fam[dom[f]]
            data = {t: v for t, v in data.items() if t not in excl}
            ctrl = {t: v for t, v in ctrl.items() if t not in excl}
            xd = {t: lg for t, lg in data.items() if t in dom and fam[dom[t]] != fm}
            xc = {t: lg for t, lg in ctrl.items() if t in dom and fam[dom[t]] != fm}
            if not data and len(xc) == 1 and not next(iter(xc.values())):
                # if (x) q <= 1; else q <= 0; の形: x をそのまま受けるのと同じ
                data, xd, xc = dict(xc), dict(xc), {}
                ctrl = {t: v for t, v in ctrl.items() if t not in xd}
            return data, ctrl, xd, xc

        def chain_of(f, data, xd, xc):
            """f が同期化の 1 段目なら、段の並び [f, 2 段目, ...]。違えば None"""
            if xc or len(data) != 1 or any(t in wmem for t in xd):
                return None
            if next(iter(data.values())) and S[next(iter(data))]["width"] != 1:
                return None                               # 多ビットを論理でまとめてから同期化: グリッチ
            # 送り側が 1 ビット 1 本だけなら、途中の反転・バッファではグリッチは出ない
            nxt, _ = nstage(f)
            if not nxt:
                return None
            ch, x = [f], nxt[0]
            for _ in range(8):
                ch.append(x)
                n2, _o = nstage(x)
                if not n2 or n2[0] in ch:
                    break
                x = n2[0]
            return ch
        # ---- 1 回目: 同期化の鎖を見つける ----
        cand = [f for f in self.ffclk if any((t in dom and fam[dom[t]] != fam[dom[f]]) or (t in roots and t != dom[f])
                                             for t in list(self.inp[f][0]) + list(self.inp[f][1]))]
        chains, chain_end, marks = {}, {}, collections.defaultdict(set)
        for f in cand:
            data, ctrl, xd, xc = cross(f, stat)
            if not xd:
                continue
            ch = chain_of(f, data, xd, xc)
            if ch:
                src = next(iter(xd))
                chains[f] = ch
                for x in ch[1:]:
                    chain_end[x] = (src, fam[dom[src]], f)
                a1, a2 = self.anc(S[src]["inst"]), set(self.anc(S[f]["inst"]))
                lca = next((i for i in a1 if i in a2), None)
                for j in [lca, self.ipar.get(lca)]:       # この階層(とその 1 つ上)に、同期化した制御がある
                    if j is not None:
                        marks[j].add((fam[dom[src]], fam[dom[f]]))
        def via_sync(start, fm, srcf, hops=3):
            """start(受け側ドメインの信号)が、srcf のドメインから同期化した信号で作られているか(FF を hops 段まで戻る)"""
            seen, fr = set(start), list(start)
            for _ in range(hops + 1):
                nx = []
                for t in fr:
                    if t in chain_end and chain_end[t][1] in srcf:
                        return chain_end[t][2]            # その同期化の 1 段目
                    if t in self.inp:
                        for u in list(self.inp[t][0]) + list(self.inp[t][1]):
                            if u not in seen and u in dom and fam[dom[u]] == fm:
                                seen.add(u); nx.append(u)
                fr = nx
                if not fr or len(seen) > 400:
                    break
            return None
        def bcone(f):
            seen, st = set(), [s for s, k, r, d in self.fin[f] if k == "seq"]
            while st and len(seen) < 30000:
                x = st.pop()
                if x in seen:
                    continue
                seen.add(x)
                if not self.terminal(x):
                    st += [s for s, k, r, d in self.fin[x] if k != "arst"]
            return seen

        def qualified(t, f, fm, bc):
            """t が f に届く前に、必ず「送り側ドメインから同期化した信号」と組み合わせ論理で合わさっているか(ゲートして渡す形)"""
            st, seen, gates = [t], {t}, set()
            while st:
                x = st.pop()
                for d, k, r, di in self.fout[x]:
                    if k == "arst" or (d != f and d not in bc):
                        continue
                    if d == f:
                        if not (k == "seq" and not di):
                            return None                   # そのまま FF に入っている
                        gates.add(None)
                    elif di:
                        if d not in seen:
                            seen.add(d); st.append(d)
                    else:
                        gates.add(d)
            if not gates or None in gates:
                return None
            sf = fam[dom[t]]
            for g in gates:
                cn = self.cone(g, memo, set())
                mine = [u for u in cn if u in dom and fam[dom[u]] == fm]
                v = via_sync(mine, fm, {sf}) if mine and not any(u in dom and fam[dom[u]] not in (fm, sf) for u in cn) else None
                if v is None:
                    return None
            return v
        # 非同期 FIFO のメモリ: 同じ階層(か 1 つ上)に 書き側→読み側 の同期化(書きポインタ)があり、
        # 書き込みの条件(満杯の判定)が 読み側 から同期化した信号(読みポインタ)で作られている
        fifo, fvia = set(), {}
        for m in wmem:
            a = fam[dom[m]]
            mk = marks.get(S[m]["inst"], ())
            others = {y for x, y in mk if x == a}
            v = via_sync(list(self.inp[m][1]), a, others) if others else None
            if v is not None:
                fifo.add(m)
                fvia[m] = v
        self.fifo_mem = fifo
        # ---- 2 回目: 分類 ----
        rows = []
        self.chain_rows = {}
        for f in cand:
            w = S[f]["width"]
            d0, c0 = self.inp[f]
            fm = fam[dom[f]]
            for t in set(d0) | set(c0):
                if t in dom and fam[dom[t]] != fm:
                    if t in stat:
                        rows.append((t, f, dom[t], dom[f], "static", 1, 0, w, None))
                    elif t in fifo:
                        rows.append((t, f, dom[t], dom[f], "fifo", 1, 0, w, fvia[t]))
                if t in roots and t != dom[f] and fam.get(t, t) != fm and self.kinds.get(t) != "async_in":
                    rows.append((t, f, t, dom[f], "clk_as_data", 1, 0, w, None))
            data, ctrl, xd, xc = cross(f, stat | fifo)
            xd = {t: v for t, v in xd.items() if not (t in roots and self.kinds.get(t) != "async_in")}
            xc = {t: v for t, v in xc.items() if not (t in roots and self.kinds.get(t) != "async_in")}
            if not xd and not xc:
                continue
            stages, via = 1, None
            nxt, other = nstage(f)
            same_ctrl = [t for t in ctrl if t in dom and fam[dom[t]] == fm]
            ch = chain_of(f, data, xd, xc) if xd else None
            if ch:
                stages = len(ch)
                use = collections.Counter()
                for x in ch[1:]:
                    for y in {d for d, k, r, di in self.fout[x] if d not in ch}:
                        use[y] += 1
                if other:
                    cls = "sync_tap"
                elif w == 1:
                    cls = "sync_pulse" if any(v > 1 for v in use.values()) else "sync"
                elif re.search(r"gr[ae]?y", S[f]["name"] + S[next(iter(xd))]["name"], re.I):
                    cls = "sync_gray"
                else:
                    cls = "sync_bus"
            elif not xc and len(data) == 1 and not next(iter(data.values())):
                cls = "data_enable" if same_ctrl else "no_sync"
            elif any(t in wmem for t in xd) and not xc:
                cls = "mem"
            elif not xc and nxt and not other:
                cls = "sync_logic"
            elif not xc and same_ctrl:
                cls = "data_enable"
            else:
                cls = "no_sync"
            if cls == "data_enable":
                via = via_sync(same_ctrl, fm, {fam[dom[t]] for t in xd})
                if via is not None:
                    cls = "handshake"                     # 条件が、送り側ドメインから同期化した信号で作られている(MCP)
            if cls == "no_sync":
                bc = bcone(f)
                qs = [qualified(t, f, fm, bc) for t in {**xc, **xd}]
                if all(q is not None for q in qs):
                    cls, via = "gated", qs[0]             # 同期化した信号と組み合わせてから使っている
            for t, lg in {**xc, **xd}.items():
                rows.append((t, f, dom[t], dom[f], cls, stages, int(lg), w, via))
        # ---- 再収束: 別々に同期化した信号(同じ送り側ドメイン)を 1 つの FF で合わせている ----
        ends = {}
        for x, (src, sf, first) in chain_end.items():
            n2, _ = nstage(x)
            if not n2 or n2[0] not in chain_end:          # 鎖の最後の段
                ends[x] = (src, sf, first)
        for f in self.ffclk:
            if f in chain_end or f in chains:
                continue
            d0, c0 = self.inp[f]
            hit = collections.defaultdict(set)
            for t in set(d0) | set(c0):
                if t in ends and S[ends[t][2]]["width"] == 1:
                    hit[ends[t][1]].add(t)
            for sf, ts in hit.items():
                by = collections.defaultdict(set)         # 送り側が同じインスタンス = 関係のある信号とみなす
                for t in ts:
                    by[S[ends[t][0]]["inst"]].add(t)
                for g in by.values():
                    if len({ends[t][2] for t in g}) >= 2:
                        for t in g:
                            rows.append((ends[t][0], f, dom[ends[t][0]], dom[f], "reconv", 1, 0, S[f]["width"], ends[t][2]))
        self.rdc_rows = self.rdc(memo)
        return rows

    def rdc(self, memo):
        """非同期リセットの出どころ: FF ごとに、リセット端子を逆にたどる → [(源, 受け側ドメイン, 分類, FF 数, 例)]"""
        S, dom, fam = self.S, self.dom, self.fam
        tm, info = {}, {}
        for f in self.ffclk:
            rs = [s for s, k, r, d in self.fin[f] if k == "arst"]
            if not rs:
                continue
            terms = {}
            for r in rs:
                if r not in tm:
                    tm[r] = {r: False} if self.terminal(r) else self.cone(r, memo, set())
                for t, lg in tm[r].items():
                    sg = S[t]
                    if t in self.ffclk or sg["is_top"] or sg["kind"] == "pin" or self.fin[t]:      # 定数につないだ端子は除く
                        terms[t] = terms.get(t, False) or lg
            if terms:
                info[f] = terms
        used = {t for terms in info.values() for t in terms if t in self.ffclk}     # リセットを出している FF
        rsync = set(used)
        for _ in range(4):                                # その前段(そのまま次の段に渡すだけの FF)も同期化回路
            for f in info:
                if f not in rsync:
                    outs = {d for d, k, r, di in self.fout[f] if d != f}
                    if outs and all(d in rsync and k == "seq" and di for d, k, r, di in self.fout[f] if d != f):
                        rsync.add(f)
        agg, ex = collections.Counter(), {}
        for f, terms in info.items():
            ffs = [t for t in terms if t in self.ffclk]
            fm = fam[dom[f]]
            if f in rsync and not any(fam[dom[t]] != fm for t in ffs):
                cls = "rst_sync"
            elif any(fam[dom[t]] != fm for t in ffs):
                cls = "rst_cross"
            elif len(ffs) >= 2:
                cls = "rst_logic"
            elif ffs:
                cls = "rst_ok"
            else:
                cls = "rst_input"
            src = min(ffs or terms, key=lambda t: S[t]["path"]) if terms else f
            agg[(src, dom[f], cls)] += 1
            ex.setdefault((src, dom[f], cls), f)
        return [(src, dm, cls, n, ex[(src, dm, cls)]) for (src, dm, cls), n in agg.items()]

    def save(self, rows):
        db = self.db
        db.execute("DELETE FROM clock"); db.execute("DELETE FROM cdc")
        nff = collections.Counter(self.dom[f] for f in self.ffclk)
        gated = collections.Counter()
        for f, c in self.ffclk.items():
            x, seen = c, set()
            while x not in seen and x not in self.gate_pts:
                seen.add(x)
                srcs = {s for s, k, r, d in self.fin[x] if k != "arst"}
                if len(srcs) != 1 or x in self.ffclk:
                    break
                x = next(iter(srcs))
            if x in self.gate_pts:
                gated[self.dom[f]] += 1
        for dm in sorted(nff, key=lambda d: -nff[d]):
            k = self.kinds.get(dm, "?")
            par = self.mux_src.get(dm) or self.gen_src.get(dm) or ([self.dom[dm]] if dm in self.dom else [])
            db.execute("INSERT INTO clock VALUES(?,?,?,?,?,?,?,?)",
                       (dm, dm, self.name(dm), k, self.fam[dm], ", ".join(self.name(p) for p in par), nff[dm], gated[dm]))
        for dm in sorted({r[2] for r in rows} - set(nff)):        # FF のないドメイン(非同期の入力端子・データに使ったクロック)
            k = self.kinds.get(dm, "?")
            db.execute("INSERT INTO clock VALUES(?,?,?,?,?,?,?,?)", (dm, dm, self.name(dm), k, dm, "", 0, 0))
        db.execute("DELETE FROM rdc")
        db.executemany("INSERT INTO rdc(src,dst_clk,class,n_ff,example) VALUES(?,?,?,?,?)", self.rdc_rows)
        db.executemany("UPDATE ff SET domain=? WHERE sig=? AND sig IN (SELECT sig FROM ff)", [(d, f) for f, d in self.dom.items()])
        db.executemany("INSERT INTO cdc(src,dst,src_clk,dst_clk,class,stages,logic,width,via) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        db.commit()

# ================= 3. 出力 =================
def short(p):
    return p.split(".", 1)[1] if "." in p else p

def report(db, out):
    top = db.execute("SELECT value FROM meta WHERE key='top'").fetchone()[0]
    L = [f"# CDC レポート: {top}", ""]
    n = {k: db.execute(f"SELECT count(*) FROM {k}").fetchone()[0] for k in ("instance", "signal", "edge", "ff")}
    L += [f"インスタンス {n['instance']} / 信号 {n['signal']} / 辺 {n['edge']} / FF(信号単位){n['ff']}", "",
          "## クロックドメイン", "", "| ドメイン(源) | 種類 | 同期の系 | 元 | FF | うちゲート付き |", "|---|---|---|---|---|---|"]
    cn = {}
    for i, name, kind, fam, par, nff, ng in db.execute("SELECT id,name,kind,family,parent,n_ff,n_gated FROM clock ORDER BY n_ff DESC"):
        cn[i] = short(name)
    for i, name, kind, fam, par, nff, ng in db.execute("SELECT id,name,kind,family,parent,n_ff,n_gated FROM clock ORDER BY n_ff DESC"):
        L.append(f"| `{short(name)}` | {kind} | `{cn.get(fam, '?')}` | {' '.join('`' + short(p) + '`' for p in par.split(', ') if p)} | {nff} | {ng or ''} |")
    L += ["", "## 乗り換え(受け側の FF 単位)", "", "| 送り側 → 受け側 | " + " | ".join(CLASS_JA[c] for c in OK_CLASSES + REVIEW + BAD) + " |",
          "|---|" + "---|" * len(OK_CLASSES + REVIEW + BAD)]
    agg = collections.defaultdict(collections.Counter)
    for sc, dc, cls, cnt in db.execute("SELECT src_clk,dst_clk,class,count(DISTINCT dst) FROM cdc WHERE class!='static' GROUP BY 1,2,3"):
        agg[(sc, dc)][cls] = cnt
    for (sc, dc), c in sorted(agg.items(), key=lambda kv: -sum(kv[1].values())):
        L.append(f"| `{cn[sc]}` → `{cn[dc]}` | " + " | ".join(str(c[x]) if c[x] else "" for x in OK_CLASSES + REVIEW + BAD) + " |")
    cand = db.execute("""SELECT s.path, count(DISTINCT c.dst) n FROM cdc c JOIN signal s ON s.id=c.src
                         WHERE c.class='no_sync' GROUP BY c.src HAVING n>=5 ORDER BY n DESC LIMIT 30""").fetchall()
    if cand:
        L += ["", "## 準静的な設定レジスタの候補", "",
              "同期化なしで多くの FF に届いている送り側です。動作中に変えない設定なら `analyze --static '正規表現'` で宣言すると、要対策から外れます。", "",
              "| 送り側 | 届く FF |", "|---|---|"] + [f"| `{short(p)}` | {n} |" for p, n in cand]
    for title, classes in (("要対策(同期化なし・グリッチ・1 段目の使用)", BAD), ("要確認", REVIEW), ("同期化あり", OK_CLASSES),
                           ("準静的と宣言した乗り換え", INFO)):
        q = ",".join("'%s'" % c for c in classes)
        rows = db.execute(f"""SELECT d.path, c.class, c.stages, c.width, group_concat(DISTINCT s.path), sc.name, dc.name, d.file, d.line, max(c.logic)
                              FROM cdc c JOIN signal s ON s.id=c.src JOIN signal d ON d.id=c.dst
                              JOIN clock sc ON sc.id=c.src_clk JOIN clock dc ON dc.id=c.dst_clk
                              WHERE c.class IN ({q}) GROUP BY c.dst ORDER BY c.class, d.path""").fetchall()
        L += ["", f"## {title}: {len(rows)} 件", ""]
        if rows:
            L += ["| 受け側の FF | 分類 | 段 | 幅 | 送り側 | クロック | 場所 |", "|---|---|---|---|---|---|---|"]
        for dp, cls, stg, w, srcs, scn, dcn, f, ln, lg in rows[:400]:
            ss = srcs.split(",")
            L.append(f"| `{short(dp)}` | {CLASS_JA[cls]} | {stg if cls.startswith('sync') else ''} | {w} | "
                     + ", ".join(f"`{short(x)}`" for x in ss[:4]) + (f" ほか {len(ss) - 4}" if len(ss) > 4 else "")
                     + f" | `{short(scn)}` → `{short(dcn)}` | {f}:{ln} |")
        if len(rows) > 400:
            L.append(f"| …ほか {len(rows) - 400} 件 | | | | | | |")
    # 階層の深さ: 送り側から共通の親まで何段上がり、受け側まで何段下がるか
    par = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id,parent,depth FROM instance")}
    def chain(i):
        o = []
        while i is not None:
            o.append(i); i = par[i][0]
        return o
    hop, dmax = collections.Counter(), collections.Counter()
    for si, di in db.execute("SELECT DISTINCT s.inst, d.inst FROM cdc c JOIN signal s ON s.id=c.src JOIN signal d ON d.id=c.dst"):
        a, b = chain(si), chain(di)
        sb = set(b)
        l = next(x for x in a if x in sb)
        hop[(par[si][1] - par[l][1]) + (par[di][1] - par[l][1])] += 1
        dmax[max(par[si][1], par[di][1])] += 1
    if hop:
        L += ["", "## 階層の深さ(乗り換えの両端)", "",
              f"インスタンスの深さは最大 {max(v[1] for v in par.values())} 段。端のある一番深い階層は {max(dmax)} 段。"
              f"送り側から共通の親まで上がり、受け側まで下がる段数の合計は最大 {max(hop)} 段。", "",
              "| 上がり + 下がりの段数 | 送り側と受け側のインスタンスの組 |", "|---|---|"]
        L += [f"| {k} | {hop[k]} |" for k in sorted(hop)]
    rd = db.execute("""SELECT r.class, s.path, k.name, r.n_ff, e.path FROM rdc r JOIN signal s ON s.id=r.src
                       JOIN clock k ON k.id=r.dst_clk JOIN signal e ON e.id=r.example ORDER BY r.class, r.n_ff DESC""").fetchall()
    if rd:
        L += ["", "## 非同期リセット(RDC)", "", "FF の非同期リセット端子を逆にたどった結果です(リセットの源・受け側のドメインごと)。", "",
              "| 分類 | リセットの源 | 受け側のドメイン | FF | 例 |", "|---|---|---|---|---|"]
        for cls, sp, kn, n, ep in rd[:200]:
            L.append(f"| {RDC_JA[cls]} | `{short(sp)}` | `{short(kn)}` | {n} | `{short(ep)}` |")
        if len(rd) > 200:
            L.append(f"| …ほか {len(rd) - 200} 行 | | | | |")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "cdc.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    return agg, cn

def cdc_map(db, out, agg, cn, png=False):
    """ドメインの表(行 = 送り側、列 = 受け側)。マスに分類ごとの件数"""
    from xml.sax.saxutils import escape as esc
    doms = [(i, short(nm), k, n) for i, nm, k, n in db.execute("SELECT id,name,kind,n_ff FROM clock ORDER BY n_ff DESC")]
    used = {x for k in agg for x in k}
    doms = [d for d in doms if d[0] in used] or doms[:8]
    top = db.execute("SELECT value FROM meta WHERE key='top'").fetchone()[0]
    LW = max([180] + [len(d[1]) * 7.4 + 90 for d in doms])
    CW, RH, X0, Y0 = 150, 64, 20, 110
    W = max(X0 + LW + len(doms) * CW + 30, 1500)
    H = Y0 + 40 + len(doms) * RH + 110
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" font-family="\'Noto Sans JP\',sans-serif">'
         f'<rect width="100%" height="100%" fill="#fff"/><text x="{X0}" y="30" font-size="17" font-weight="800" fill="#0f172a">'
         f'クロックドメインの乗り換え(CDC): {esc(top)}</text><text x="{X0}" y="52" font-size="11.5" fill="#64748b">'
         f'行 = 送り側、列 = 受け側。数字は受け側の FF(信号単位)の数。RTL の接続データベースから自動検出</text>']
    for j, d in enumerate(doms):
        x = X0 + LW + j * CW
        o.append(f'<text x="{x + CW / 2}" y="{Y0 + 14}" font-size="11" font-weight="800" fill="#0f172a" text-anchor="middle">{esc(d[1][-22:])}</text>'
                 f'<text x="{x + CW / 2}" y="{Y0 + 29}" font-size="10" fill="#64748b" text-anchor="middle">{d[2]}・FF {d[3]}</text>')
    for i, s in enumerate(doms):
        y = Y0 + 40 + i * RH
        o.append(f'<text x="{X0}" y="{y + 26}" font-size="11.5" font-weight="800" fill="#0f172a">{esc(s[1])}</text>'
                 f'<text x="{X0}" y="{y + 42}" font-size="10" fill="#64748b">{s[2]}・FF {s[3]} →</text>')
        for j, d in enumerate(doms):
            x = X0 + LW + j * CW
            c = agg.get((s[0], d[0]))
            if i == j:
                o.append(f'<rect x="{x + 3}" y="{y + 3}" width="{CW - 6}" height="{RH - 6}" rx="4" fill="#f1f5f9"/>')
                continue
            if not c:
                o.append(f'<rect x="{x + 3}" y="{y + 3}" width="{CW - 6}" height="{RH - 6}" rx="4" fill="#fff" stroke="#e2e8f0"/>')
                continue
            ok, rv, bad = sum(c[k] for k in OK_CLASSES), sum(c[k] for k in REVIEW), sum(c[k] for k in BAD)
            col = "#dc2626" if bad else ("#d97706" if rv else "#16a34a")
            o.append(f'<rect x="{x + 3}" y="{y + 3}" width="{CW - 6}" height="{RH - 6}" rx="4" fill="{col}" fill-opacity=".1" stroke="{col}" stroke-width="1.6"/>')
            for k, (lab, v, cc) in enumerate((("同期化あり", ok, "#16a34a"), ("要確認", rv, "#b45309"), ("要対策", bad, "#dc2626"))):
                o.append(f'<text x="{x + 12}" y="{y + 20 + k * 15}" font-size="10.5" fill="{cc if v else "#cbd5e1"}" '
                         f'font-weight="{800 if v else 400}">{lab} {v}</text>')
    yb = Y0 + 40 + len(doms) * RH + 26
    o.append(f'<text x="{X0}" y="{yb}" font-size="11" fill="#64748b">同期化あり = 論理なしで受けて 2 段以上の FF(1 ビット・グレイコード)/ '
             f'要確認 = 多ビットの同期化・受け側の条件で取り込むデータ・別クロックで書くメモリ</text>'
             f'<text x="{X0}" y="{yb + 17}" font-size="11" fill="#64748b">要対策 = 同期化なし・同期化の前に論理・1 段目の出力を使用。'
             f'信号単位の解析(ビットごとではない)。詳細は cdc.md</text></svg>')
    p = os.path.join(out, "cdc_map.svg")
    open(p, "w", encoding="utf-8").write("".join(o))
    if png:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from chipdiag import to_png
        to_png(p)
    return p

def iface_yaml(db, inst):
    """ブロック間の接続図(ifdiag.py)用: inst を中心に、接続元(左)・接続先(右)"""
    r = db.execute("SELECT id,name,module,parent FROM instance WHERE path=?", (inst,)).fetchone()
    if not r:
        sys.exit(f"インスタンス {inst} がない")
    cid, cname, cmod, par = r
    sig = {x[0]: x for x in db.execute("SELECT id,name,inst,width,kind,dir FROM signal")}
    iname = {x[0]: (x[1], x[2]) for x in db.execute("SELECT id,name,module FROM instance")}
    fin, fout = collections.defaultdict(list), collections.defaultdict(list)
    for d, s in db.execute("SELECT dst,src FROM edge WHERE kind IN ('conn','comb')"):
        fin[d].append(s); fout[s].append(d)
    ffset = {x[0] for x in db.execute("SELECT sig FROM ff")}
    def trace(p, adj):
        """上位の階層の配線だけをたどり、兄弟ブロックの端子・上位の端子・上位の論理に行き着く"""
        res, seen, st = set(), set(), list(adj[p])
        while st:
            x = st.pop()
            if x in seen:
                continue
            seen.add(x)
            _, nm, ins, w, kind, d = sig[x]
            if ins == par and kind == "port":
                res.add(("(上位)", iname[par][0], nm, x))
            elif ins != par and ins != cid:
                res.add((iname[ins][1], iname[ins][0], nm, x))
            elif ins == par:
                nx = adj[x]
                if x in ffset:
                    res.add(("(上位の論理)", iname[par][0], nm, x))      # 上位の階層にある FF
                elif not nx:
                    res.add(("(未接続/定数)", "-", nm, x))
                else:
                    st += nx
        return res
    center = {"module": cmod, "instance": cname, "ports": []}
    peers = {"left": collections.OrderedDict(), "right": collections.OrderedDict()}
    nets = []
    for sidx, nm, ins, w, kind, d in sorted((s for s in sig.values() if s[2] == cid and s[4] == "port"), key=lambda s: s[0]):
        p = {"name": nm, "dir": d}
        if w > 1:
            p["width"] = w
        if d == "in" and not fin[sidx]:
            p["tie"] = "定数"                             # 駆動が信号でない(定数をつないでいる)
        center["ports"].append(p)
        for side, adj, pd in (("left", fin, "out"), ("right", fout, "in")):
            if (d == "in" and side == "right") or (d == "out" and side == "left"):
                continue
            for mod, iname_, pn, x in sorted(trace(sidx, adj)):
                if iname_ == "-":
                    continue
                key = (mod, iname_)
                pe = peers[side].setdefault(key, {"module": mod, "instance": iname_, "ports": []})
                if not any(q["name"] == pn for q in pe["ports"]):
                    q = {"name": pn, "dir": pd}
                    if sig[x][3] > 1:
                        q["width"] = sig[x][3]
                    pe["ports"].append(q)
                nets.append({"from": f"{iname_}.{pn}", "to": f"{cname}.{nm}"} if side == "left" else
                            {"from": f"{cname}.{nm}", "to": f"{iname_}.{pn}"})
    return {"interface": {"title": f"{cname}({cmod})の接続 — RTL の接続データベースから", "center": center,
                          "left": list(peers["left"].values()), "right": list(peers["right"].values()), "nets": nets}}

def show_path(db, src_re, dst_re):
    """受け側の FF から組み合わせの辺を逆にたどり、送り側の FF までの経路を 1 本出す"""
    ps, pd = re.compile(src_re), re.compile(dst_re)
    ff = {r[0] for r in db.execute("SELECT sig FROM ff")}
    cand = [(s, d) for s, d, sp, dp in db.execute("""SELECT c.src, c.dst, s.path, d.path FROM cdc c JOIN signal s ON s.id=c.src
                                                     JOIN signal d ON d.id=c.dst""") if ps.search(sp) and pd.search(dp)]
    if not cand:
        print("当てはまる乗り換えがありません")
        return 1
    sid, did = cand[0]
    prev, q = {did: None}, collections.deque([did])
    while q:
        x = q.popleft()
        if x == sid:
            break
        for s_, k, di, ln in db.execute("SELECT src,kind,direct,line FROM edge WHERE dst=? AND kind!='arst'", (x,)):
            if s_ not in prev and (s_ not in ff or s_ == sid):
                prev[s_] = (x, k, di, ln); q.append(s_)
    if sid not in prev:
        print("経路が見つかりません")
        return 1
    x, n = sid, 0
    print("| # | 信号(階層パス) | 種類 | 次への辺 | 行 |\n|---|---|---|---|---|")
    while x is not None:
        pth, kind, f_ = db.execute("SELECT path,kind,file FROM signal WHERE id=?", (x,)).fetchone()
        e = prev[x]
        print(f"| {n} | `{pth}` | {'FF' if x in ff else kind} | " + (f"{e[1]}{'' if e[2] else '(論理)'} | {f_}:{e[3]}" if e else " | ") + " |")
        x, n = (e[0] if e else None), n + 1
    return 0


def selftest(exp_path):
    """cdc_expected.yaml: 受け側 FF(正規表現)→ 分類。ほかの乗り換えが出たら失敗、出てはいけない所に出ても失敗"""
    import yaml, tempfile
    base = os.path.dirname(os.path.abspath(exp_path))
    bad = 0
    for t in yaml.safe_load(open(exp_path, encoding="utf-8")):
        with tempfile.TemporaryDirectory() as td:
            bd = Builder()
            bd.build(bd.compile([os.path.join(base, f) for f in t["files"]], t["top"], [], [], ""))
            db = bd.save(os.path.join(td, "t.db"), {"top": t["top"], "files": "1", "errors": str(len(bd.errors))})
            an = Analyzer(db)
            an.save(an.cdc(t.get("static", []), t.get("async_in", [])))
            got = collections.defaultdict(set)
            for dp, cls in db.execute("SELECT d.path, c.class FROM cdc c JOIN signal d ON d.id=c.dst"):
                got[dp].add(cls)
            used, ng = set(), []
            for pat, cls in t.get("expect", {}).items():
                hit = [p for p in got if re.search(pat, p)]
                used |= set(hit)
                if not hit:
                    ng.append(f"出ていない: {pat}(期待 {cls})")
                for p in hit:
                    if got[p] != {cls}:
                        ng.append(f"分類が違う: {p} = {sorted(got[p])}(期待 {cls})")
            for pat in t.get("none", []):
                for p in got:
                    if re.search(pat, p):
                        used.add(p)
                        ng.append(f"出てはいけない: {p} = {sorted(got[p])}")
            for p in sorted(set(got) - used):
                ng.append(f"期待にない乗り換え: {p} = {sorted(got[p])}")
            rgot = {(short(sp), short(kn), cls): n for sp, kn, cls, n in db.execute(
                "SELECT s.path,k.name,r.class,r.n_ff FROM rdc r JOIN signal s ON s.id=r.src JOIN clock k ON k.id=r.dst_clk")}
            for src, clk, cls, n in t.get("rdc", []):
                if rgot.get((src, clk, cls)) != n:
                    ng.append(f"RDC が違う: {src} → {clk} {cls} = {rgot.get((src, clk, cls))}(期待 {n})")
            dep = db.execute("SELECT max(depth) FROM instance").fetchone()[0]
            if t.get("depth") and dep != t["depth"]:
                ng.append(f"階層の深さ {dep}(期待 {t['depth']})")
            print(f"{'OK ' if not ng else 'NG '} {t['name']}: 期待 {len(t.get('expect', {}))} 件 / 出力 {len(got)} 件 / 階層の深さ {dep}"
                  + (f" / slang エラー {len(bd.errors)}" if bd.errors else ""))
            for x in ng:
                print("    " + x)
            bad += len(ng) + len(bd.errors)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("files", nargs="*"); b.add_argument("-o", "--out", default="design.db"); b.add_argument("--top")
    b.add_argument("-I", action="append", default=[]); b.add_argument("-D", action="append", default=[])
    b.add_argument("-f", action="append", default=[]); b.add_argument("--slang", default="")
    a = sub.add_parser("analyze")
    a.add_argument("db"); a.add_argument("-o", "--out", default="out"); a.add_argument("--png", action="store_true")
    a.add_argument("--static", action="append", default=[], help="準静的な送り側(階層パスの正規表現)。動作中に変えない設定レジスタ")
    a.add_argument("--async-in", action="append", default=[], help="非同期の入力端子(階層パスの正規表現)。それぞれ別のドメインとして扱う")
    i = sub.add_parser("iface")
    i.add_argument("db"); i.add_argument("--inst", required=True); i.add_argument("-o", "--out", default="iface.yaml")
    s = sub.add_parser("stats")
    s.add_argument("db")
    w = sub.add_parser("path", help="送り側から受け側まで、どの信号を通って届くか(階層をまたぐ経路)を出す")
    w.add_argument("db"); w.add_argument("--src", required=True); w.add_argument("--dst", required=True)
    t = sub.add_parser("selftest", help="答えのわかっている RTL(examples/rtl/cdc_expected.yaml)で分類を確かめる")
    t.add_argument("expected", nargs="?", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "rtl",
                                                                "cdc_expected.yaml"))
    g = ap.parse_args()
    if g.cmd == "selftest":
        sys.exit(selftest(g.expected))
    if g.cmd == "path":
        sys.exit(show_path(sqlite3.connect(g.db), g.src, g.dst))
    if g.cmd == "build":
        files = list(g.files)
        for fl in g.f:
            base = os.path.dirname(fl)
            for ln in open(fl):
                ln = ln.split("//")[0].split("#")[0].strip()
                if ln and not ln.startswith(("+", "-")):
                    files.append(ln if os.path.isabs(ln) or os.path.exists(ln) else os.path.join(base, ln))
        t0 = time.time()
        bd = Builder()
        tops = bd.compile(files, g.top, g.I, g.D, g.slang)
        t1 = time.time()
        bd.build(tops)
        db = bd.save(g.out, {"top": ", ".join(t.name for t in tops), "files": str(len(files)),
                             "errors": str(len(bd.errors))})
        print(f"生成: {g.out}  インスタンス {len(bd.insts)} / 信号 {len(bd.sigs)} / 辺 {len(set(bd.edges))} / FF {len(bd.ffs)}"
              f"  (エラボレート {t1 - t0:.1f}s + 抽出 {time.time() - t1:.1f}s)")
        if bd.errors:
            print(f"  slang のエラー {len(bd.errors)} 件(解析は続行。未定義のモジュールはブラックボックス)")
            eng = bd.ps.DiagnosticEngine(bd.sm)
            for x in bd.errors[:5]:
                try:
                    f_, l_ = bd.loc(x.location)
                    print(f"    {f_}:{l_}: {eng.formatMessage(x)}")
                except Exception:
                    pass
        bb = db.execute("SELECT module, count(*) FROM instance WHERE blackbox=1 GROUP BY 1").fetchall()
        if bb:
            print("  ブラックボックス: " + ", ".join(f"{m}×{n}" for m, n in bb))
        for k, v in bd.nwarn.items():
            print(f"  [info] {k}: {v}")
    elif g.cmd == "analyze":
        db = sqlite3.connect(g.db)
        t0 = time.time()
        an = Analyzer(db)
        rows = an.cdc(g.static, g.async_in)
        an.save(rows)
        agg, cn = report(db, g.out)
        p = cdc_map(db, g.out, agg, cn, g.png)
        import cdcdiag
        cdcdiag.hier(db, g.out, png=g.png)
        print(f"解析 {time.time() - t0:.1f}s → {g.out}/cdc.md, {os.path.basename(p)}, cdc_hier.svg")
        for i, name, kind, nff in db.execute("SELECT id,name,kind,n_ff FROM clock ORDER BY n_ff DESC LIMIT 12"):
            print(f"  ドメイン {short(name)}: {kind}, FF {nff}")
        for cls, n in db.execute("SELECT class, count(DISTINCT dst) FROM cdc GROUP BY 1 ORDER BY 2 DESC"):
            print(f"  {CLASS_JA[cls]}: {n}")
    elif g.cmd == "iface":
        import yaml
        db = sqlite3.connect(g.db)
        y = iface_yaml(db, g.inst)
        open(g.out, "w", encoding="utf-8").write(yaml.safe_dump(y, allow_unicode=True, sort_keys=False))
        print(f"生成: {g.out}(左 {len(y['interface']['left'])} / 右 {len(y['interface']['right'])} ブロック)")
    elif g.cmd == "stats":
        db = sqlite3.connect(g.db)
        for k in ("instance", "signal", "edge", "ff", "clock", "cdc"):
            print(k, db.execute(f"SELECT count(*) FROM {k}").fetchone()[0])

if __name__ == "__main__":
    main()
