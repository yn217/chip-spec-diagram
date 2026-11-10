#!/usr/bin/env python3
"""rtldb — RTL の接続をデータベース(SQLite)にし、クロックドメインと非同期の乗り換え(CDC)を検出する
  python3 rtldb.py build   -o design.db --top TOP [-I incdir] [-D NAME=VAL] [-f files.f] a.v b.sv ...
  python3 rtldb.py analyze design.db [-o out/]          # クロックの源・ドメイン・CDC を求めて DB に書く。cdc.md / cdc_map.svg
  python3 rtldb.py iface   design.db --inst top.u_x -o iface.yaml   # ブロック間の接続図(ifdiag.py)用の YAML
  python3 rtldb.py stats   design.db
方式(詳しくは reference/rtldb.md)
  1. slang(pyslang)でエラボレートする: パラメータ・generate・マクロ・インスタンス配列を展開した後の階層を得る。
  2. 階層を歩き、信号(端子・ネット・変数)を「階層パス」で一意な節点にし、駆動の関係を辺にする(信号単位、ビット単位ではない)。
       comb: assign / always @* / ゲート       seq: クロックで叩く代入(dst は FF)     conn: インスタンスの端子接続
       arst: 非同期リセット                     role: data(右辺)/ ctrl(if・case の条件)  direct: 論理を通らない(名前だけの式)
  3. クロック: FF のクロック端子から駆動を逆にたどり、源(トップ端子・ブラックボックスの出力・FF の出力=分周・
     複数クロックの合流=切替)を求める。ゲートされたクロックは元のドメインに含める。分周は元と同じ「系」(同期)とする。
  4. CDC: FF ごとに D 入力の論理の円錐を FF まで逆にたどり、系の違う FF があれば乗り換え。受け側の形で分類する。
       sync / sync_bus / sync_gray  … 論理なしで受け、同じドメインの次の FF へ直結(2 段以上)。多ビットは要確認
       sync_logic … 同期化の前に組み合わせ論理(グリッチ)   sync_tap … 1 段目の出力を論理でも使っている
       data_enable … 受け側ドメインの条件で取り込むデータ(イネーブル同期・ハンドシェイクか要確認)
       mem … 別クロックで書くメモリ(非同期 FIFO の RAM)       no_sync … 同期化なし
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
                 class TEXT, stages INTEGER, logic INTEGER, width INTEGER);
CREATE INDEX e_dst ON edge(dst); CREATE INDEX e_src ON edge(src); CREATE INDEX s_inst ON signal(inst);
CREATE VIEW conn AS   -- 兄弟ブロック間・上位との接続(図を描く時の入口)
  SELECT e.id, si.path AS src_inst, s.name AS src_port, di.path AS dst_inst, d.name AS dst_port, d.width
  FROM edge e JOIN signal s ON s.id=e.src JOIN signal d ON d.id=e.dst
  JOIN instance si ON si.id=s.inst JOIN instance di ON di.id=d.inst WHERE e.kind='conn';
"""
OK_CLASSES = ("sync", "sync_gray")
INFO = ("static",)
REVIEW = ("sync_bus", "data_enable", "mem")
BAD = ("no_sync", "sync_logic", "sync_tap")
CLASS_JA = {"sync": "同期化あり(2 段以上)", "sync_gray": "同期化あり(グレイコード)", "sync_bus": "多ビットを FF で同期化(要確認)",
            "data_enable": "受け側の条件で取り込むデータ(要確認)", "mem": "別クロックで書くメモリ(要確認)",
            "static": "準静的と宣言(設定レジスタ)", "sync_logic": "同期化の前に論理(グリッチ)", "sync_tap": "1 段目の出力を論理で使用", "no_sync": "同期化なし"}

# ================= 1. RTL → DB =================
class Builder:
    def __init__(self):
        import pyslang as ps
        self.ps, self.A = ps, ps.ast
        self.insts, self.sig, self.sigs, self.edges, self.ffs = [], {}, [], [], {}
        self.bbconn = []
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
    def sid(self, sym, inst=None):
        p = sym.hierarchicalPath
        i = self.sig.get(p)
        if i is None:
            t = sym.type
            try:
                mem = bool(t.isUnpackedArray)
                w = int(t.elementType.bitWidth if mem else t.bitWidth)
            except Exception:
                mem, w = False, 1
            f, ln = self.loc(sym.location)
            i = len(self.sigs)
            self.sig[p] = i
            self.sigs.append(dict(path=p, name=sym.name, inst=inst if inst is not None else self.cur, width=max(w, 1),
                                  kind="net" if sym.kind == self.A.SymbolKind.Net else "var", dir=None, is_top=0,
                                  is_mem=int(mem), file=f, line=ln))
        return i
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
        if k in (K.NamedValue, K.HierarchicalValue):
            s = e.symbol
            if s.kind in (self.A.SymbolKind.Net, self.A.SymbolKind.Variable):
                if s.kind == self.A.SymbolKind.Variable and str(s.lifetime).endswith("Automatic"):
                    return out
                out.append(self.sid(s, self.owner(s)))
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
        if k in (K.NamedValue, K.HierarchicalValue):
            self.refs(e, tg)
        elif k == K.ElementSelect:
            self.lhs(e.value, tg, deps); self.refs(e.selector, deps)
        elif k == K.RangeSelect:
            self.lhs(e.value, tg, deps); self.refs(e.left, deps); self.refs(e.right, deps)
        elif k == K.MemberAccess:
            self.lhs(e.value, tg, deps)
        elif k == K.Concatenation:
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
        tg, deps = [], []
        self.lhs(e.left, tg, deps)
        rs = self.refs(e.right, [])
        d = self.plain(e.right) and not deps
        ln = self.line(e)
        for t in set(tg):
            for r in set(rs):
                self.edge(t, r, kind, "data", d, ln)
            cd = {}
            for r, pl in conds:
                cd[r] = cd.get(r, True) and pl
            for r in set(deps):
                cd[r] = False
            for r, pl in cd.items():
                if r not in arst:
                    self.edge(t, r, kind, "ctrl", pl, ln)
            if kind == "seq":
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
                    t = self.sid(m)
                    ini = m.initializer if k == SK.Net else None      # wire x = 式;
                    if ini is not None:
                        d = self.plain(ini)
                        for r in set(self.refs(ini, [])):
                            self.edge(t, r, "comb", "data", d, self.line(ini))
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
        names = list(m.getPortNames())
        for j, ex in enumerate(m.getPortConnections()):
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
            s = self.sid(p.internalSymbol, iid)
            d = str(p.direction).split(".")[-1]
            d = {"In": "in", "Out": "out", "InOut": "inout"}.get(d, "inout")
            self.sigs[s].update(kind="port", dir=d, is_top=int(par is None))
            if pc.expression is not None:
                conns.append((s, d, pc.expression))
        if par is None:
            for p in inst.body.portList:
                if p.kind == A.SymbolKind.Port and p.internalSymbol is not None:
                    s = self.sid(p.internalSymbol, iid)
                    d = {"In": "in", "Out": "out"}.get(str(p.direction).split(".")[-1], "inout")
                    self.sigs[s].update(kind="port", dir=d, is_top=1)
        self.cur = par if par is not None else iid
        for s, d, ex in conns:
            e = ex.left if ex.kind == A.ExpressionKind.Assignment else ex
            ln2 = self.line(e)
            if d in ("in", "inout"):
                for r in set(self.refs(e, [])):
                    self.edge(s, r, "conn", "data", self.plain(e), ln2)
            if d in ("out", "inout"):
                tg, dp = [], []
                self.lhs(e, tg, dp)
                for t in set(tg):
                    self.edge(t, s, "conn", "data", 1, ln2)
        self.cur = iid
        self.scope(inst.body)
        self.cur = par
    def build(self, tops):
        self.ipath, self.cur = {}, None
        for t in tops:
            self.instance(t)
        driven = {e[0] for e in self.edges}
        for pin, r, d in self.bbconn:                     # ブラックボックス: ほかに駆動のない信号は出力とみなす
            if r in driven:
                self.edges.append((pin, r, "conn", "data", d, self.sigs[pin]["inst"], 0))
            else:
                self.edges.append((r, pin, "conn", "data", 1, self.sigs[pin]["inst"], 0))
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
                ck = {r for r in rs if r[1] in ("primary", "blackbox", "mux", "undriven") and clocky(r[0])}
                if len(ck) == 1 and all(r in ck or r[1] in ("generated", "primary", "undriven", "loop") for r in rs):
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
    def cdc(self, static=()):
        self.domains()
        pats = [re.compile(p) for p in static]
        stat = {f for f in self.ffclk if any(p.search(self.S[f]["path"]) for p in pats)}
        memo = {}
        self.inp = {f: self.ff_inputs(f, memo) for f in self.ffclk}
        wmem = {s for s in self.ffclk if self.S[s]["is_mem"]}
        rows = []
        for f in self.ffclk:
            data, ctrl = self.inp[f]
            fm = self.fam[self.dom[f]]
            xs_d = {t: lg for t, lg in data.items() if t in self.dom and self.fam[self.dom[t]] != fm}
            xs_c = {t: lg for t, lg in ctrl.items() if t in self.dom and self.fam[self.dom[t]] != fm}
            if not xs_d and not xs_c:
                continue
            w = self.S[f]["width"]
            st_src = {t for t in list(xs_d) + list(xs_c) if t in stat}
            if st_src:                                    # 準静的(動作中に変えない設定)と宣言された送り側は除く
                for t in st_src:
                    rows.append((t, f, self.dom[t], self.dom[f], "static", 1, 0, w))
                xs_d = {t: v for t, v in xs_d.items() if t not in stat}
                xs_c = {t: v for t, v in xs_c.items() if t not in stat}
                data = {t: v for t, v in data.items() if t not in stat}
                ctrl = {t: v for t, v in ctrl.items() if t not in stat}
                if not xs_d and not xs_c:
                    continue
            if not data and len(xs_c) == 1 and not next(iter(xs_c.values())):
                # if (x) q <= 1; else q <= 0; の形: x をそのまま受けるのと同じ
                data, xs_d, xs_c = dict(xs_c), dict(xs_c), {}
                ctrl = {t: v for t, v in ctrl.items() if t not in xs_d}
            nxt, other = self.next_stage(f, memo)
            stages = 1
            if not xs_c and len(data) == 1 and not next(iter(data.values())) and not any(t in wmem for t in xs_d):
                # 1 本を論理なしで受けている
                if nxt:
                    x, stages = nxt[0], 2
                    for _ in range(8):
                        n2, _o = self.next_stage(x, memo)
                        if not n2:
                            break
                        x, stages = n2[0], stages + 1
                    if other:
                        cls = "sync_tap"
                    elif w == 1:
                        cls = "sync"
                    elif re.search(r"gr[ae]?y", self.S[f]["name"] + self.S[next(iter(xs_d))]["name"], re.I):
                        cls = "sync_gray"
                    else:
                        cls = "sync_bus"
                elif any(t in self.dom and self.fam[self.dom[t]] == fm for t in ctrl):
                    cls = "data_enable"                   # 受け側ドメインの条件(イネーブル)で取り込む
                else:
                    cls = "no_sync"
            elif any(t in wmem for t in xs_d) and not xs_c:
                cls = "mem"
            elif not xs_c and nxt and not other:
                cls = "sync_logic"
            elif not xs_c and any(t in self.dom and self.fam[self.dom[t]] == fm for t in ctrl):
                cls = "data_enable"
            else:
                cls = "no_sync"
            for t, lg in {**xs_c, **xs_d}.items():
                rows.append((t, f, self.dom[t], self.dom[f], cls, stages, int(lg), w))
        return rows
    def save(self, rows):
        db = self.db
        db.execute("DELETE FROM clock"); db.execute("DELETE FROM cdc")
        nff = collections.Counter(self.dom.values())
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
        db.executemany("UPDATE ff SET domain=? WHERE sig=?", [(d, f) for f, d in self.dom.items()])
        db.executemany("INSERT INTO cdc(src,dst,src_clk,dst_clk,class,stages,logic,width) VALUES(?,?,?,?,?,?,?,?)", rows)
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
          "|---|" + "---|" * 8]
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
                if not nx:
                    res.add(("(未接続/定数)", "-", nm, x))
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
    i = sub.add_parser("iface")
    i.add_argument("db"); i.add_argument("--inst", required=True); i.add_argument("-o", "--out", default="iface.yaml")
    s = sub.add_parser("stats")
    s.add_argument("db")
    g = ap.parse_args()
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
        rows = an.cdc(g.static)
        an.save(rows)
        agg, cn = report(db, g.out)
        p = cdc_map(db, g.out, agg, cn, g.png)
        print(f"解析 {time.time() - t0:.1f}s → {g.out}/cdc.md, {os.path.basename(p)}")
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
