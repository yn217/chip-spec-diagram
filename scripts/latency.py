"""アクセスのレイテンシ目安(サイクル数)を、経路(バス・ブリッジ・CDC・スレーブ)ごとに積み上げる。

spec:
  latency:
    unit: cpu          # 図に書く数字: cpu(CPU クロック換算・既定) / ns / local(各要素のクロック数)
    cdc: 2             # CDC 1回の同期にかかる受け側クロック数(既定 2)
    accesses:
      - {from: cpu, to: uart, op: read}            # 経路はバスとブリッジから自動で探す
      - {from: cpu, to: sram, op: write, title: ...}
  要素ごとの値(書かなければ既定値 = 目安。図と表に「既定」と出る):
    buses:  latency: {req: 2, resp: 1}      # 調停・デコード(そのバスのクロック数)
    blocks(ブリッジ): latency: {req: 2, resp: 1}
    blocks(スレーブ): latency: {read: 4, write: 2}
    blocks(マスタ):   latency: {issue: 1}   # 発行までの段数(任意)
"""
import math

BUS_DEF = {"axi": (2, 1), "ahb": (1, 1), "apb": (2, 0)}       # (req, resp)
BRIDGE_DEF = {"link": (0, 0), "apb": (1, 1), "ahb": (1, 1), "axi": (2, 1)}
SLAVE_DEF = {"memory": (2, 1), "logic": (2, 1)}               # (read, write)


def hz(v):
    from chipdiag import hz as _hz
    return _hz(v)


def bus_kind(bus):
    s = (str(bus.get("protocol", "")) + str(bus.get("label", "")) + str(bus.get("id", ""))).upper()
    if "APB" in s:
        return "apb"
    if "AHB" in s:
        return "ahb"
    return "axi"


def _att_blocks(c, bid):
    return [(a.split(".")[0], a) for a in c.buses[bid].get("attach", [])]


def _role(c, key):
    a = c.anch.get(key)
    return ((a[4] or {}).get("role")) if a else None


def chain_to_bus(c, src, prefer="d"):
    """バスに直接出ていないマスタ(例 CPU コア): 引っ付けた枠(attached)と結線(links)をたどって、
    バスにマスタ端子で出るブロック(例 L2)までの経路を返す: [(blk, in_key, out_key), ...]"""
    links = c.s.get("links", [])
    def bus_m(bid):
        return [a for bus in c.buses.values() for a in bus.get("attach", [])
                if a.split(".")[0] == bid and _role(c, a) == "M"]
    from collections import deque
    q = deque([(src, [])])
    seen = {src}
    while q:
        cur, path = q.popleft()
        nxt = []
        for b in c.blocks.values():           # 引っ付けた枠
            if b.get("_att_of") == cur:
                nxt.append((b["id"], None, None))
        for ln in links:
            for u, v in ((ln["from"], ln["to"]), (ln["to"], ln["from"])):
                if u.split(".")[0] == cur and v.split(".")[0] != cur:
                    nxt.append((v.split(".")[0], v, u))
        nxt.sort(key=lambda t: (prefer not in (t[0] + "." + str(t[1] or "") + "." + str(t[2] or "")).lower().replace("none", "")))   # データ側(D$)を優先
        for bid, in_key, out_from in nxt:
            if bid in seen or bid not in c.blocks:
                continue
            seen.add(bid)
            p2 = path[:-1] + ([(path[-1][0], path[-1][1], out_from)] if path else []) + [(bid, in_key, None)]
            if bus_m(bid):
                p2[-1] = (bid, in_key, bus_m(bid)[0])
                return p2
            q.append((bid, p2))
    return None


def route(c, src, dst):
    """src(マスタ) から dst(スレーブ) までのバス経路を幅優先で探す。
    戻り値: [("src", key), ("bus", bid), ("bridge", blk, s_key, m_key), ("bus", bid2), ..., ("dst", key)]"""
    starts = [(bid, a) for bid in c.buses for b, a in _att_blocks(c, bid) if b == src and _role(c, a) == "M"]
    if not starts:                 # コア → I$/D$ → L2 → バス のように、結線をたどってバスへ
        ch = chain_to_bus(c, src)
        if not ch:
            return None
        rest = route(c, ch[-1][0], dst)
        if not rest:
            return None
        return [("src", src)] + [("via", b, i, o) for b, i, o in ch] + rest[1:]
    from collections import deque
    q = deque()
    seen = set()
    for bid, a in starts:
        q.append((bid, [("src", a), ("bus", bid)]))
        seen.add(bid)
    while q:
        bid, path = q.popleft()
        for b, a in _att_blocks(c, bid):
            if b == dst and _role(c, a) == "S":
                return path + [("dst", a)]
        for b, a in _att_blocks(c, bid):           # このバスの S ポート → 同じブロックの M ポートが別のバス
            if _role(c, a) != "S" or b == dst or c.blocks.get(b, {}).get("cpu"):
                continue
            for bid2 in c.buses:
                if bid2 in seen:
                    continue
                for b2, a2 in _att_blocks(c, bid2):
                    if b2 == b and _role(c, a2) == "M":
                        seen.add(bid2)
                        q.append((bid2, path + [("bridge", b, a, a2), ("bus", bid2)]))
    return None


def _clk(c, o):
    """要素(ポートキー/ブロック/バス)の周波数(Hz)とドメイン名"""
    if isinstance(o, str) and o in c.anch:
        oc = c.owner_clock(o) or {}
    elif isinstance(o, str):
        oc = c.blocks.get(o) or {}
    else:
        oc = o or {}
    return hz(oc.get("clock")), oc.get("clock_domain") or (str(oc.get("clock")) if oc.get("clock") else None)


def compute(c, acc):
    L = c.s.get("latency", {})
    cdc_n = float(L.get("cdc", 2))
    src, dst, op = acc["from"], acc["to"], acc.get("op", "read")
    path = route(c, src, dst)
    if not path:
        return None, f"[レイテンシ] {src} → {dst} の経路がない"
    fcpu, _ = _clk(c, c.blocks.get(src))
    if not fcpu:
        fcpu, _ = _clk(c, path[0][1])
    hops, defaults = [], []

    def val(o, k, dflt, name):
        v = (o.get("latency") or {}).get(k)
        if v is None:
            defaults.append(f"{name}.{k}")
            return float(dflt), True
        return float(v), False

    # 要素の並びとドメイン
    seq = []   # (種類, 名前, 表示名, req_clk(Hz,dom), resp_clk(Hz,dom), req, resp, 既定か, 位置の情報)
    srcb = c.blocks.get(src, {})
    iss = float((srcb.get("latency") or {}).get("issue", 0))   # 発行の段数(任意。既定 0 は警告しない)
    k0 = path[0][1]
    seq.append(dict(kind="src", id=src, label=srcb.get("label", src), clk=_clk(c, k0), clk2=_clk(c, k0),
                    req=iss, resp=0, dflt=False, key=k0))
    for i, h in enumerate(path[1:], 1):
        if h[0] == "via":          # コアからバスまでの途中(キャッシュ等): ミスして次へ出るまで
            b = c.blocks[h[1]]
            r, s_ = (2, 1) if b.get("kind") == "memory" else (1, 1)
            rq, d1 = val(b, "req", r, b.get("label", h[1]))
            rs, d2 = val(b, "resp", s_, b.get("label", h[1]))
            cl = _clk(c, b["id"])
            seq.append(dict(kind="via", id=h[1], label=b.get("label", h[1]), clk=cl, clk2=cl,
                            req=rq, resp=rs, dflt=d1 or d2))
        elif h[0] == "bus":
            bus = c.buses[h[1]]
            r, s = BUS_DEF[bus_kind(bus)]
            rq, d1 = val(bus, "req", r, bus.get("label", h[1]))
            rs, d2 = val(bus, "resp", s, bus.get("label", h[1]))
            cl = _clk(c, bus)
            seq.append(dict(kind="bus", id=h[1], label=bus.get("label", h[1]), clk=cl, clk2=cl,
                            req=rq, resp=rs, dflt=d1 or d2, prev=path[i - 1], next=path[i + 1]))
        elif h[0] == "bridge":
            b = c.blocks[h[1]]
            kind = "link" if (b.get("virtual") or h[1].startswith("bl_")) else bus_kind(c.buses[path[i + 1][1]])
            r, s = BRIDGE_DEF.get(kind, (1, 1))
            rq, d1 = val(b, "req", r, b.get("label", h[1]))
            rs, d2 = val(b, "resp", s, b.get("label", h[1]))
            seq.append(dict(kind="bridge", id=h[1], label=b.get("label", h[1]),
                            clk=_clk(c, h[2]), clk2=_clk(c, h[3]), req=rq, resp=rs, dflt=d1 or d2, s=h[2], m=h[3]))
        else:
            b = c.blocks[h[1].split(".")[0]]
            rd, wr = SLAVE_DEF.get(b.get("kind", "logic"), (2, 1))
            v, d1 = val(b, op, rd if op == "read" else wr, b.get("label", b["id"]))
            cl = _clk(c, h[1])
            seq.append(dict(kind="dst", id=b["id"], label=b.get("label", b["id"]), clk=cl, clk2=cl,
                            req=v, resp=0, dflt=d1, key=h[1]))
    # 1要素の時間(ns)。CDC: 前の要素とドメインが違えば、受け側クロック cdc_n 個
    prev_dom = seq[0]["clk"][1]
    t_req = 0.0
    for e in seq:
        f_in, dom_in = e["clk"]
        f_out, dom_out = e["clk2"]
        e["cdc_req"] = cdc_n if (prev_dom and dom_in and dom_in != prev_dom) else 0
        e["cdc_int"] = cdc_n if (e["kind"] == "bridge" and dom_in and dom_out and dom_in != dom_out) else 0
        prev_dom = dom_out
        # ブリッジ: 要求は出口(M)側、応答は入口(S)側のクロックで数える
        f_req = f_out if e["kind"] == "bridge" else f_in
        f_rsp = f_in
        if not f_req or not f_rsp:
            return None, f"[レイテンシ] {e['label']} のクロックが未定(clock_domain を決めて)"
        e["ns_req"] = (e["req"] + e["cdc_int"]) / f_req * 1e9 + e["cdc_req"] / f_in * 1e9
        t_req += e["ns_req"]
        e["t_req"] = t_req
    # 応答: 逆向き。要素 i から i-1 へドメインが変われば、受け側(i-1 の出口)クロックで cdc_n
    t = t_req
    for i in range(len(seq) - 1, -1, -1):
        e = seq[i]
        f_in = e["clk"][0]
        cd = 0.0
        if e["kind"] == "bridge" and e["cdc_int"]:
            cd += cdc_n / f_in * 1e9
        if i > 0 and e["cdc_req"]:
            fp = seq[i - 1]["clk2"][0]
            cd += cdc_n / fp * 1e9
        e["ns_rsp"] = e["resp"] / f_in * 1e9 + cd
        e["cdc_rsp"] = (e["cdc_int"] + (e["cdc_req"] and cdc_n)) if cd else 0
        t += e["ns_rsp"]
    total = t
    cyc = lambda ns: ns * fcpu / 1e9
    res = dict(acc=acc, op=op, seq=seq, fcpu=fcpu, ns_req=t_req, ns_total=total,
               cyc_req=cyc(t_req), cyc_total=cyc(total), defaults=defaults, path=path,
               cdc=cdc_n)
    return res, None


def fmt_hz(f):
    return f"{f/1e9:.3g}G" if f >= 1e9 else f"{f/1e6:.3g}M"


def hop_text(r, e, which, unit):
    """図に書く短い数字(要素ごと)"""
    ns = e["ns_req"] if which == "req" else e["ns_rsp"]
    if ns <= 0:
        return None
    if unit == "ns":
        return f"+{ns:.3g}ns"
    if unit == "local":
        n = (e["req"] + e.get("cdc_int", 0) + e["cdc_req"]) if which == "req" else (e["resp"] + e["cdc_rsp"])
        f = (e["clk2"][0] if (which == "req" and e["kind"] == "bridge") else e["clk"][0])
        return f"+{n:g}clk@{fmt_hz(f)}"
    return f"+{math.ceil(ns * r['fcpu'] / 1e9 - 1e-9)}"


def total_text(r, unit):
    k = "Read" if r["op"] == "read" else "Write"
    c = math.ceil(r["cyc_total"] - 1e-9)
    if unit == "ns":
        return f"{k} 計 {r['ns_total']:.3g}ns"
    return f"{k} 計 ≈{c}cyc ({r['ns_total']:.3g}ns)"


def table_md(results):
    out = []
    for r in results:
        a = r["acc"]
        unit_cpu = f"{r['seq'][0]['label']} {fmt_hz(r['fcpu'])}Hz のサイクル"
        out.append(f"### {a.get('title') or (a['from'] + ' → ' + a['to'])}  ({r['op']})\n")
        out.append(f"| 要素 | クロック | 要求(clk) | CDC | 応答(clk) | 要求 ns | 応答 ns | 既定値 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for e in r["seq"]:
            ck = fmt_hz(e["clk"][0]) + ("→" + fmt_hz(e["clk2"][0]) if e["clk2"][0] != e["clk"][0] else "")
            cdc = []
            if e["cdc_req"]:
                cdc.append(f"入口 {e['cdc_req']:g}")
            if e.get("cdc_int"):
                cdc.append(f"内部 {e['cdc_int']:g}")
            out.append(f"| {e['label']} | {ck}Hz | {e['req']:g} | {' / '.join(cdc) or '-'} | {e['resp']:g} | "
                       f"{e['ns_req']:.3g} | {e['ns_rsp']:.3g} | {'仮' if e['dflt'] else ''} |")
        out.append(f"\n到着まで {r['ns_req']:.3g} ns ≈ {math.ceil(r['cyc_req'] - 1e-9)} cyc / "
                   f"{'データが戻るまで' if r['op'] == 'read' else '応答(B)が戻るまで'} "
                   f"{r['ns_total']:.3g} ns ≈ **{math.ceil(r['cyc_total'] - 1e-9)} cyc** ({unit_cpu})")
        if r["defaults"]:
            out.append(f"\n既定値(目安)を使った項目: {', '.join(r['defaults'])}")
        out.append("")
    return "\n".join(out)


def expand_steps(c):
    """latency.accesses → シナリオのステップ(要求の矢印 + 応答の矢印 + 要素ごとの数字)"""
    L = c.s.get("latency") or {}
    res = []
    for acc in L.get("accesses", []):
        r, err = compute(c, acc)
        if err:
            c.warn.append(err)
            continue
        for d in r["defaults"]:
            msg = f"[レイテンシ] 既定値(目安)を使用: {d} → 決まったら latency: を書く(decisions に仮置き)"
            if msg not in c.warn:
                c.warn.append(msg)
        req_path = [r["path"][0][1]]
        for h in r["path"][1:]:
            if h[0] == "via":
                req_path += [k for k in (h[2], h[3]) if k] or [h[1]]
            elif h[0] == "bus":
                req_path.append(h[1])
            elif h[0] == "bridge":
                req_path += [h[2], h[3]]
            else:
                req_path.append(h[1])
        col = acc.get("color", "#e11d48")
        flows = [dict(path=req_path, color=col, _lat=(r, "req"))]
        if True:   # 応答(リードデータ / 書き込み応答)も戻す
            flows.append(dict(path=req_path[::-1], color=acc.get("resp_color", "#0891b2"), style="control",
                              _lat=(r, "rsp")))
        title = acc.get("title") or f"{c.blocks[acc['from']].get('label')} → {c.blocks[acc['to']].get('label')} {'リード' if r['op'] == 'read' else 'ライト'}"
        note = (f"レイテンシ目安: 到着 ≈{math.ceil(r['cyc_req'] - 1e-9)}cyc、"
                f"{'データが戻るまで' if r['op'] == 'read' else '応答が戻るまで'} ≈{math.ceil(r['cyc_total'] - 1e-9)}cyc "
                f"({r['ns_total']:.3g}ns, {c.blocks[acc['from']].get('label')} {fmt_hz(r['fcpu'])}Hz 換算)" + (" ※既定値を含む" if r["defaults"] else ""))
        c.steps.append(dict(title=title, master=c.blocks[acc["from"]].get("label"), note=note,
                            flows=flows, cumulative=False, _latency=r))
        res.append(r)
    c.lat_results = res
    return res
