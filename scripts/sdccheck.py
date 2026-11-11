#!/usr/bin/env python3
"""sdccheck — タイミング制約(SDC / XDC)と RTL のクロック・乗り換えを突き合わせる

  python3 rtldb.py analyze design.db -o out/ --sdc chip.sdc [--sdc more.xdc]   # 解析と一緒に → out/sdc_check.md
  python3 sdccheck.py design.db chip.sdc ... -o out/sdc_check.md               # 解析済みの DB に対して

見ること(非同期対策の忘れを、制約の側からも見つける):
  1. RTL のクロックの系ごとに、create_clock / create_generated_clock があるか
  2. 乗り換えのあるクロックの組ごとに、制約で非同期と扱っているか
       非同期の宣言(set_clock_groups -asynchronous、クロック間の set_false_path)がある組で「対策なし・誤り」があると、
       STA はその経路を見ないので、対策忘れがどこにも出てこない → 一番危ない
       宣言も経路ごとの制約もない組は、STA が同期として扱う(誤った違反が出るか、間違った最適化をする)
  3. set_false_path -from [get_ports …] の入力端子(非同期の入力): RTL で同期化しているか
     (analyze --sdc の時は、これらの端子を --async-in と同じく非同期入力として扱う)
読めない行($変数・foreach・proc など)は、そのまま一覧に出す。推測で補わない。
"""
import os, re, sys, fnmatch, argparse, collections, sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------- 読む ----------------
def tcl_lines(text):
    """コメントを除き、行の継続(\\)をつなぎ、; で分ける"""
    text = re.sub(r"\\\n", " ", text)
    out = []
    for n, ln in enumerate(text.split("\n"), 1):
        t = ln.strip()
        if not t or t.startswith("#"):
            continue
        for part in re.split(r";(?![^\[]*\])", t):
            part = part.strip()
            if part and not part.startswith("#"):
                out.append((n, part))
    return out


def objs(arg):
    """[get_ports {a b[*]}] / {a b} / name → (種類, [パターン])"""
    arg = arg.strip()
    m = re.match(r"^\[\s*(get_\w+|all_inputs|all_outputs|all_clocks)\s*(.*?)\]$", arg, re.S)
    if m:
        kind, rest = m.group(1), m.group(2)
        rest = re.sub(r"-(hierarchical|hier|regexp|nocase|quiet|of_objects|filter\s+\S+)\b", "", rest)
        pats = re.findall(r"\{([^}]*)\}|(\S+)", rest)
        pl = []
        for a, b in pats:
            pl += (a.split() if a else [b])
        return kind, [p.strip('"') for p in pl if p]
    if arg.startswith("{"):
        return "name", arg.strip("{}").split()
    return "name", [arg.strip('"')]


def split_args(s):
    """Tcl の引数: [ ] と { } の中は 1 つにまとめる"""
    out, cur, d = [], "", 0
    for ch in s:
        if ch in "[{":
            d += 1
        elif ch in "]}":
            d -= 1
        if ch.isspace() and d == 0:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def parse(files):
    C = dict(clocks=[], groups=[], paths=[], unread=[], other=collections.Counter())
    for f in files:
        for n, ln in tcl_lines(open(f, encoding="utf-8", errors="ignore").read()):
            where = f"{os.path.basename(f)}:{n}"
            if "$" in ln or re.match(r"^(foreach|proc|if|for|while|set\s|source|puts)\b", ln):
                C["unread"].append((where, ln))
                continue
            a = split_args(ln)
            cmd, rest = a[0], a[1:]
            opt, pos = {}, []
            k = 0
            while k < len(rest):
                x = rest[k]
                if x.startswith("-") and not re.match(r"^-?\d", x):
                    if x in ("-asynchronous", "-logically_exclusive", "-physically_exclusive", "-datapath_only", "-add",
                             "-setup", "-hold", "-start", "-end", "-invert", "-combinational"):
                        opt[x] = True
                    elif x == "-group":
                        opt.setdefault("-group", []).append(rest[k + 1]); k += 1
                    elif k + 1 < len(rest):
                        opt[x] = rest[k + 1]; k += 1
                else:
                    pos.append(x)
                k += 1
            if cmd in ("create_clock", "create_generated_clock"):
                tgt = objs(pos[-1]) if pos else ("name", [])
                name = opt.get("-name") or (tgt[1][0] if tgt[1] else "?")
                C["clocks"].append(dict(name=name.strip("{}"), kind=cmd, target=tgt, period=opt.get("-period"),
                                        source=objs(opt["-source"]) if "-source" in opt else None, where=where))
            elif cmd == "set_clock_groups":
                typ = "asynchronous" if "-asynchronous" in opt else "exclusive"
                C["groups"].append(dict(type=typ, groups=[objs(g)[1] for g in opt.get("-group", [])], where=where))
            elif cmd in ("set_false_path", "set_max_delay", "set_min_delay", "set_multicycle_path"):
                C["paths"].append(dict(cmd=cmd, frm=objs(opt["-from"]) if "-from" in opt else None,
                                       to=objs(opt["-to"]) if "-to" in opt else None,
                                       thr=objs(opt["-through"]) if "-through" in opt else None,
                                       dponly="-datapath_only" in opt, value=pos[0] if pos else "", where=where))
            else:
                C["other"][cmd] += 1
    return C


def async_inputs(C):
    """set_false_path -from [get_ports …](-to なし)の入力端子 → 非同期入力の候補"""
    clockports = {p for c in C["clocks"] if c["target"][0] == "get_ports" for p in c["target"][1]}
    out = []
    for p in C["paths"]:
        if p["cmd"] == "set_false_path" and p["frm"] and p["frm"][0] == "get_ports" and not p["to"]:
            out += [x for x in p["frm"][1] if x not in clockports]
    return out


def port_regex(pats, top):
    """端子のパターン(glob)→ 階層パスの正規表現"""
    rs = []
    for x in pats:
        x = re.sub(r"\[[^\]]*\]$", "", x)                # btn[*] / data[3] → btn(ビットの選択は端子全体として扱う)
        g = re.escape(x).replace(r"\*", ".*").replace(r"\?", ".")
        rs.append(rf"^{re.escape(top)}\.{g}(\[[^\]]*\])?$")
    return rs


# ---------------- 突き合わせ ----------------
def check(db, C, out):
    top = db.execute("SELECT value FROM meta WHERE key='top'").fetchone()[0].split(",")[0].strip()
    S = {}
    for i, p_, is_top, kind in db.execute("SELECT id,path,is_top,kind FROM signal"):
        S[i] = (p_, is_top, kind)
    rel = lambda p_: p_[len(top) + 1:] if p_.startswith(top + ".") else p_
    clk = {r[0]: dict(id=r[0], root=r[1], name=rel(r[2]), kind=r[3], fam=r[4], parent=r[5], nff=r[6])
           for r in db.execute("SELECT id,root,name,kind,family,parent,n_ff FROM clock")}
    fams = collections.defaultdict(list)
    for c in clk.values():
        fams[c["fam"]].append(c)
    famname = lambda f: rel(S[f][0]) if f in S else "?"
    # SDC のクロック → RTL の系
    def sigs_of(kind, pats):
        hit = set()
        for p_ in pats:
            if kind == "get_ports":
                rx = re.compile(port_regex([p_], top)[0])
                hit |= {i for i, (path, it, k) in S.items() if it and rx.match(path)}
            elif kind in ("get_pins", "get_nets", "get_cells"):
                g = p_.replace("/", ".")
                hit |= {i for i, (path, it, k) in S.items() if fnmatch.fnmatchcase(rel(path), g)}
        return hit
    # 信号 → それを源・途中に持つ系(クロックの源・途中のバッファ・PLL の出力)
    upto = {}
    for c in clk.values():
        for x in [c["root"]] + [int(v) for v in re.findall(r"\d+", "")]:
            upto.setdefault(x, set()).add(c["fam"])
    for f in fams:
        upto.setdefault(f, set()).add(f)
    for c in clk.values():                                # 元(バッファ・PLL の入力)もたどる。切替(mux)の元はたどらない(別に定義が要る)
        if c["kind"] != "blackbox":
            continue
        for pname in [x for x in (c["parent"] or "").split(", ") if x]:
            r = db.execute("SELECT id FROM signal WHERE path=?", (pname,)).fetchone()
            if r:
                upto.setdefault(r[0], set()).add(c["fam"])
    sdc_fam = {}                                          # SDC のクロック名 → RTL の系
    for c in C["clocks"]:
        hit = sigs_of(*c["target"]) if c["target"][0] != "name" else set()
        fs = set()
        for h in hit:
            fs |= upto.get(h, set())
        c["fams"] = fs
        c["hit"] = len(hit)
        sdc_fam[c["name"]] = fs
    def clocks_of(o):
        if not o:
            return None
        if o[0] in ("get_clocks", "name", "all_clocks"):
            names = [n for n in sdc_fam if any(fnmatch.fnmatchcase(n, p_) for p_ in o[1])] if o[0] != "all_clocks" else list(sdc_fam)
            return set().union(*[sdc_fam[n] for n in names]) if names else set()
        return None
    # 非同期の宣言: 系の組 → 宣言の場所
    asyn = {}
    for g in C["groups"]:
        gs = []
        for grp in g["groups"]:
            names = [n for n in sdc_fam if any(fnmatch.fnmatchcase(n, p_) for p_ in grp)]
            gs.append(set().union(*[sdc_fam[n] for n in names]) if names else set())
        for i, a in enumerate(gs):
            for j, b in enumerate(gs):
                if i != j:
                    for x in a:
                        for y in b:
                            asyn[(x, y)] = f'set_clock_groups -{g["type"]}({g["where"]})'
    for p in C["paths"]:
        fa, fb = clocks_of(p["frm"]), clocks_of(p["to"])
        if fa and fb and p["cmd"] in ("set_false_path", "set_max_delay"):
            for x in fa:
                for y in fb:
                    if x != y:
                        asyn.setdefault((x, y), f'{p["cmd"]} クロック間({p["where"]})')
    # 経路ごとの制約(セル → セル)
    def cell_rx(o):
        if not o or o[0] not in ("get_cells", "get_pins"):
            return None
        rs = []
        for p_ in o[1]:
            q = re.sub(r"/(Q|D|C|CK|CLK|CE|R|S)$", "", p_)
            q = q.replace("/", ".")
            q = re.sub(r"_reg(\[[^\]]*\])?(\*)?$", r"\1", q)
            g = re.escape(q).replace(r"\*", ".*").replace(r"\?", ".")
            rs.append(re.compile(rf"(^|\.){g}(\[[^\]]*\])?$"))
        return rs
    pc = [(cell_rx(p["frm"]), cell_rx(p["to"]), p) for p in C["paths"] if cell_rx(p["frm"]) or cell_rx(p["to"])]
    # 乗り換え(受け側の FF 単位)
    from rtldb import OK_CLASSES, REVIEW, NONE, WRONG, CLASS_JA
    pair = collections.defaultdict(lambda: dict(n=0, cls=collections.Counter(), covered=0, ex=None, bad_ex=[]))
    for sc, dc, cls, sp, dp in db.execute("""SELECT c.src_clk, c.dst_clk, c.class, s.path, d.path FROM cdc c
                                             JOIN signal s ON s.id=c.src JOIN signal d ON d.id=c.dst"""):
        if sc not in clk or dc not in clk:
            continue
        k = (clk[sc]["fam"], clk[dc]["fam"])
        if k[0] == k[1]:
            continue
        e = pair[k]
        e["n"] += 1
        e["cls"][cls] += 1
        e["ex"] = e["ex"] or (rel(sp), rel(dp))
        if any((not a or any(r.search(rel(sp)) for r in a)) and (not b or any(r.search(rel(dp)) for r in b)) for a, b, p in pc):
            e["covered"] += 1
        if cls in NONE + WRONG and len(e["bad_ex"]) < 3:
            e["bad_ex"].append((rel(sp), rel(dp), cls))
    # ---- 書く ----
    L = [f"# タイミング制約と RTL の突き合わせ: {top}", "",
         f"制約: クロック {len(C['clocks'])}・クロックグループ {len(C['groups'])}・経路の制約 {len(C['paths'])}・読めない行 {len(C['unread'])}", ""]
    L += ["## 1. クロックの定義", "", "| RTL のクロックの系 | FF | 種類 | 制約のクロック |", "|---|---|---|---|"]
    nodef = 0
    for f, cs in sorted(fams.items(), key=lambda kv: -sum(c["nff"] for c in kv[1])):
        nff = sum(c["nff"] for c in cs)
        if not nff:
            continue
        names = [c["name"] for c in C["clocks"] if f in c["fams"]]
        if not names:
            nodef += 1
        kinds = ",".join(sorted({c["kind"] for c in cs}))
        L.append(f"| `{famname(f)}` | {nff} | {kinds} | " + (", ".join(f"`{n}`" for n in names) if names else "**定義なし**") + " |")
    nomatch = [c for c in C["clocks"] if not c["fams"]]
    if nomatch:
        L += ["", "RTL のクロックにつながらなかった制約のクロック(端子名・ピン名の違い、または使っていないクロック):", ""]
        L += [f"- `{c['name']}`({c['kind']}、{c['where']}、対象 {' '.join(c['target'][1])})" for c in nomatch]
    L += ["", "## 2. 乗り換えのあるクロックの組", "",
          "| 送り側の系 → 受け側の系 | 乗り換え | うち対策なし・誤り | 制約 | 判定 |", "|---|---|---|---|---|"]
    danger = []
    ain = {c["fam"] for c in clk.values() if c["kind"] == "async_in"}
    for (a, b), e in sorted(pair.items(), key=lambda kv: -kv[1]["n"]):
        if a in ain:
            continue                                      # 非同期の入力端子からの乗り換えは 3. に書く
        bad = sum(v for c_, v in e["cls"].items() if c_ in NONE + WRONG)
        dec = asyn.get((a, b))
        if dec:
            st = dec
            judge = "**危ない: 非同期と宣言され、STA が見ない所に対策なし・誤りがある**" if bad else "宣言どおり(対策あり)"
            if bad:
                danger.append((a, b, e, dec))
        elif e["covered"]:
            st = f"経路ごとの制約 {e['covered']}/{e['n']}"
            judge = "一部に制約なし(STA は同期として扱う)" if e["covered"] < e["n"] else "経路ごとに制約あり"
        else:
            st = "なし"
            judge = "**制約なし: STA は同期として扱う(誤った違反か、間違った最適化)**"
        L.append(f"| `{famname(a)}` → `{famname(b)}` | {e['n']} | {bad} | {st} | {judge} |")
    if danger:
        L += ["", "### 非同期と宣言された組の、対策なし・誤り(STA では見つからない)", ""]
        for a, b, e, dec in danger:
            for sp, dp, cls in e["bad_ex"]:
                L.append(f"- `{famname(a)}` → `{famname(b)}`: `{sp}` → `{dp}` — {CLASS_JA[cls]}")
    ai = async_inputs(C)
    if ai:
        L += ["", "## 3. set_false_path -from の入力端子(非同期の入力)", "",
              "analyze --sdc の時は、これらを非同期入力として扱い、RTL で同期化しているかを見る。", "",
              "| 端子 | RTL での受け方 |", "|---|---|"]
        for p_ in ai:
            rx = re.compile(port_regex([p_], top)[0])
            got = collections.Counter()
            for (sp, cls) in db.execute("""SELECT s.path, c.class FROM cdc c JOIN signal s ON s.id=c.src JOIN clock k ON k.id=c.src_clk
                                           WHERE k.kind='async_in'"""):
                if rx.match(sp):
                    got[cls] += 1
            if got:
                txt = "・".join(f"{CLASS_JA[c_]} {v}" for c_, v in got.most_common())
            else:
                from rtldb import RDC_JA
                rd = collections.Counter()
                for sp, cls, n_ in db.execute("SELECT s.path, r.class, r.n_ff FROM rdc r JOIN signal s ON s.id=r.src"):
                    if rx.match(sp):
                        rd[cls] += n_
                txt = ("リセットとして使用: " + "・".join(f"{RDC_JA[c_]}(FF {v})" for c_, v in rd.most_common())) if rd else \
                      "データとしてもリセットとしても FF に届いていない(ブラックボックスだけ、または未使用)"
            L.append(f"| `{p_}` | {txt} |")
    if C["unread"]:
        L += ["", "## 読めなかった行", "", "変数・ループ・手続きを含む行は読まない(推測で補わない)。中身を展開した制約を渡すと突き合わせられる。", ""]
        L += [f"- `{w}`: `{ln[:140]}`" for w, ln in C["unread"][:40]]
        if len(C["unread"]) > 40:
            L.append(f"- …ほか {len(C['unread']) - 40} 行")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    nconst = sum(1 for (a, b), e in pair.items() if a not in ain and not asyn.get((a, b)) and e["covered"] < e["n"])
    return dict(nodef=nodef, danger=len(danger), noconst=nconst, unread=len(C["unread"]), pairs=len(pair))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db"); ap.add_argument("sdc", nargs="+"); ap.add_argument("-o", "--out", default="sdc_check.md")
    g = ap.parse_args()
    st = check(sqlite3.connect(g.db), parse(g.sdc), g.out)
    print(f"生成: {g.out}(定義のないクロックの系 {st['nodef']} / 制約のない組 {st['noconst']} / "
          f"非同期と宣言して対策なし {st['danger']} / 読めない行 {st['unread']})")


if __name__ == "__main__":
    main()
