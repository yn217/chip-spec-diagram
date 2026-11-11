#!/usr/bin/env python3
"""cdcdiag — 非同期箇所(クロックの乗り換え)の図。rtldb.py の DB から描く

  python3 cdcdiag.py hier   design.db -o out/ [--png] [--focus 階層パス] [--rows 36] [--arcs 28]   # CDC 階層図
  python3 cdcdiag.py detail design.db --dst 正規表現 -o out/cdc_detail.yaml                         # CDC 詳細図(gatediag の YAML)
      → python3 gatediag.py render out/cdc_detail.yaml -o out/ --png

CDC 階層図(cdc_hier.svg)の決まり(R54):
  行 = 階層(乗り換えの端がある枝だけ。子が 1 つだけの中間は 1 行にまとめ、行数が多ければ深い所をたたむ)
  列 = クロックドメイン。マスの数字 = その行にある、そのドメインの FF 数(下の行に出していない下位を含む)
  線 = 乗り換え。送り側のマス(●)から右の通路に出て、受け側のマス(▶)に入る。同じ行どうし・同じドメインどうしはまとめて 1 本
  線の種類 = 赤の太い実線: 対策なし / 赤の一点鎖線: 対策の誤り / 橙の破線: 要確認 / 緑の細い実線: 対策あり。番号は右の一覧と対応(重い順)
"""
import os, sys, re, argparse, collections, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import esc, wlen, INK, to_png
from rtldb import OK_CLASSES, INFO, REVIEW, BAD, NONE, WRONG, CLASS_JA

GRAY = "#64748b"
SEV = {"none": ("#dc2626", "対策なし"), "wrong": ("#b91c1c", "対策の誤り"), "review": ("#d97706", "要確認"), "ok": ("#15803d", "対策あり"),
       "info": ("#64748b", "準静的")}
# 隣り合う線を見分けるための色の違い(同じ重さの中で 3 色)。重さは線の種類と番号の形で示すので、色の違いに意味はない
TONES = {"none": ["#dc2626", "#7f1d1d", "#db2777"], "wrong": ["#dc2626", "#7f1d1d", "#db2777"],
         "review": ["#d97706", "#854d0e", "#ca8a04"], "ok": ["#15803d", "#0f766e", "#65a30d"], "info": ["#64748b", "#334155", "#94a3b8"]}
ORDER = {"none": 0, "wrong": 1, "review": 2, "ok": 3, "info": 4}
STROKE = {"none": 'stroke-width="2.4"', "wrong": 'stroke-width="2" stroke-dasharray="12 3 2 3"',
          "review": 'stroke-width="1.8" stroke-dasharray="7 3"', "ok": 'stroke-width="1.2"', "info": 'stroke-width="1.2" stroke-dasharray="2 3"'}
DOMC = [("#2563eb", "#dbeafe"), ("#7c3aed", "#ede9fe"), ("#0891b2", "#cffafe"), ("#be185d", "#fce7f3"), ("#475569", "#e2e8f0"),
        ("#92400e", "#fef3c7"), ("#4338ca", "#e0e7ff"), ("#0f766e", "#ccfbf1"), ("#a21caf", "#fae8ff"), ("#1e293b", "#f1f5f9")]
CLASS_S = {"sync": "2段以上", "sync_gray": "グレイコード", "sync_pulse": "エッジ検出", "fifo": "非同期FIFO", "handshake": "ハンドシェイク",
           "gated": "ゲートの形", "sync_bus": "多ビット同期", "data_enable": "条件で取り込み", "mem": "メモリ", "reconv": "再収束",
           "clk_as_data": "クロックをデータに", "static": "準静的", "sync_logic": "同期化の前に論理", "sync_tap": "1段目を使用",
           "no_sync": "同期化なし"}


LEG_ITEMS = {"none": "対策なし(同期化が見つからない・条件やポインタが同期化されていない)",
             "wrong": "対策の誤り(グレイコードでない多ビット・同期化の前に論理・1 段目を使用)",
             "review": "要確認(ゲートの形・再収束・クロックをデータに)",
             "ok": "対策あり(2 段以上・グレイコード・エッジ検出・非同期 FIFO・ハンドシェイク)"}
LEG_TEXT = ["読み方: ●(送り側のマス)から右の通路へ出て、▶(受け側のマス)に入る。",
            "線の両端(マスの列のすぐ右)に同じ番号がある。線をたどらなくても、同じ番号を探せば相手の行がわかる(束ねた幹線の根元には番号をまとめて書く)。",
            "線が交わる所は白い縁で上下を分け、つながる所だけに丸を打つ。",
            "隣り合う線は色を変えてある(赤・えんじ・ピンクなど)。色の違いは見分けるためだけで、重さは線の種類(実線・一点鎖線・破線・細線)と番号の形で見る。",
            "マス = その行(と、下の行に出していない下位)にある、その列のドメインの FF 数。太枠のマスが乗り換えの端。",
            "行は乗り換えの端がある枝だけ。", "子が 1 つだけの中間の階層は 1 行にまとめ(…(n 階層)…)、同じモジュールの兄弟 4 つ以上は 1 行(×n)、",
            "行が多い時は深い所をたたむ(▸ 下位 n 段を含む)。", "線の多い行は高くする。",
            "同じ行どうしの乗り換えは重さごとに 1 本にまとめ、送り側か受け側が同じ線は 1 本の幹線に束ねる(分岐・合流の点に丸)。",
            "1 本ずつの回路は CDC 詳細図(cdcdiag.py detail)、全件は cdc.md。", "グレイコードは記述(X ^ (X >> 1))で確かめ、名前は見ない。",
            "判定は構造だけを見たもの(準静的・タイミングは人が確認する)。",
            "SVG をブラウザで開いて番号か一覧にマウスを乗せると、その線だけが残る。"]


def sev_of(cls):
    return "none" if cls in NONE else "wrong" if cls in WRONG else "review" if cls in REVIEW else "ok" if cls in OK_CLASSES else "info"


def cut(s, n):
    """半角 n 文字ぶんに収める(中を … で省く)"""
    s = str(s)
    if wlen(s) <= n:
        return s
    h = max(2, (n - 1) // 2)
    a, b = "", ""
    for c in s:
        if wlen(a + c) > h:
            break
        a += c
    for c in reversed(s):
        if wlen(c + b) > n - 1 - wlen(a):
            break
        b = c + b
    return a + "…" + b


def short(p):
    return p.split(".", 1)[1] if "." in p else p


def tail(p, n=2):
    return ".".join(p.split(".")[-n:])


# ================= CDC 階層図 =================
def hier(db, out, focus=None, max_rows=36, max_arcs=28, png=False, name="cdc_hier", sevs=None, gap=14, lane=34, tones=True, merge=True, style="lines"):
    """gap = 同じ行に出入りする線どうしの間隔(px)、lane = 縦の幹線どうしの間隔(px)。広げると追いやすく、図は大きくなる"""
    top = db.execute("SELECT value FROM meta WHERE key='top'").fetchone()[0]
    I = {r[0]: dict(id=r[0], path=r[1], name=r[2], module=r[3], parent=r[4], depth=r[5])
         for r in db.execute("SELECT id,path,name,module,parent,depth FROM instance")}
    root = min(I, key=lambda i: I[i]["depth"])
    if focus:
        cand = [i for i, x in I.items() if x["path"] == focus or x["path"].endswith("." + focus)]
        if not cand:
            sys.exit(f"--focus {focus}: その階層はありません")
        root = cand[0]

    def anc(i):
        o = []
        while i is not None:
            o.append(i)
            if i == root:
                return o
            i = I[i]["parent"]
        return None                                       # root の外
    rows = db.execute("""SELECT s.inst, d.inst, c.src_clk, c.dst_clk, c.class, c.dst, s.path, d.path
                         FROM cdc c JOIN signal s ON s.id=c.src JOIN signal d ON d.id=c.dst""").fetchall()
    X = []                                                # 乗り換え(root の中で両端が見えるもの。片方が外なら root に寄せる)
    for si, di, sc, dc, cls, dst, sp, dp in rows:
        a, b = anc(si), anc(di)
        if a is None and b is None:
            continue
        X.append((si if a else -1, di if b else -1, sc, dc, cls, dst, sp, dp, a is None, b is None))
    if sevs and not any(sev_of(x[4]) in sevs for x in X):
        sevs = None                                       # その重さの乗り換えがない: 全部の重さで描く
    XR = [x for x in X if sev_of(x[4]) in sevs] if sevs else X      # 行を作る元: 描く重さの乗り換えだけ(--sev で絞った時は行も減る)
    OUT = -1                                              # 絞った階層の外にある端: 一番下の「この階層の外」の行へ
    I[OUT] = dict(id=OUT, path="", name="(この階層の外)", module="", parent=None, depth=I[root]["depth"])
    need = set([root])
    for x in XR:
        for i in (x[0], x[1]):
            if i != OUT:
                need |= set(anc(i))
    ends = {x[0] for x in XR} | {x[1] for x in XR}
    # 行数が多ければ、たたむ。重い乗り換え(対策なし・誤り → 要確認 → 件数)のある枝から順に開く
    wt = collections.defaultdict(lambda: [0, 0, 0])
    for x in XR:
        k = {"none": 0, "wrong": 0, "review": 1}.get(sev_of(x[4]), 2)
        for i in set(anc(x[0]) if x[0] != OUT else []) | set(anc(x[1]) if x[1] != OUT else []):
            wt[i][k] += 1
    kids = collections.defaultdict(list)
    for i in need:
        if i != root:
            kids[I[i]["parent"]].append(i)
    import heapq
    shown = {root}
    heap = [(I[k]["depth"], tuple(-v for v in wt[k]), I[k]["path"], k) for k in kids[root]]
    heapq.heapify(heap)
    while heap:                # 浅い階層から、同じ深さでは重い枝(対策なし・誤り → 要確認 → 件数)から行にする。入りきらない枝は親の行にたたむ
        _, _, _, i = heapq.heappop(heap)
        if len(reduced_rows(I, need, ends, root, shown | {i})) > max_rows:
            break
        shown.add(i)
        for k in kids[i]:
            heapq.heappush(heap, (I[k]["depth"], tuple(-v for v in wt[k]), I[k]["path"], k))
    R4 = [r + ([],) for r in reduced_rows(I, need, ends, root, shown)]   # [(行の代表, まとめた中間, 表示の段, 同じ形の兄弟, そのほか含める階層)]
    if OUT in ends:
        R4.append((OUT, [], 0, [], []))
    # ドメイン(列)
    C = {r[0]: dict(id=r[0], name=short(r[1]), kind=r[2], nff=r[3]) for r in db.execute("SELECT id,name,kind,n_ff FROM clock")}
    usec = collections.Counter()
    for x in X:
        usec[x[2]] += 1; usec[x[3]] += 1
    doms = sorted(usec, key=lambda d: (-C.get(d, {}).get("nff", 0), -usec[d]))[:10]
    col = {d: n for n, d in enumerate(doms)}
    order = ORDER
    inroot = {}

    def under(i):
        if i not in inroot:
            inroot[i] = anc(i) is not None
        return inroot[i]
    ffrows = db.execute("SELECT s.inst, f.domain, count(*) FROM ff f JOIN signal s ON s.id=f.sig GROUP BY 1,2").fetchall()
    for _pass in range(6):
        rowid = {}
        for n, (i, chain, lvl, same, extra) in enumerate(R4):
            for j in [i] + chain + same + extra:
                rowid[j] = n
        R = [r[:3] for r in R4]

        def row_of(i):
            while i not in rowid:
                i = I[i]["parent"]
            return rowid[i]
        hidden = collections.Counter()                    # たたんだ下位の階層の数
        for i in need:
            if i not in rowid or i in R4[rowid[i]][4]:
                hidden[row_of(i)] = max(hidden[row_of(i)], I[i]["depth"] - I[R[row_of(i)][0]]["depth"])
        cell = collections.Counter()                      # マス: FF 数
        for inst, dm, n in ffrows:
            if dm in col and under(inst):
                cell[(row_of(inst), dm)] += n
        # 乗り換えをまとめる: (送り側の行, 受け側の行, 重さ)で 1 本。ドメインの組は、端のマスと一覧で示す
        G = collections.defaultdict(lambda: dict(dst=collections.defaultdict(set), ex={}, pairs=collections.defaultdict(set)))
        for si, di, sc, dc, cls, dst, sp, dp, so, do in X:
            if sc not in col or dc not in col:
                continue
            g = G[(row_of(si), row_of(di))]
            g["dst"][cls].add(dst)
            g["ex"].setdefault(cls, (sp, dp))
            g["pairs"][sev_of(cls)].add((sc, dc))
        arcs = []
        for (rs, rd), g in G.items():
            for sv in order:
                cnt = {c: len(v) for c, v in g["dst"].items() if sev_of(c) == sv}
                if cnt:
                    worst = max(cnt, key=lambda c: cnt[c])
                    arcs.append(dict(rs=rs, rd=rd, pairs=sorted(g["pairs"][sv], key=lambda t: (col[t[0]], col[t[1]])), cnt=cnt, sev=sv,
                                     n=len(set().union(*[v for c, v in g["dst"].items() if sev_of(c) == sv])), ex=g["ex"][worst], worst=worst))
        new = merge_rows(R4, arcs) if merge else R4
        if len(new) == len(R4):
            break
        R4 = new
    if sevs:
        arcs = [a for a in arcs if a["sev"] in sevs]      # 線にする重さを絞る(行と数字はそのまま)
    arcs.sort(key=lambda a: (order[a["sev"]], -a["n"]))
    rest = arcs[max_arcs:]
    arcs = arcs[:max_arcs]
    for n, a in enumerate(arcs):
        a["no"] = n + 1
    # ---- 寸法 ----
    RH, Y0, X0 = 30, 150, 24
    labels, shown = [], {}                                # 行の名前 = 上の行からの相対パス(generate の名前も入る)
    stack = []
    for i, chain, lvl in R:
        stack = stack[:lvl]
        base = I[stack[-1]]["path"] + "." if stack else ""
        rel = lambda j: I[j]["path"][len(base):] if I[j]["path"].startswith(base) else I[j]["name"]
        same = R4[len(labels)][3]
        if i == root or i == OUT:
            lab = I[i]["name"]
        elif same:                                        # 同じモジュールの兄弟をまとめた行: 違う数字を * に
            ns = [re.sub(r"\d+", "*", rel(j)) for j in [i] + same]
            lab = (ns[0] if len(set(ns)) == 1 else os.path.commonprefix([rel(j) for j in [i] + same]) + "*") + f"  ×{len(same) + 1}"
        elif len(chain) >= 2:
            lab = f"{rel(chain[0])} …({len(chain) - 1} 階層)… {I[i]['name']}"
        else:
            lab = rel(i)
        labels.append(cut(lab, 46))
        stack.append(i)
    rp = I[root]["path"] + "."
    full = [I[i]["name"] if i in (root, OUT) else (I[i]["path"][len(rp):] if not R4[n][3] else
                                                   I[I[i]["parent"]]["path"][len(rp):] + "." + labels[n] if I[i]["parent"] != root else labels[n])
            for n, (i, ch, lvl) in enumerate(R)]          # 一覧に書く名前(根からのパス)
    mods = [(f"▸ 下位 {hidden[n]} 段を含む " if hidden[n] else "") + f'{cut(I[i]["module"], 18)}・深さ {I[i]["depth"] - I[root]["depth"]}'
            if i != OUT else "" for n, (i, ch, lvl) in enumerate(R)]
    NW = max([230] + [lvl * 16 + wlen(l) * 7.4 + wlen(m) * 5.4 + 44 for (i, ch, lvl), l, m in zip(R, labels, mods)])
    NW = min(NW, 640)
    CW = 96
    XC = X0 + NW                                          # ドメインの列の左端
    XA = XC + len(doms) * CW + 14                         # 通路の左端
    if style == "matrix":
        return matrix(dict(db=db, out=out, name=name, png=png, top=top, focus=focus, I=I, root=root, OUT=OUT, R=R, labels=labels, full=full,
                           mods=mods, doms=doms, col=col, C=C, cell=cell, arcs=arcs + rest, X=X, need=need, hidden=hidden))
    RY, YT, RHr = {}, {}, {}                              # 行ごとの中心・上端・高さ(線の多い行は高くする。幹線を決めた後で埋める)
    ry = lambda r: RY[r]
    # 幹線にまとめる: 重さごとに、線の多い行(送り側なら分岐、受け側なら合流)から順に、その行の線を 1 本の幹線に束ねる
    trunks = []                                           # dict(kind "s"=分岐 / "d"=合流, row, sev, mem=[線])
    for sv in order:
        left = [a for a in arcs if a["sev"] == sv and a["rs"] != a["rd"]]
        while left:
            cntk = collections.Counter()
            for a in left:
                cntk[("d", a["rd"])] += 1
                cntk[("s", a["rs"])] += 1
            (kind, row), _ = max(cntk.items(), key=lambda kv: (kv[1], kv[0][0] == "d", -kv[0][1]))
            mem = [a for a in left if (a["rd"] if kind == "d" else a["rs"]) == row]
            trunks.append(dict(kind=kind, row=row, sev=sv, mem=mem))
            left = [a for a in left if a not in mem]
    # 通路(レーン)の割り当て: 短い幹線から内側へ
    lanes = []
    for t in trunks:
        rows_ = [t["row"]] + [(a["rs"] if t["kind"] == "d" else a["rd"]) for a in t["mem"]]
        t["lo"], t["hi"] = min(rows_), max(rows_)
    for t in sorted(trunks, key=lambda t: (t["hi"] - t["lo"], min(a["no"] for a in t["mem"]))):
        for k, used in enumerate(lanes):
            if all(t["hi"] < u0 or t["lo"] > u1 for u0, u1 in used):
                used.append((t["lo"], t["hi"])); t["lane"] = k
                break
        else:
            lanes.append([(t["lo"], t["hi"])]); t["lane"] = len(lanes) - 1
    # 線の端: 行ごとに、幹線の根元(1 本)と枝(線 1 本ごと)を並べる。通路の内側の順に、上から下・左から右へ階段状に置く
    STEP = float(gap)
    SG = max(8.0, STEP * 0.75)                            # 同じ行の中の線どうしの間隔
    stubs = collections.defaultdict(list)                 # 行 → [(通路, 番号, 幹線, 線 or None)]
    for t in trunks:
        stubs[t["row"]].append((t["lane"], 0, t, None))
        for a in t["mem"]:
            stubs[a["rs"] if t["kind"] == "d" else a["rd"]].append((t["lane"], a["no"], t, a))

    def numlist(ns):                                      # [1, 2, 3, 7] → "1–3,7"
        ns, out, k = sorted(ns), [], 0
        while k < len(ns):
            j = k
            while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
                j += 1
            out.append(str(ns[k]) if j == k else f"{ns[k]}–{ns[j]}" if j > k + 1 else f"{ns[k]},{ns[j]}")
            k = j + 1
        return ",".join(out)
    # 番号の帯(マスの列と通路の間): 線の両端に同じ番号を置く。線をたどらなくても、番号で相手の行がわかる
    G = 0
    for r, lst in stubs.items():
        lst.sort(key=lambda x: (x[0], x[1]))
        xo = 8
        for _, _, t, a in lst:
            if a is None:
                t["lab"] = numlist([m["no"] for m in t["mem"]])
                t["gx"], t["gw"] = xo, (16 if len(t["mem"]) == 1 else max(20, wlen(t["lab"]) * 6.2 + 12))
                xo += t["gw"] + 8
            else:
                a["gx"] = xo
                xo += 26
        G = max(G, xo + 8)
    # 色: 同じ行で隣り合う線、隣の通路の幹線とは違う色にする(同じ重さの色の中から、まだ使っていないものを選ぶ)
    adj = collections.defaultdict(set)
    for r, lst in stubs.items():
        for (_, _, t1, _), (_, _, t2, _) in zip(lst, lst[1:]):
            if t1 is not t2:
                adj[id(t1)].add(id(t2)); adj[id(t2)].add(id(t1))
    for t1 in trunks:
        for t2 in trunks:
            if t1 is not t2 and abs(t1["lane"] - t2["lane"]) == 1 and t1["lo"] <= t2["hi"] and t2["lo"] <= t1["hi"]:
                adj[id(t1)].add(id(t2))
    tone = {}
    for t in sorted(trunks, key=lambda t: (t["lane"], t["lo"])):
        fam = TONES[t["sev"]] if tones else [SEV[t["sev"]][0]]
        used = collections.Counter(tone[x] for x in adj[id(t)] if x in tone)
        t["c"] = min(fam, key=lambda c: (used[c], fam.index(c)))
        tone[id(t)] = t["c"]
        for a in t["mem"]:
            a["c"] = t["c"]
    nself = collections.Counter()
    for a in arcs:
        if a["rs"] == a["rd"]:                            # 同じ行の中の線: 行の中で順に色を変える
            fam = TONES[a["sev"]] if tones else [SEV[a["sev"]][0]]
            a["c"] = fam[nself[a["rs"]] % len(fam)]
            nself[a["rs"]] += 1
    # 行の高さ: 出入りする線が多い行だけ高くし、線を STEP 刻みで離す
    selfn = collections.defaultdict(lambda: [0, 0])       # 同じ行の中の線: 右向き・左向きの本数
    for a in arcs:
        if a["rs"] == a["rd"]:
            for sc, dc in a["pairs"]:
                selfn[a["rs"]][0 if col[dc] > col[sc] else 1] += 1
    yy = Y0
    for n in range(len(R)):
        RHr[n] = max(RH, (len(stubs[n]) if n in stubs else 0) * STEP + 18,
                     2 * (SG / 2 + 1 + SG * (max(selfn[n]) - 1)) + 20 if n in selfn else 0)
        YT[n], RY[n] = yy, yy + RHr[n] / 2
        yy += RHr[n]
    YB = yy                                               # 行の下端
    for r, lst in stubs.items():
        for k, (_, _, t, a) in enumerate(lst):
            y = RY[r] + (k - (len(lst) - 1) / 2) * STEP
            if a is None:
                t["y"] = y
            else:
                a["ym"] = y
    LW = float(lane)
    XLN = XA + G                                          # 通路の左端
    XL = XLN + max(len(lanes), 1) * LW + 34               # 一覧の左端
    LISTW = 700
    W = max(XL + LISTW + 20, 1500)
    EH = 46
    # 凡例の高さ: 線の種類と読み方の文を、図の幅で折り返して数える
    lx_, nrow = X0, 1
    for txt in LEG_ITEMS.values():
        wd = 70 + wlen(txt) * 5.9
        if lx_ > X0 and lx_ + wd > W - 20:
            lx_, nrow = X0, nrow + 1
        lx_ += wd
    leg_lines, cur = [], ""
    for sen in LEG_TEXT:
        if cur and (wlen(cur + sen)) * 5.6 > W - 2 * X0:
            leg_lines.append(cur); cur = ""
        cur += sen
    leg_lines.append(cur)
    LEGH = nrow * 20 + len(leg_lines) * 18 + 44
    H = max(YB, Y0 + (len(arcs) + (1 if rest else 0)) * EH) + 46 + LEGH
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" font-family="\'Noto Sans JP\',sans-serif">',
         '<style>.ln{transition:opacity .12s}' + "".join(f'svg:has(.h{a["no"]}:hover) .ln:not(.m{a["no"]}){{opacity:.08}}' for a in arcs)
         + '</style>',                                    # ブラウザで開いた時: 番号か一覧にマウスを乗せると、その線だけ残す
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="{X0}" y="32" font-size="18" font-weight="800" fill="{INK}">CDC 階層図: {esc(I[root]["name"] if focus else top)}'
         f'<tspan font-size="12" font-weight="500" fill="{GRAY}" dx="12">行 = 階層 / 列 = クロックドメイン / 線 = クロックの乗り換え(非同期箇所)</tspan></text>']
    tot = collections.Counter()
    seen_d = {}
    for x in X:                                           # 受け側の FF 1 本は、一番重い分類で 1 回だけ数える
        sv = sev_of(x[4])
        if x[5] not in seen_d or order[sv] < order[seen_d[x[5]]]:
            seen_d[x[5]] = sv
    for sv in seen_d.values():
        tot[sv] += 1
    o.append(f'<text x="{X0}" y="54" font-size="12" fill="{GRAY}">乗り換え(受け側の FF 単位): 対策なし {tot["none"]} / 対策の誤り {tot["wrong"]} / 要確認 {tot["review"]} / 対策あり {tot["ok"]}'
             f'・階層の深さ 最大 {max(I[i]["depth"] for i in need) - I[root]["depth"]} 段・行の数字は FF 数</text>')
    # 列の見出し
    o.append(f'<text x="{X0 + 4}" y="{Y0 - 14}" font-size="11.5" font-weight="700" fill="{GRAY}">階層(インスタンス)</text>')
    o.append(f'<text x="{XC - 8}" y="{Y0 - 14}" font-size="10.5" fill="{GRAY}" text-anchor="end">モジュール・深さ</text>')
    for d, n in col.items():
        cx = XC + n * CW + CW / 2
        fg, bg = DOMC[n % len(DOMC)]
        nm = C[d]["name"]
        l1, l2 = (cut(nm, 13), "") if wlen(nm) <= 13 else (cut(nm.split(".")[-2] if "." in nm else "", 17), cut(nm.split(".")[-1], 13))
        o.append(f'<rect x="{cx - CW / 2 + 3:.0f}" y="{Y0 - 82}" width="{CW - 6}" height="58" rx="6" fill="{bg}" stroke="{fg}" stroke-width="1.4"/>')
        o.append(f'<text x="{cx:.0f}" y="{Y0 - 65}" font-size="11" font-weight="800" fill="{fg}" text-anchor="middle">{chr(65 + n)}</text>')
        if l2:
            o.append(f'<text x="{cx:.0f}" y="{Y0 - 53}" font-size="8.6" fill="{INK}" text-anchor="middle">{esc(l1)}</text>')
            o.append(f'<text x="{cx:.0f}" y="{Y0 - 42}" font-size="10.5" font-weight="700" fill="{INK}" text-anchor="middle">{esc(l2)}</text>')
        else:
            o.append(f'<text x="{cx:.0f}" y="{Y0 - 46}" font-size="11.5" font-weight="700" fill="{INK}" text-anchor="middle">{esc(l1)}</text>')
        o.append(f'<text x="{cx:.0f}" y="{Y0 - 30}" font-size="9" fill="{GRAY}" text-anchor="middle">{esc(C[d]["kind"])}・FF {C[d]["nff"]}</text>')
        o.append(f'<line x1="{cx:.0f}" y1="{Y0}" x2="{cx:.0f}" y2="{YB:.0f}" stroke="{bg}" stroke-width="{CW - 30}" opacity="0.35"/>')
    # 行
    for n, ((i, chain, lvl), lab) in enumerate(zip(R, labels)):
        y, RH = YT[n], RHr[n]
        if n % 2 == 0:
            o.append(f'<rect x="{X0}" y="{y}" width="{XA - X0 - 8:.0f}" height="{RH}" fill="#f8fafc"/>')
        o.append(f'<line x1="{X0}" y1="{y + RH}" x2="{XA - 8:.0f}" y2="{y + RH}" stroke="#e2e8f0" stroke-width="0.8"/>')
        x = X0 + 8 + lvl * 16
        if lvl:
            o.append(f'<path d="M{x - 10},{y} V{y + RH / 2} H{x - 2}" fill="none" stroke="#94a3b8" stroke-width="1"/>')
        o.append(f'<text x="{x}" y="{y + RH / 2 + 4}" font-size="12" font-weight="{700 if lvl < 2 else 500}" fill="{INK}">{esc(lab)}</text>')
        o.append(f'<text x="{XC - 8}" y="{y + RH / 2 + 4}" font-size="9.5" fill="{GRAY}" text-anchor="end">{esc(mods[n])}</text>')
    hot = set()
    for a in arcs:
        for sc, dc in a["pairs"]:
            hot |= {(a["rs"], sc), (a["rd"], dc)}
    PW = 52
    px = lambda d: XC + col[d] * CW + CW / 2 + PW / 2      # マスの右端
    def badge(x, y, a, r=8.2):
        c = a["c"]
        fs = 10 if r >= 8 else 9
        if a["sev"] == "wrong":                           # 対策の誤り: 四角
            return (f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" rx="2.5" fill="{c}" stroke="#fff" stroke-width="1.2"/>'
                    f'<text x="{x:.1f}" y="{y + fs * .36:.1f}" font-size="{fs}" font-weight="800" fill="#fff" text-anchor="middle">{a["no"]}</text>')
        if a["sev"] == "none":                            # 対策なし: 塗りの丸
            return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{c}" stroke="#fff" stroke-width="1.2"/>'
                    f'<text x="{x:.1f}" y="{y + fs * .36:.1f}" font-size="{fs}" font-weight="800" fill="#fff" text-anchor="middle">{a["no"]}</text>')
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#fff" stroke="{c}" stroke-width="{1.8 if a["sev"] == "review" else 1.1}"/>'
                f'<text x="{x:.1f}" y="{y + fs * .36:.1f}" font-size="{fs}" font-weight="700" fill="{c}" text-anchor="middle">{a["no"]}</text>')

    # 線(マスの下に敷く)。軽いものを先に、重いものを上に。線には白い縁をつけ、交差で上下がわかるようにする
    def seg(d, c, st, cls):
        return (f'<path d="{d}" fill="none" stroke="#fff" stroke-width="5.5" class="{cls}"/>'
                f'<path d="{d}" fill="none" stroke="{c}" {st} class="{cls}"/>')
    marks = []                                            # 番号(線・マスより上に描く)
    BR = 7.6 if STEP >= 12 else 6.8
    selfk = collections.defaultdict(lambda: [0, 0])
    for a in sorted(arcs, key=lambda a: -order[a["sev"]]):
        if a["rs"] != a["rd"]:
            continue
        c, st = a["c"], STROKE[a["sev"]]                   # 同じ行の中: マスからマスへまっすぐ(右向きは上、左向きは下に 6px 刻み)
        cls = f'ln m{a["no"]} h{a["no"]}'
        for k, (sc, dc) in enumerate(a["pairs"]):
            right = col[dc] > col[sc]
            kk = selfk[a["rs"]][0 if right else 1]
            selfk[a["rs"]][0 if right else 1] += 1
            y = ry(a["rs"]) + (-(SG / 2 + 1) - SG * kk if right else SG / 2 + 1 + SG * kk)
            xs = px(sc) + 2 if right else px(sc) - PW - 2
            xd = px(dc) - PW - 8 if right else px(dc) + 8
            o.append(seg(f"M{xs:.1f},{y:.1f} H{xd:.1f}", c, st, cls))
            o.append(f'<path d="M{xd + (7 if right else -7):.1f},{y:.1f} l{-8 if right else 8},-4.5 v9 z" fill="{c}" class="{cls}"/>')
            o.append(f'<circle cx="{xs:.1f}" cy="{y:.1f}" r="3.2" fill="{c}" stroke="#fff" stroke-width="1" class="{cls}"/>')
            if k == 0:                                    # 番号は線の上。同じ行の線どうしで重ならないよう横にずらす
                marks.append(f'<g class="{cls}">' + badge((xd - 14 - 20 * kk if right else xd + 14 + 20 * kk), y, a, BR) + '</g>')

    def src_end(scs, y, lx, c, st, cls):                  # 送り側: 一番左のマスから幹線へ。関係するマスごとに ●
        o.append(seg(f"M{px(scs[0]) + 2:.1f},{y:.1f} H{lx:.1f}", c, st, cls))
        for d in scs:
            o.append(f'<circle cx="{px(d) + 2:.1f}" cy="{y:.1f}" r="3.2" fill="{c}" stroke="#fff" stroke-width="1" class="{cls}"/>')

    def dst_end(dcs, y, lx, c, st, cls):                  # 受け側: 幹線から一番左のマスへ。関係するマスごとに ▶
        o.append(seg(f"M{lx:.1f},{y:.1f} H{px(dcs[0]) + 8:.1f}", c, st, cls))
        for d in dcs:
            o.append(f'<path d="M{px(d) + 1:.1f},{y:.1f} l8,-4.5 v9 z" fill="{c}" class="{cls}"/>')
    bycol = lambda ds: sorted(set(ds), key=lambda d: col[d])
    for t in sorted(trunks, key=lambda t: -order[t["sev"]]):
        c, st = t["c"], STROKE[t["sev"]]
        lx = XLN + t["lane"] * LW + LW / 2
        tcls = "ln " + " ".join(f'm{a["no"]}' for a in t["mem"])       # 幹線: 束ねた線のどれを選んでも残す
        if len(t["mem"]) == 1:
            tcls += f' h{t["mem"][0]["no"]}'
        for a in t["mem"]:
            cls = f'ln m{a["no"]} h{a["no"]}'
            if t["kind"] == "d":
                src_end(bycol(sc for sc, _ in a["pairs"]), a["ym"], lx, c, st, cls)
            else:
                dst_end(bycol(dc for _, dc in a["pairs"]), a["ym"], lx, c, st, cls)
            marks.append(f'<g class="{cls}">' + badge(XA + a["gx"] + 10, a["ym"], a, BR) + '</g>')       # 枝の端の番号
        if t["kind"] == "d":
            dst_end(bycol(dc for a in t["mem"] for _, dc in a["pairs"]), t["y"], lx, c, st, tcls)
        else:
            src_end(bycol(sc for a in t["mem"] for sc, _ in a["pairs"]), t["y"], lx, c, st, tcls)
        ys_all = [t["y"]] + [a["ym"] for a in t["mem"]]
        o.append(seg(f"M{lx:.1f},{min(ys_all):.1f} V{max(ys_all):.1f}", c, st + ' stroke-linecap="round"', tcls))
        if len(t["mem"]) > 1:
            for a in t["mem"]:
                o.append(f'<circle cx="{lx:.1f}" cy="{a["ym"]:.1f}" r="2.8" fill="{c}" class="ln m{a["no"]}"/>')      # 分岐・合流の点
            # 幹線の根元: 束ねた線の番号をまとめて書く
            marks.append(f'<g class="{tcls}"><rect x="{XA + t["gx"]:.1f}" y="{t["y"] - 7.5:.1f}" width="{t["gw"]:.1f}" height="15" rx="7.5" '
                         f'fill="#fff" stroke="{c}" stroke-width="1.4"/><text x="{XA + t["gx"] + t["gw"] / 2:.1f}" y="{t["y"] + 3.5:.1f}" '
                         f'font-size="10" font-weight="800" fill="{c}" text-anchor="middle">{esc(t["lab"])}</text></g>')
        else:
            a = t["mem"][0]
            marks.append(f'<g class="ln m{a["no"]} h{a["no"]}">' + badge(XA + t["gx"] + 8, t["y"], a, BR) + '</g>')
    # マス
    for (r, d), n in cell.items():
        cx = XC + col[d] * CW + CW / 2
        fg, bg = DOMC[col[d] % len(DOMC)]
        on = (r, d) in hot
        o.append(f'<rect x="{cx - PW / 2:.0f}" y="{ry(r) - 9:.0f}" width="{PW}" height="18" rx="9" fill="{bg if on else "#fff"}" '
                 f'stroke="{fg}" stroke-width="{1.8 if on else 0.8}"/>')
        o.append(f'<text x="{cx:.0f}" y="{ry(r) + 4:.0f}" font-size="10.5" font-weight="{800 if on else 500}" fill="{fg}" '
                 f'text-anchor="middle">{n}</text>')
    for (r, d) in hot:
        if (r, d) not in cell:                            # FF のないドメイン(入力端子・データに使ったクロック)
            cx = XC + col[d] * CW + CW / 2
            fg, bg = DOMC[col[d] % len(DOMC)]
            o.append(f'<rect x="{cx - PW / 2:.0f}" y="{ry(r) - 9:.0f}" width="{PW}" height="18" rx="9" fill="{bg}" stroke="{fg}" '
                     f'stroke-width="1.8" stroke-dasharray="3 2"/><text x="{cx:.0f}" y="{ry(r) + 4:.0f}" font-size="9.5" '
                     f'font-weight="700" fill="{fg}" text-anchor="middle">{"外" if R[r][0] == OUT else "端子"}</text>')

    o += marks
    # 一覧
    o.append(f'<text x="{XL}" y="{Y0 - 14}" font-size="11.5" font-weight="700" fill="{GRAY}">乗り換えの一覧(番号は線と対応。対策なし → 対策の誤り → 要確認 → 対策あり の順)</text>')
    for n, a in enumerate(arcs):
        y = Y0 + n * EH
        c = SEV[a["sev"]][0]
        o.append(f'<g class="ln m{a["no"]} h{a["no"]}"><rect x="{XL}" y="{y}" width="{LISTW}" height="{EH - 4}" fill="#fff" opacity="0"/>')
        o.append(f'<line x1="{XL}" y1="{y + EH - 3}" x2="{XL + LISTW}" y2="{y + EH - 3}" stroke="#e2e8f0" stroke-width="0.8"/>')
        o.append(badge(XL + 10, y + 13, a))
        x = XL + 26

        def chips(x, ds):
            for d in ds[:4]:
                fg, bg = DOMC[col[d] % len(DOMC)]
                o.append(f'<rect x="{x:.0f}" y="{y + 5}" width="16" height="16" rx="3" fill="{bg}" stroke="{fg}"/><text x="{x + 8:.0f}" '
                         f'y="{y + 17}" font-size="10.5" font-weight="800" fill="{fg}" text-anchor="middle">{chr(65 + col[d])}</text>')
                x += 18
            return x + 3
        s1, s2 = cut(full[a["rs"]], 32), cut(full[a["rd"]], 32)
        x = chips(x, sorted({sc for sc, _ in a["pairs"]}, key=lambda d: col[d]))
        o.append(f'<text x="{x:.0f}" y="{y + 17}" font-size="11.5" font-weight="700" fill="{INK}">{esc(s1)}</text>')
        x += wlen(s1) * 6.9 + 8
        o.append(f'<text x="{x:.0f}" y="{y + 17}" font-size="12" font-weight="800" fill="{c}">→</text>')
        x = chips(x + 18, sorted({dc for _, dc in a["pairs"]}, key=lambda d: col[d]))
        o.append(f'<text x="{x:.0f}" y="{y + 17}" font-size="11.5" font-weight="700" fill="{INK}">{esc(s2)}</text>')
        br = "・".join(f"{CLASS_S[k]} {v}" for k, v in sorted(a["cnt"].items(), key=lambda kv: (order[sev_of(kv[0])], -kv[1])))
        o.append(f'<text x="{XL + 26}" y="{y + 34}" font-size="10.5" fill="{c}" font-weight="700">{esc(SEV[a["sev"]][1])}: {esc(br if wlen(br) <= 46 else cut(br, 90)[:30] + "…")}'
                 f'<tspan fill="{GRAY}" font-weight="400" dx="8">例 {esc(cut(tail(a["ex"][0]), 26))} → {esc(cut(tail(a["ex"][1]), 26))}</tspan></text></g>')
    if rest:
        y = Y0 + len(arcs) * EH
        rc = collections.Counter(a["sev"] for a in rest)
        o.append(f'<text x="{XL + 26}" y="{y + 20}" font-size="11" fill="{GRAY}">ほか {len(rest)} 組('
                 + " / ".join(f"{SEV[k][1]} {v}" for k, v in rc.items()) + ')は線を省略。cdc.md を参照、または --focus で階層を絞る</text>')
    # 凡例(幅に収まらなければ折り返す)
    y = H - LEGH + 14
    o.append(f'<line x1="{X0}" y1="{y - 14}" x2="{W - 20}" y2="{y - 14}" stroke="#cbd5e1"/>')
    x = X0
    for k, txt in LEG_ITEMS.items():
        st, c = STROKE[k], SEV[k][0]
        wd = 70 + wlen(txt) * 5.9
        if x > X0 and x + wd > W - 20:
            x, y = X0, y + 20
        o.append(f'<circle cx="{x + 4}" cy="{y}" r="3.2" fill="{c}"/><line x1="{x + 4}" y1="{y}" x2="{x + 44}" y2="{y}" stroke="{c}" {st}/>'
                 f'<path d="M{x + 40},{y} l8,-4.5 v9 z" fill="{c}" transform="rotate(180 {x + 44} {y})"/>'
                 f'<text x="{x + 56}" y="{y + 4}" font-size="10.5" fill="{INK}">{esc(txt)}</text>')
        x += wd
    y += 8
    for ln in leg_lines:
        y += 18
        o.append(f'<text x="{X0}" y="{y}" font-size="11" fill="{GRAY}">{esc(ln)}</text>')
    o.append("</svg>")
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, name + ".svg")
    open(p, "w", encoding="utf-8").write("\n".join(o))
    if png:
        to_png(p)
    return p, dict(rows=len(R), arcs=len(arcs), rest=len(rest), depth=max(I[i]["depth"] for i in need) - I[root]["depth"])


MARK_BG = {"none": ("#fee2e2", "#dc2626"), "wrong": ("#fee2e2", "#dc2626"), "review": ("#fef3c7", "#d97706"), "ok": ("#ecfdf5", "#86efac"),
           "info": ("#f1f5f9", "#cbd5e1")}


def mark(x, y, sv, r=5.2):
    """重さの印: 対策なし = 塗りの丸 / 対策の誤り = 塗りの四角 / 要確認 = 白抜きのひし形 / 対策あり = 細い輪"""
    c = SEV[sv][0]
    if sv == "none":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{c}"/>'
    if sv == "wrong":
        return f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" rx="1.5" fill="{c}"/>'
    if sv == "review":
        return f'<path d="M{x:.1f},{y - r - 1:.1f} l{r + 1},{r + 1} l{-r - 1},{r + 1} l{-r - 1},{-r - 1} z" fill="#fff" stroke="{c}" stroke-width="1.8"/>'
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r - 1.2}" fill="#fff" stroke="{c}" stroke-width="1.5"/>'


def matrix(v):
    """CDC 階層表: 線を描かない。行 = 送り側の階層、列 = 受け側の階層(同じ行の並び)、マス = 乗り換え(重さの印・件数・ドメインの組)"""
    I, R, labels, full, mods, doms, col, C, cell, arcs = (v[k] for k in ("I", "R", "labels", "full", "mods", "doms", "col", "C", "cell", "arcs"))
    root, OUT = v["root"], v["OUT"]
    order = ORDER
    arcs = sorted(arcs, key=lambda a: (order[a["sev"]], -a["n"]))
    for n, a in enumerate(arcs):
        a["no"] = n + 1
    recv = sorted({a["rd"] for a in arcs})                # 受け側になる行だけを列にする
    cidx = {r: k for k, r in enumerate(recv)}
    wide = len(recv) <= 12
    CWm = 132 if wide else 62
    bycell = collections.defaultdict(list)
    for a in arcs:
        bycell[(a["rs"], a["rd"])].append(a)
    hot = set()
    for a in arcs:
        for sc, dc in a["pairs"]:
            hot |= {(a["rs"], sc), (a["rd"], dc)}
    # 行の左側: 番号・階層の名前・モジュール・ドメインの札(FF 数)
    X0, NUMW = 24, 30
    chips = []
    for n in range(len(R)):
        cs = [(d, cell.get((n, d), 0)) for d in doms if cell.get((n, d), 0) or (n, d) in hot]
        chips.append(cs)
    chipw = lambda d, k: 22 + len(str(k) if k else "外") * 6.4 + 4
    DW = max([40] + [sum(chipw(d, k) + 4 for d, k in cs) for cs in chips])
    NW = min(660, max([240] + [lvl * 16 + wlen(l) * 7.6 + wlen(m) * 5.6 + 40 for (i, ch, lvl), l, m in zip(R, labels, mods)]))
    XD = X0 + NUMW + NW + 10
    XG = XD + DW + 18                                     # 表の左端
    W = max(XG + len(recv) * CWm + 30, 1500)
    # ドメインの凡例(上)
    dl, x, y = [], X0, 84
    for d in doms:
        t = f'{C[d]["name"]}({C[d]["kind"]}・FF {C[d]["nff"]})'
        t = cut(t, 46)
        wd = 26 + wlen(t) * 6.3 + 18
        if x > X0 and x + wd > W - 20:
            x, y = X0, y + 24
        dl.append((x, y, d, t))
        x += wd
    HH = 74 if wide else 176
    Y0 = y + 30 + HH
    RHr, YT, yy = {}, {}, Y0
    for n in range(len(R)):
        k = max([len(bycell.get((n, r), ())) for r in recv] + [1])
        RHr[n] = max(30, k * 18 + 12)
        YT[n] = yy
        yy += RHr[n]
    YB = yy
    bad = [a for a in arcs if a["sev"] in ("none", "wrong", "review")][:40]
    nbad_rest = len([a for a in arcs if a["sev"] in ("none", "wrong", "review")]) - len(bad)
    LCOLS = 2 if W >= 1800 else 1
    EH = 40
    LH = -(-len(bad) // LCOLS) * EH + (50 if bad else 0)
    leg = ["読み方: 行が送り側、列が受け側。印のあるマスが、その行の階層からその列の階層へのクロックの乗り換え。列の番号は行の番号と同じ(斜めの印がその行自身)。",
           "マスの中は 印・件数(受け側の FF 単位)・ドメインの組(A→C = A のクロックの FF から C のクロックの FF へ)。赤い印のマスが対策忘れ・誤りの候補。",
           "行は乗り換えの端がある枝だけ。子が 1 つだけの中間の階層は 1 行にまとめ、つながり方が同じ兄弟は 1 行(×n)にまとめ、子が親と同じつながり方なら親にたたむ。",
           "グレイコードは記述(X ^ (X >> 1))で確かめ、名前は見ない。判定は構造だけを見たもの(準静的・タイミングは人が確認する)。全件は cdc.md、1 本ずつの回路は CDC 詳細図。"]
    H = YB + 30 + 26 + len(leg) * 18 + 20 + LH + 20
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" font-family="\'Noto Sans JP\',sans-serif">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="{X0}" y="32" font-size="18" font-weight="800" fill="{INK}">CDC 階層表: {esc(I[root]["name"] if v["focus"] else v["top"])}'
         f'<tspan font-size="12" font-weight="500" fill="{GRAY}" dx="12">行 = 送り側の階層 / 列 = 受け側の階層 / マス = クロックの乗り換え(非同期箇所)</tspan></text>']
    tot, seen_d = collections.Counter(), {}
    for x_ in v["X"]:
        sv = sev_of(x_[4])
        if x_[5] not in seen_d or order[sv] < order[seen_d[x_[5]]]:
            seen_d[x_[5]] = sv
    for sv in seen_d.values():
        tot[sv] += 1
    o.append(f'<text x="{X0}" y="54" font-size="12" fill="{GRAY}">乗り換え(受け側の FF 単位): 対策なし {tot["none"]} / 対策の誤り {tot["wrong"]} / '
             f'要確認 {tot["review"]} / 対策あり {tot["ok"]}・階層の深さ 最大 {max(I[i]["depth"] for i in v["need"]) - I[root]["depth"]} 段</text>')
    for x, y, d, t in dl:                                 # クロックドメイン
        fg, bg = DOMC[col[d] % len(DOMC)]
        o.append(f'<rect x="{x}" y="{y - 13}" width="20" height="18" rx="4" fill="{bg}" stroke="{fg}" stroke-width="1.3"/>'
                 f'<text x="{x + 10}" y="{y + 1}" font-size="11.5" font-weight="800" fill="{fg}" text-anchor="middle">{chr(65 + col[d])}</text>'
                 f'<text x="{x + 26}" y="{y + 1}" font-size="11.5" fill="{INK}">{esc(t)}</text>')
    # 列の見出し(受け側)
    o.append(f'<text x="{X0 + NUMW + 4}" y="{Y0 - 12}" font-size="12" font-weight="700" fill="{GRAY}">送り側(行): 階層</text>')
    o.append(f'<text x="{XD}" y="{Y0 - 12}" font-size="11" fill="{GRAY}">クロックと FF 数</text>')
    o.append(f'<text x="{XG}" y="{Y0 - HH - 8}" font-size="12" font-weight="700" fill="{GRAY}">受け側(列): 番号は左の行の番号</text>')
    for r, k in cidx.items():                             # 列の帯(先に全部敷く。斜めの見出しを隠さないため)
        if k % 2 == 0:
            o.append(f'<rect x="{XG + k * CWm}" y="{Y0 - (HH if wide else 26)}" width="{CWm}" height="{YB - Y0 + (HH if wide else 26)}" fill="#f8fafc"/>')
    for r, k in cidx.items():
        x = XG + k * CWm
        o.append(f'<circle cx="{x + (14 if wide else CWm / 2)}" cy="{Y0 - (HH - 14 if wide else 14)}" r="10" fill="{INK}"/>'
                 f'<text x="{x + (14 if wide else CWm / 2)}" y="{Y0 - (HH - 18 if wide else 10)}" font-size="10.5" font-weight="800" fill="#fff" '
                 f'text-anchor="middle">{r + 1}</text>')
        nm = labels[r]
        if wide:
            l1 = nm if wlen(nm) <= 19 else cut(nm, 38)
            a_, b_ = (l1, "") if wlen(l1) <= 19 else (l1[:len(l1) // 2], l1[len(l1) // 2:])
            o.append(f'<text x="{x + 4}" y="{Y0 - HH + 42}" font-size="10.5" font-weight="700" fill="{INK}">{esc(a_)}</text>')
            if b_:
                o.append(f'<text x="{x + 4}" y="{Y0 - HH + 56}" font-size="10.5" font-weight="700" fill="{INK}">{esc(b_)}</text>')
        else:
            o.append(f'<text transform="translate({x + CWm / 2 + 4},{Y0 - 30}) rotate(-55)" font-size="10.5" font-weight="700" fill="{INK}">'
                     f'{esc(cut(nm, 24))}</text>')
    # 行
    for n, ((i, chain, lvl), lab) in enumerate(zip(R, labels)):
        y, rh = YT[n], RHr[n]
        o.append(f'<line x1="{X0}" y1="{y + rh}" x2="{XG + len(recv) * CWm}" y2="{y + rh}" stroke="#e2e8f0" stroke-width="0.8"/>')
        isr = n in cidx
        o.append(f'<circle cx="{X0 + 12}" cy="{y + rh / 2}" r="10" fill="{INK if isr else "#fff"}" stroke="{INK if isr else "#cbd5e1"}"/>'
                 f'<text x="{X0 + 12}" y="{y + rh / 2 + 3.8}" font-size="10.5" font-weight="{800 if isr else 500}" fill="{"#fff" if isr else GRAY}" '
                 f'text-anchor="middle">{n + 1}</text>')
        x = X0 + NUMW + 6 + lvl * 16
        if lvl:
            o.append(f'<path d="M{x - 10},{y} V{y + rh / 2} H{x - 2}" fill="none" stroke="#94a3b8" stroke-width="1"/>')
        o.append(f'<text x="{x}" y="{y + rh / 2 + 4}" font-size="12.5" font-weight="{700 if lvl < 2 else 500}" fill="{INK}">{esc(lab)}</text>')
        o.append(f'<text x="{XD - 12}" y="{y + rh / 2 + 4}" font-size="9.5" fill="{GRAY}" text-anchor="end">{esc(mods[n])}</text>')
        cx = XD
        for d, k in chips[n]:
            fg, bg = DOMC[col[d] % len(DOMC)]
            on = (n, d) in hot
            wd = chipw(d, k)
            o.append(f'<rect x="{cx:.0f}" y="{y + rh / 2 - 9}" width="{wd:.0f}" height="18" rx="9" fill="{bg if on else "#fff"}" stroke="{fg}" '
                     f'stroke-width="{1.6 if on else 0.8}"/><text x="{cx + 8:.0f}" y="{y + rh / 2 + 4}" font-size="10.5" font-weight="800" fill="{fg}">'
                     f'{chr(65 + col[d])}</text><text x="{cx + 19:.0f}" y="{y + rh / 2 + 4}" font-size="10.5" fill="{fg}">'
                     f'{k if k else ("外" if i == OUT else "端子")}</text>')
            cx += wd + 4
        if n in cidx:                                     # 斜めの印: この列はこの行自身
            x = XG + cidx[n] * CWm
            o.append(f'<path d="M{x + 3},{y + 3} L{x + CWm - 3},{y + rh - 3}" stroke="#cbd5e1" stroke-width="1.2"/>')
    # マス
    for (rs, rd), ents in bycell.items():
        x, y, rh = XG + cidx[rd] * CWm, YT[rs], RHr[rs]
        ents.sort(key=lambda a: order[a["sev"]])
        bg, bd = MARK_BG[ents[0]["sev"]]
        o.append(f'<rect x="{x + 3}" y="{y + 3}" width="{CWm - 6}" height="{rh - 6}" rx="5" fill="{bg}" stroke="{bd}" '
                 f'stroke-width="{1.6 if ents[0]["sev"] in ("none", "wrong") else 1}"/>')
        for k, a in enumerate(ents):
            yy = y + rh / 2 + (k - (len(ents) - 1) / 2) * 18
            c = SEV[a["sev"]][0]
            o.append(mark(x + 14, yy, a["sev"]))
            o.append(f'<text x="{x + 24}" y="{yy + 4}" font-size="12" font-weight="800" fill="{c}">{a["n"]}</text>')
            if wide:
                bs = collections.defaultdict(list)
                for sc, dc in a["pairs"]:
                    bs[sc].append(dc)
                pt = " ".join(chr(65 + col[sc]) + "→" + ",".join(chr(65 + col[d]) for d in dcs) for sc, dcs in bs.items())
                o.append(f'<text x="{x + 30 + len(str(a["n"])) * 7.4}" y="{yy + 4}" font-size="10.5" fill="{INK}">{esc(cut(pt, 12))}</text>')
                if a in bad:
                    o.append(f'<text x="{x + CWm - 8}" y="{yy + 3.5}" font-size="9" fill="{GRAY}" text-anchor="end">#{a["no"]}</text>')
    # 凡例
    y = YB + 30
    o.append(f'<line x1="{X0}" y1="{y - 16}" x2="{W - 20}" y2="{y - 16}" stroke="#cbd5e1"/>')
    x = X0
    for sv, txt in LEG_ITEMS.items():
        o.append(mark(x + 6, y - 1, sv) + f'<text x="{x + 18}" y="{y + 3}" font-size="11" fill="{INK}">{esc(txt)}</text>')
        x += 30 + wlen(txt) * 6.1
        if x > W - 420:
            x, y = X0, y + 20
    y += 8
    for ln in leg:
        y += 18
        o.append(f'<text x="{X0}" y="{y}" font-size="11" fill="{GRAY}">{esc(ln)}</text>')
    # 対策なし・対策の誤り・要確認の内訳(番号はマスの #n)
    if bad:
        y += 34
        o.append(f'<text x="{X0}" y="{y}" font-size="12" font-weight="700" fill="{GRAY}">対策なし・対策の誤り・要確認の内訳(#番号はマスの番号)'
                 + (f' ほか {nbad_rest} 件は cdc.md' if nbad_rest else '') + '</text>')
        per = -(-len(bad) // LCOLS)
        cw = (W - 2 * X0) / LCOLS
        for k, a in enumerate(bad):
            ex, ey = X0 + (k // per) * cw, y + 12 + (k % per) * EH
            c = SEV[a["sev"]][0]
            o.append(mark(ex + 8, ey + 12, a["sev"]) + f'<text x="{ex + 20}" y="{ey + 16}" font-size="11" font-weight="700" fill="{GRAY}">#{a["no"]}</text>'
                     f'<text x="{ex + 50}" y="{ey + 16}" font-size="11.5" font-weight="700" fill="{INK}">{esc(cut(full[a["rs"]], 40))}'
                     f'<tspan fill="{c}" font-weight="800" dx="6">→</tspan><tspan dx="6">{esc(cut(full[a["rd"]], 40))}</tspan></text>')
            br = "・".join(f"{CLASS_S[c_]} {n_}" for c_, n_ in sorted(a["cnt"].items(), key=lambda kv: -kv[1]))
            o.append(f'<text x="{ex + 50}" y="{ey + 31}" font-size="10.5" fill="{c}" font-weight="700">{esc(SEV[a["sev"]][1])}: {esc(cut(br, 40))}'
                     f'<tspan fill="{GRAY}" font-weight="400" dx="8">例 {esc(cut(tail(a["ex"][0]), 28))} → {esc(cut(tail(a["ex"][1]), 28))}</tspan></text>')
    o.append("</svg>")
    os.makedirs(v["out"], exist_ok=True)
    p = os.path.join(v["out"], v["name"] + ".svg")
    open(p, "w", encoding="utf-8").write("\n".join(o))
    if v["png"]:
        to_png(p)
    return p, dict(rows=len(R), arcs=len(arcs), rest=0, depth=max(I[i]["depth"] for i in v["need"]) - I[root]["depth"])


def merge_rows(R4, arcs):
    """つながり方(相手の行・向き・重さ)がまったく同じ行をまとめる。
    1) 同じ親の下の葉どうし → 1 行(×n)  2) 子が全部、親と同じつながり方 → 親の行にたたむ"""
    n = len(R4)
    par, stack = [None] * n, []
    for k, r in enumerate(R4):
        stack = stack[:r[2]]
        par[k] = stack[-1] if stack else None
        stack.append(k)
    kids = collections.defaultdict(list)
    for k in range(n):
        if par[k] is not None:
            kids[par[k]].append(k)
    sig = collections.defaultdict(set)
    for a in arcs:
        if a["rs"] == a["rd"]:
            sig[a["rs"]].add(("x", -1, a["sev"]))
        else:
            sig[a["rs"]].add(("s", a["rd"], a["sev"]))
            sig[a["rd"]].add(("d", a["rs"], a["sev"]))
    drop, out = {}, [list(r) for r in R4]
    for p_, ks in list(kids.items()):                     # 1) 兄弟
        grp = collections.defaultdict(list)
        for k in ks:
            if not kids[k] and sig[k] and R4[k][2] > 0:
                grp[frozenset(sig[k])].append(k)
        for g in grp.values():
            if len(g) >= 2 and not any(t[1] in g for k in g for t in sig[k]):
                for k in g[1:]:
                    out[g[0]][3] += [R4[k][0]] + R4[k][3]
                    out[g[0]][4] += R4[k][1] + R4[k][4]
                    drop[k] = g[0]
    if not drop:
        for p_, ks in list(kids.items()):                 # 2) 親と同じ形の子
            if ks and sig[p_] and all(not kids[k] and sig[k] == sig[p_] for k in ks) and p_ not in drop:
                for k in ks:
                    out[p_][4] += [R4[k][0]] + R4[k][1] + R4[k][3] + R4[k][4]
                    drop[k] = p_
    return [tuple(r) for k, r in enumerate(out) if k not in drop]


def reduced_rows(I, need, ends, root, shown):
    """need(乗り換えの端とその祖先)から行を作る → [(代表, まとめた中間, 表示の段, 同じ形の兄弟)]
    子が 1 つで自分に端のない中間は子とまとめ、同じモジュールの葉の兄弟が 4 つ以上あれば 1 行にまとめる"""
    ch = collections.defaultdict(list)
    for i in need:
        if i != root and i in shown:
            ch[I[i]["parent"]].append(i)
    out = []

    def emit(i, chain, lvl):
        kids = sorted(ch[i], key=lambda j: I[j]["path"])
        if i != root and len(kids) == 1 and i not in ends:
            return emit(kids[0], chain + [i], lvl)
        out.append((i, chain, lvl, []))
        grp = collections.defaultdict(list)
        for k in kids:
            if not ch[k]:
                grp[I[k]["module"]].append(k)
        done = set()
        for k in kids:
            if k in done:
                continue
            g = grp.get(I[k]["module"], []) if not ch[k] else []
            if len(g) >= 4:
                out.append((g[0], [], lvl + 1, g[1:]))
                done |= set(g)
            else:
                emit(k, [], lvl + 1)
    emit(root, [], 0)
    return out


# ================= CDC 詳細図(gatediag の YAML)=================
def detail(db, dst_re):
    """受け側の FF 1 本について、送り側の FF・途中の論理・同期化の段を gatediag の YAML にする"""
    pat = re.compile(dst_re)
    rows = [r for r in db.execute("""SELECT c.dst, d.path, c.src, s.path, c.class, c.stages, c.logic, c.via, sc.name, dc.name, s.width, d.width
                                     FROM cdc c JOIN signal s ON s.id=c.src JOIN signal d ON d.id=c.dst
                                     JOIN clock sc ON sc.id=c.src_clk JOIN clock dc ON dc.id=c.dst_clk""") if pat.search(r[1])]
    if not rows:
        sys.exit(f"--dst {dst_re}: 当てはまる乗り換えがありません")
    order = list(BAD) + list(REVIEW) + list(OK_CLASSES) + list(INFO)
    rows.sort(key=lambda r: (order.index(r[4]), r[1]))
    dst, dpath, cls = rows[0][0], rows[0][1], rows[0][4]
    rows = [r for r in rows if r[0] == dst and r[4] == cls]
    ffs = {r[0]: r[1] for r in db.execute("SELECT sig, domain FROM ff")}

    def nm(p):                                            # 表示名: 最後の 2 階層(上が最上位だけなら信号名だけ)
        q = p.split(".")
        return q[-1] if len(q) <= 2 else ".".join(q[-2:])

    def nxt(x):
        """x をそのまま受ける、同じドメインの次の段の FF"""
        seen, st = set(), [x]
        while st:
            y = st.pop()
            for d, k, di in db.execute("SELECT dst,kind,direct FROM edge WHERE src=? AND role='data' AND direct=1", (y,)):
                if k == "seq" and d in ffs and ffs[d] == ffs[x] and d != x:
                    return d
                if k in ("comb", "conn") and d not in seen and d not in ffs:
                    seen.add(d); st.append(d)
        return None

    def path_of(i):
        return db.execute("SELECT path FROM signal WHERE id=?", (i,)).fetchone()[0]

    def chain_from(x, n=4):
        c = [x]
        while len(c) < n:
            y = nxt(c[-1])
            if y is None or y in c:
                break
            c.append(y)
        return c
    cid = lambda p: re.sub(r"[^A-Za-z0-9_]", "_", nm(p))
    sclk, dclk = cid(rows[0][8]) + "_src" if cid(rows[0][8]) == cid(rows[0][9]) else cid(rows[0][8]), cid(rows[0][9])
    ports = [dict(id=sclk, dir="in"), dict(id=dclk, dir="in")]
    cells, nets, gs, gd = [], {}, [], []
    srcs = rows[:4]
    sq = []
    for k, r in enumerate(srcs):
        q = f"s{k}_q"
        ports.append(dict(id=f"s{k}_d", dir="in"))
        cells.append(dict(id=f"u_src{k}", type="dff", d=f"s{k}_d", ck=sclk, q=q, label=cut(nm(r[3]), 22)))
        if r[10] > 1:
            nets[q] = r[10]; nets[f"s{k}_d"] = r[10]
        sq.append(q); gs.append(f"u_src{k}")
    note = [f"{CLASS_JA[cls]}。受け側: {short(dpath)}"]
    if len(rows) > 4:
        note.append(f"送り側は {len(rows)} 本(図は 4 本まで)")
    w = rows[0][11]
    din = sq[0]
    logic = len(srcs) > 1 or bool(rows[0][6])
    out = "to_logic"
    ports.append(dict(id=out, dir="out"))
    if w > 1:
        nets[out] = w

    def sync_chain(first, d_net, pre, last_net, ok=None, width=1):
        ch = chain_from(first)
        prev = d_net
        ids = []
        for n, f in enumerate(ch):
            q = last_net if n == len(ch) - 1 else f"{pre}{n + 1}"
            c = dict(id=f"u_{pre}{n + 1}", type="dff", d=prev, ck=dclk, q=q, label=cut(nm(path_of(f)), 22))
            if n == 0 and ok:
                c["cdc_ok"] = ok
            if width > 1:
                nets[q] = width
            cells.append(c); ids.append(c["id"]); prev = q
        return ids
    if cls in ("sync", "sync_gray", "sync_bus", "sync_pulse", "sync_logic", "sync_tap"):
        if logic and cls == "sync_logic":
            cells.append(dict(id="u_logic", type="block", label="組み合わせ\n論理", **{"in": sq}, out="n_logic"))
            din = "n_logic"; gs.append("u_logic")
        ok = {"sync_gray": "グレイコード(1 回に 1 ビットだけ変わる)", "sync_bus": None, "sync_logic": None, "sync_tap": None}.get(cls, None)
        ids = sync_chain(dst, din, "sync", "sync_q", ok, w)
        gd += ids
        if cls == "sync_tap" and len(ids) >= 1:
            cells.append(dict(id="u_tap", type="block", label="ほかの\n論理", **{"in": ["sync1" if len(ids) > 1 else "sync_q"]}, out="tap_use"))
            ports.append(dict(id="tap_use", dir="out")); gd.append("u_tap")
        if cls == "sync_pulse":
            cells.append(dict(id="u_edge", type="xor", **{"in": ["sync_q", f"sync{max(len(ids) - 1, 1)}"]}, out=out, label="エッジ検出"))
            gd.append("u_edge")
        else:
            cells.append(dict(id="u_out", type="buf", **{"in": ["sync_q"]}, out=out))
            gd.append("u_out")
        if w > 1:
            nets["sync_q"] = w
    else:
        via = rows[0][7]
        if logic:
            cells.append(dict(id="u_logic", type="block", label="組み合わせ\n論理" if cls != "fifo" else "メモリの\n読み出し", **{"in": sq}, out="n_data"))
            din = "n_data"
            (gd if cls in ("fifo", "mem") else gs).append("u_logic")
            if w > 1:
                nets["n_data"] = w
        if cls in ("data_enable", "handshake", "gated", "fifo") :
            en = "en"
            if cls == "fifo":                             # 書きポインタの同期化(送り側 → 受け側)を探す。via は読みポインタ側
                r = db.execute("""SELECT c.dst FROM cdc c JOIN signal s ON s.id=c.src JOIN signal m ON m.id=?
                                  WHERE c.class IN ('sync_gray','sync_bus','sync') AND c.src_clk=(SELECT src_clk FROM cdc WHERE dst=? AND src=?)
                                    AND c.dst_clk=(SELECT dst_clk FROM cdc WHERE dst=? AND src=?) AND s.inst=m.inst LIMIT 1""",
                               (rows[0][2], dst, rows[0][2], dst, rows[0][2])).fetchone()
                via = r[0] if r else None
            if via is not None:
                vp = db.execute("SELECT s.path FROM cdc c JOIN signal s ON s.id=c.src WHERE c.dst=? LIMIT 1", (via,)).fetchone()
                ports.append(dict(id="req_d", dir="in"))
                cells.append(dict(id="u_req", type="dff", d="req_d", ck=sclk, q="req", label=cut(nm(vp[0]) if vp else "req", 22)))
                gs.append("u_req")
                ids = sync_chain(via, "req", "rsync", "req_s")
                en = "req_s"
                gd += ids
            else:
                ports.append(dict(id=en, dir="in"))
            if cls == "gated":
                cells.append(dict(id="u_gate", type="and", **{"in": [din, en]}, out="gated"))
                cells.append(dict(id="u_dst", type="dff", d="gated", ck=dclk, q=out, label=cut(nm(dpath), 22),
                                  cdc_ok="同期化した信号でゲートしてから受ける(ゲートの種類とタイミングは要確認)"))
                gd += ["u_gate", "u_dst"]
                if w > 1:
                    nets["gated"] = w
            else:
                # 条件の論理と取り込みは 1 つの箱にまとめ、名前は 2 行(FF の段の間を横に広げない。説明は注記に書く)
                cells.append(dict(id="u_cap", type="block", **{"in": [din, en]}, out="d_sel",
                                  label="空でなければ\n読み出す" if cls == "fifo" else "条件が立てば\n取り込む"))
                c = dict(id="u_dst", type="dff", d="d_sel", ck=dclk, q=out, label=cut(nm(dpath), 22))
                if cls in ("handshake", "fifo"):
                    c["cdc_ok"] = "同期化した制御が立っている間だけ取り込む(送り側はその間データを変えない)"
                cells.append(c); gd += ["u_cap", "u_dst"]
                if w > 1:
                    nets["d_sel"] = w
        else:                                             # no_sync / mem / clk_as_data / reconv / static
            c = dict(id="u_dst", type="dff", d=din, ck=dclk, q=out, label=cut(nm(dpath), 22))
            if cls == "static":
                c["cdc_ok"] = "準静的(動作中に変えない設定)"
            cells.append(c); gd.append("u_dst")
    how = {"fifo": "書きポインタを 2 段で同期化し、読みポインタと比べて空でない時だけメモリを読む",
           "handshake": "送り側の要求を 2 段で同期化し、それで作った条件が立った時だけデータを取り込む(その間、送り側はデータを変えない)",
           "gated": "同期化した信号とデータを論理で合わせてから受ける", "data_enable": "取り込みの条件が同期化されていない",
           "no_sync": "同期化の FF がない", "mem": "ポインタの同期化が見つからない", "sync_bus": "グレイコードでない多ビットをそのまま FF で受けている",
           "sync_logic": "論理でまとめた後に同期化している(グリッチを取り込む)", "sync_tap": "1 段目の出力をほかの論理でも使っている"}.get(cls)
    y = dict(title=f"CDC 詳細図: {cut(nm(rows[0][3]), 34)} → {cut(nm(dpath), 34)}",
             note=CLASS_JA[cls] + (f"。{how}" if how else "") + (f"。送り側は {len(rows)} 本(図は 4 本まで)" if len(rows) > 4 else "")
             + f"。受け側の FF: {short(dpath)}",
             ports=ports, cells=cells,
             groups=[dict(label=f"送り側ドメイン {cut(short(rows[0][8]), 30)}", cells=gs, color="#2563eb"),
                     dict(label=f"受け側ドメイン {cut(short(rows[0][9]), 30)}", cells=gd, color="#7c3aed")])
    if nets:
        y["nets"] = nets
    return y


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hier")
    h.add_argument("db"); h.add_argument("-o", "--out", default="out"); h.add_argument("--png", action="store_true")
    h.add_argument("--focus"); h.add_argument("--rows", type=int, default=36); h.add_argument("--arcs", type=int, default=28)
    h.add_argument("--name", default="cdc_hier")
    h.add_argument("--gap", type=int, default=14, help="同じ行に出入りする線どうしの間隔(px)。既定 14")
    h.add_argument("--lane", type=int, default=34, help="縦の幹線どうしの間隔(px)。既定 34")
    h.add_argument("--mono", action="store_true", help="隣り合う線の色を変えない(重さごとに 1 色)")
    h.add_argument("--style", choices=["both", "lines", "matrix"], default="both",
                   help="lines = CDC 階層図(線でつなぐ)/ matrix = CDC 階層表(線を描かず、送り側 × 受け側の表)/ both = 両方(既定)")
    h.add_argument("--no-merge", action="store_true", help="つながり方が同じ行をまとめない")
    h.add_argument("--sev", help="線にする重さ(none,wrong,review,ok,info をカンマ区切り。bad = none,wrong)。既定は全部(重い順に --arcs 本まで)")
    d = sub.add_parser("detail")
    d.add_argument("db"); d.add_argument("--dst", required=True); d.add_argument("-o", "--out", default="cdc_detail.yaml")
    g = ap.parse_args()
    db = sqlite3.connect(g.db)
    if not db.execute("SELECT count(*) FROM clock").fetchone()[0]:
        sys.exit("この DB はまだ解析していません。先に rtldb.py analyze を実行してください")
    if g.cmd == "hier":
        sv = set(g.sev.replace("bad", "none,wrong").split(",")) if g.sev else None
        for style in (["lines", "matrix"] if g.style == "both" else [g.style]):
            name = g.name if (style == "lines" or g.style != "both") else (g.name.replace("hier", "matrix") if "hier" in g.name else g.name + "_matrix")
            p, st = hier(db, g.out, g.focus, g.rows, g.arcs, g.png, name, sv, g.gap, g.lane, not g.mono, not g.no_merge, style)
            print(f"生成: {p}(行 {st['rows']} / 乗り換えの組 {st['arcs']}" + (f" + 省略 {st['rest']}" if st["rest"] else "")
                  + f" / 階層の深さ {st['depth']})")
    else:
        import yaml
        y = detail(db, g.dst)
        os.makedirs(os.path.dirname(os.path.abspath(g.out)), exist_ok=True)
        open(g.out, "w", encoding="utf-8").write(yaml.safe_dump(y, allow_unicode=True, sort_keys=False, width=200))
        print(f"生成: {g.out}({y['title']})")


if __name__ == "__main__":
    main()
