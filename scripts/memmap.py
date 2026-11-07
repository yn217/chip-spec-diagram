#!/usr/bin/env python3
"""memmap — 図YAMLのアドレス範囲からメモリマップ図と表を作り、重なり・はみ出しを検査する。

  python3 memmap.py diagram.yaml -o out/ [--png]

アドレスの書き場所:
  buses[].attach の {port: X.s, addr: "0x3000_0000-0x303F_FFFF"} (インターコネクトのデコード範囲)
  blocks の addr: (pack 表の周辺など。親ブロックの範囲の内側にあること)
書式: "0xA-0xB" / "0xA/4M" / {base, size}
"""
import os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from addrmap import parse_addr, hx, fmt_size


def regions(c):
    """[{id, label, base, end, parent, via, bus, window}]
    window=True: バス間ブリッジの転送窓(メモリマップの図には出さず、デコードの重なり検査にだけ使う)"""
    from addrmap import parse_ranges
    out = []
    owner = {}   # block id -> [region id]
    port_bus = {a: bid for bid, bus in c.buses.items() for a in bus.get("attach", [])}
    for bid, bus in c.buses.items():
        for a in bus.get("attach", []):
            rr = c.attach_addr(bus, a)
            if not rr:
                continue
            blkid = a.split(".")[0]
            b = c.blocks.get(blkid, {})
            is_win = any(p.get("role") == "M" and port_bus.get(f"{blkid}.{p['id']}") not in (None, bid)
                         for p in b.get("ports", []))
            lab = c.bus_ports.get((bid, a), (None, None))[1]
            rngs = [r for r in rr if r != "default"]
            for k, r in enumerate(rngs):
                rid = a if len(rngs) == 1 else f"{a}#{k}"
                out.append(dict(id=rid, label=b.get("label", blkid) + (f" ({k + 1})" if len(rngs) > 1 else ""),
                                base=r[0], end=r[1], parent=None, bus=bid, window=is_win,
                                via=f"{bus.get('label', bid)} {lab or ''}".strip(), block=blkid))
                if not is_win:
                    owner.setdefault(blkid, []).append(rid)
    for b in c.blocks.values():
        if b.get("addr"):
            for k, r in enumerate([x for x in parse_ranges(b["addr"]) if x != "default"]):
                p, par = b["_parent"], None
                while p:
                    if p in owner:
                        par = owner[p]
                        break
                    p = c.blocks[p]["_parent"]
                cand = par or []
                pid = next((q for q in cand for R in out if R["id"] == q and R["base"] <= r[0] and r[1] <= R["end"]),
                           cand[0] if cand else None)
                out.append(dict(id=f"{b['id']}#{k}" if k else b["id"], label=b.get("label", b["id"]), base=r[0], end=r[1],
                                parent=pid, parents=cand, via=(c.blocks[b["_parent"]].get("label") if b["_parent"] else ""),
                                block=b["id"], bus=None, window=False))
                owner.setdefault(b["id"], []).append(out[-1]["id"])
    # 複数ポートのスレーブ(例 DRAM コントローラに AXI が3本): 同じブロック・同じ範囲は1行にまとめ、経由を並べる
    merged, seen = [], {}
    for r in out:
        k = (r.get("block"), r["base"], r["end"]) if r.get("bus") and not r.get("window") else None
        if k and k in seen:
            m = seen[k]
            m.setdefault("vias", [(m["bus"], m["via"])]).append((r["bus"], r["via"]))
            for x in out:            # 子の親の付け替え
                if x.get("parent") == r["id"]:
                    x["parent"] = m["id"]
                if x.get("parents") and r["id"] in x["parents"]:
                    x["parents"] = [m["id"] if q == r["id"] else q for q in x["parents"]]
            continue
        if k:
            seen[k] = r
        merged.append(r)
    return merged


def check(rs):
    w = []
    def overl(lst, what):
        lst = sorted(lst, key=lambda r: r["base"])
        for a, b in zip(lst, lst[1:]):
            if b["base"] <= a["end"]:
                w.append(f"{what}範囲が重なる: {a['label']} {hx(a['base'])}–{hx(a['end'])} と {b['label']} {hx(b['base'])}–{hx(b['end'])}")
    # 1) バスごとのデコード(転送窓を含む)
    for bus in sorted({r["bus"] for r in rs if r.get("bus")}):
        overl([r for r in rs if r.get("bus") == bus and r["parent"] is None], f"[{bus}] ")
    # 2) システム全体: 実体(窓以外)の最上位範囲は一意
    overl([r for r in rs if r["parent"] is None and not r.get("window")], "[全体] ")
    # 3) 子(表のセル等)の重なりとはみ出し
    byp = {}
    for r in rs:
        if r["parent"]:
            byp.setdefault(r["parent"], []).append(r)
    for par, lst in byp.items():
        overl(lst, "")
    for r in rs:
        if r.get("parents"):
            if not any(R["id"] in r["parents"] and R["base"] <= r["base"] and r["end"] <= R["end"] for R in rs):
                w.append(f"{r['label']} が親の範囲の外: {hx(r['base'])}–{hx(r['end'])}")
    for r in rs:
        size = r["end"] - r["base"] + 1
        if size & (size - 1) == 0 and r["base"] % size:
            w.append(f"[info] {r['label']}: 先頭 {hx(r['base'])} がサイズ {fmt_size(size)} 境界でない(デコードの都合なら確認)")
    return w


def color_of(rs, r, colors):
    """範囲の色 = その範囲がつながるバスの色(子は親の色)"""
    while r is not None:
        if r.get("bus"):
            return colors.get(r["bus"], "#475569")
        r = next((x for x in rs if x["id"] == r.get("parent")), None)
    return "#475569"


def svg(rs, title, parent=None, show_gaps=True, colors=None, bus_labels=None):
    """縦に並べたアドレス表(上=高位)。大きさは比例させず1行ずつ(小さい範囲も読める)"""
    lst = sorted([r for r in rs if r["parent"] == parent and not r.get("window")], key=lambda r: -r["base"])
    rows = []
    for i, r in enumerate(lst):
        rows.append(("r", r))
        nxt = lst[i + 1] if i + 1 < len(lst) else None
        if show_gaps and nxt and nxt["end"] + 1 < r["base"]:
            rows.append(("g", dict(base=nxt["end"] + 1, end=r["base"] - 1)))
    RH, X0, BW = 26, 150, 250
    H = 60 + RH * len(rows) + 20
    kids = {r["id"] for r in rs if r["parent"]} and {r["parent"] for r in rs if r["parent"]}
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 {H}" font-family="\'Noto Sans JP\',sans-serif">'
         f'<rect width="100%" height="100%" fill="#fff"/>'
         f'<text x="10" y="26" font-size="17" font-weight="800" fill="#1e293b">{title}</text>']
    y = 50
    maxx = 640
    for kind, r in rows:
        if kind == "g":
            o.append(f'<rect x="{X0}" y="{y}" width="{BW}" height="{RH}" fill="#f8fafc" stroke="#cbd5e1" stroke-dasharray="3 3"/>'
                     f'<text x="{X0 + BW/2}" y="{y + 17}" font-size="10.5" fill="#94a3b8" text-anchor="middle">Reserved {fmt_size(r["end"]-r["base"]+1)}</text>')
        else:
            sub = r["id"] in kids
            col = color_of(rs, r, colors or {})
            vias = r.get("vias") or [(r.get("bus"), r.get("via", ""))]
            cols_ = [(colors or {}).get(b_, col) if b_ else col for b_, _ in vias]
            o.append(f'<rect x="{X0}" y="{y}" width="{BW}" height="{RH}" fill="{col}" fill-opacity="{".16" if sub else ".07"}" stroke="#475569"/>')
            for j, cc in enumerate(cols_):   # 左の帯: つながるバスの色(複数ポートなら色を並べる)
                o.append(f'<rect x="{X0 + j * 5}" y="{y}" width="{7 if len(cols_) == 1 else 5}" height="{RH}" fill="{cc}"/>')
            o.append(f'<text x="{X0 + 9 + 5 * len(cols_)}" y="{y + 17}" font-size="12" font-weight="700" fill="#1e293b">{r["label"]}</text>'
                     f'<text x="{X0 + BW - 8}" y="{y + 17}" font-size="10.5" fill="#64748b" text-anchor="end">{fmt_size(r["end"]-r["base"]+1)}{" ▸" if sub else ""}</text>')
            vx = X0 + BW + 10
            for (b_, v_), cc in zip(vias, cols_):
                o.append(f'<text x="{vx}" y="{y + 17}" font-size="9.5" font-weight="700" fill="{cc}">{v_}</text>')
                vx += len(str(v_)) * 6.2 + 12
            maxx = max(maxx, vx)
        o.append(f'<text x="{X0 - 8}" y="{y + RH - 2}" font-size="10" fill="#334155" text-anchor="end" font-family="Consolas,Menlo,monospace">{hx(r["base"])}</text>'
                 f'<text x="{X0 - 8}" y="{y + 9}" font-size="10" fill="#94a3b8" text-anchor="end" font-family="Consolas,Menlo,monospace">{hx(r["end"])}</text>')
        y += RH
    if colors and bus_labels:   # 凡例: 色 = バスグループ(構成図のバスと同じ色)
        lx = 10
        for bid, col in colors.items():
            o.append(f'<rect x="{lx}" y="{H + 6}" width="12" height="12" fill="{col}"/>'
                     f'<text x="{lx + 16}" y="{H + 16}" font-size="11" font-weight="700" fill="{col}">{bus_labels.get(bid, bid)}</text>')
            lx += 30 + len(bus_labels.get(bid, bid)) * 8
        o[0] = o[0].replace(f'viewBox="0 0 640 {H}"', f'viewBox="0 0 640 {H + 26}"')
    o.append(f'<text x="10" y="{H - 6}" font-size="10" fill="#64748b">上が高位アドレス。大きさは比例させず1行に1範囲。▸ = 内訳あり(別の図)</text></svg>')
    if maxx > 640:   # 経由が長い(複数ポート)時は横に広げる
        o[0] = o[0].replace('viewBox="0 0 640 ', f'viewBox="0 0 {maxx + 10:.0f} ')
    return "".join(o)


def table_md(rs):
    lines = ["| 範囲 | 先頭 | 末尾 | サイズ | 接続 |", "|---|---|---|---|---|"]
    def walk(par, ind):
        for r in sorted([r for r in rs if r["parent"] == par and not r.get("window")], key=lambda r: r["base"]):
            lines.append(f"| {'　' * ind}{r['label']} | {hx(r['base'])} | {hx(r['end'])} | {fmt_size(r['end']-r['base']+1)} | {' / '.join(v for _, v in r['vias']) if r.get('vias') else r.get('via','')} |")
            walk(r["id"], ind + 1)
    walk(None, 0)
    return "\n".join(lines)


def pages(c):
    rs = regions(c)
    if not rs:
        return [], rs
    cols = dict(c.bus_color) if len(c.buses) > 1 else {}
    labs = {b: c.buses[b].get("label", b) for b in c.buses}
    out = [("30_memmap.svg", "メモリマップ(システム全体)", svg(rs, "メモリマップ(システム全体)", None, True, cols, labs))]
    for par in sorted({r["parent"] for r in rs if r["parent"]}, key=str):
        P = next(r for r in rs if r["id"] == par)
        t = f"メモリマップ: {P['label']} {hx(P['base'])}–{hx(P['end'])}"
        out.append((f"31_memmap_{P['id'].replace('.', '_').replace('#', '_')}.svg", t, svg(rs, t, par, True, cols, labs)))
    return out, rs


if __name__ == "__main__":
    from chipdiag import Chip, load, to_png
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    ps, rs = pages(c)
    os.makedirs(a.out, exist_ok=True)
    for name, t, s in ps:
        path = os.path.join(a.out, name)
        open(path, "w", encoding="utf-8").write(s)
        if a.png:
            to_png(path)
    open(os.path.join(a.out, "memmap.md"), "w", encoding="utf-8").write(table_md(rs))
    print(f"メモリマップ: {len(rs)} 範囲 / 図 {len(ps)} 枚 / 表 memmap.md → {a.out}")
    for w in check(rs):
        print("  警告:", w)
