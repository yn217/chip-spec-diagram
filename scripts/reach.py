"""到達性と所属の検査
  - どの CPU(cpu: true のブロック)からも、すべてのアドレス範囲(各バスのスレーブ)に届くか
    バスとバスはブリッジ(2本のバスにまたがるブロック: S 端子のデコード範囲 = 転送する窓)で辿る
  - owner: (管理する CPU) を持つブロックが、その CPU と同じバスにつながっているか
"""
from addrmap import SPACE, norm, inter, subtract, total, hx, fmt_size


def bus_graph(c):
    """bus -> {"local": [(ranges, label, key)], "out": [(ranges or ["default"], to_bus, bridge_id)]}"""
    g = {b: {"local": [], "out": []} for b in c.buses}
    port_bus = {}
    for bid, bus in c.buses.items():
        for a in bus.get("attach", []):
            port_bus[a] = bid
    for bid, bus in c.buses.items():
        for a in bus.get("attach", []):
            rr = c.attach_addr(bus, a)
            if not rr:
                continue
            blk = c.blocks.get(a.split(".")[0], {})
            # このブロックが他のバスにマスタとして出ていればブリッジ(窓)
            outs = {port_bus[f"{blk['id']}.{p['id']}"] for p in blk.get("ports", [])
                    if p.get("role") == "M" and port_bus.get(f"{blk['id']}.{p['id']}") not in (None, bid)}
            if outs:
                for o in outs:
                    g[bid]["out"].append((rr, o, blk["id"]))
            else:
                g[bid]["local"].append((rr, blk.get("label", blk["id"]), a))
    return g


def window(g, bus, rr):
    if rr != ["default"]:
        return norm([r for r in rr if r != "default"])
    taken = [r for lst, _, _ in g[bus]["local"] for r in lst if r != "default"]
    taken += [r for lst, _, _ in g[bus]["out"] if lst != ["default"] for r in lst]
    return subtract([SPACE], taken)


def reach_from(g, bus, F, seen=None, got=None):
    seen = (seen or set()) | {bus}
    got = got if got is not None else {}
    for rr, lab, key in g[bus]["local"]:
        hit = inter(rr, F)
        if hit:
            got[key] = norm(got.get(key, []) + hit)
    for rr, to, br in g[bus]["out"]:
        F2 = inter(F, window(g, bus, rr))
        if F2 and to not in seen:
            reach_from(g, to, F2, seen, got)
    return got


def cpu_buses(c, b):
    out = set()
    for bid, bus in c.buses.items():
        for a in bus.get("attach", []):
            if a.split(".")[0] == b["id"] and ((c.anch.get(a) or (0, 0, 0, 0, {}))[4] or {}).get("role") == "M":
                out.add(bid)
    if not out:            # コア → I$/D$ → L2 → バス: 結線をたどった先のバス
        from latency import chain_to_bus
        ch = chain_to_bus(c, b["id"])
        if ch:
            return cpu_buses(c, c.blocks[ch[-1][0]])
    return out


def merge_ports(got, targets):
    """複数ポートのスレーブ(同じブロック・同じ範囲): どれか1つのポートに届けば全ポート届いたとみなす"""
    grp = {}
    for key, (rr, lab) in targets.items():
        grp.setdefault((key.split(".")[0], str(rr)), []).append(key)
    out = dict(got)
    for keys in grp.values():
        best = max((got.get(k) for k in keys if got.get(k)), key=total, default=None)
        if best:
            for k in keys:
                if not out.get(k) or total(out[k]) < total(best):
                    out[k] = best
    return out


def masters(c):
    """リード/ライトを出すマスタのマクロ: バスにマスタ端子でつながるブロック(ブリッジ・直接接続は除く)と、
    結線をたどってバスに出る CPU コア(I$/D$→L2 等。L2 はコアの代わりに数える)。[(表示名, ブロック, バス集合)]"""
    from latency import chain_to_bus
    out, proxy = [], {}
    for b in c.blocks.values():          # バスに直接出ていないマスタ(cpu: true)
        if b.get("cpu") and not cpu_buses_direct(c, b):
            ch = chain_to_bus(c, b["id"])
            if ch:
                proxy[ch[-1][0]] = b
    for b in c.blocks.values():
        if b.get("virtual") or b.get("_ext") or b.get("access") in (False, "none"):
            continue
        mb = cpu_buses_direct(c, b)
        if not mb:
            continue
        sb = {bid for bid, bus in c.buses.items() for a in bus.get("attach", [])
              if a.split(".")[0] == b["id"] and ((c.anch.get(a) or (0, 0, 0, 0, {}))[4] or {}).get("role") == "S"}
        if sb and not (mb & sb) and not b.get("cpu"):   # 片方のバスの S → 別のバスの M = ブリッジ
            continue
        if b["id"] in proxy:
            p = proxy[b["id"]]
            out.append((f"{p.get('label', p['id'])}({b.get('label', b['id'])}経由)", p, mb))
        else:
            out.append((b.get("label", b["id"]), b, mb))
    return out


def cpu_buses_direct(c, b):
    return {bid for bid, bus in c.buses.items() for a in bus.get("attach", [])
            if a.split(".")[0] == b["id"] and ((c.anch.get(a) or (0, 0, 0, 0, {}))[4] or {}).get("role") == "M"}


def reach_of(c, g, buses, targets):
    got = {}
    for bus in buses:
        reach_from(g, bus, [SPACE], None, got)
    return merge_ports(got, targets)


def check_reach(c):
    """マスタのマクロごとに、全てのメモリアドレス(スレーブの範囲)へ届くか"""
    if not c.buses:
        return []
    g = bus_graph(c)
    targets = {key: (rr, lab) for bus in g for rr, lab, key in g[bus]["local"]}
    if not targets:
        return []
    w = []
    for name, b, buses in masters(c):
        acc = b.get("access")
        tg = targets if not isinstance(acc, list) else \
            {k: v for k, v in targets.items() if k.split(".")[0] in acc}
        got = reach_of(c, g, buses, tg)
        miss = list(dict.fromkeys(lab for key, (rr, lab) in tg.items() if not got.get(key)))
        part = list(dict.fromkeys(lab for key, (rr, lab) in tg.items() if got.get(key) and total(got[key]) < total(rr)))
        if miss:
            w.append(f"[到達性] {name} から届かない: {', '.join(miss[:8])}{' ...' if len(miss) > 8 else ''}"
                     " → バス間の接続(bus_links)と転送するアドレス窓を見直す(届かなくてよいなら access: を書く)")
        if part:
            w.append(f"[到達性] {name} から一部しか届かない: {', '.join(part[:6])}")
    return w + check_loops(c, g)


def check_loops(c, g=None):
    """アドレスがバスの間を回り続ける所(どこにもデコードされず、窓どうしで転送し合う)"""
    g = g or bus_graph(c)
    found, w = set(), []
    def dfs(bus, F, path):
        for rr, to, br in g[bus]["out"]:
            F2 = inter(F, window(g, bus, rr))
            if not F2:
                continue
            if to in path:
                cyc = path[path.index(to):] + [to]
                key = frozenset(cyc)
                if key not in found:
                    found.add(key)
                    from addrmap import hx
                    rng = ", ".join(f"{hx(a)}–{hx(b_)}" for a, b_ in F2[:2]) + (" ほか" if len(F2) > 2 else "")
                    w.append(f"[ループ] {' → '.join(c.buses[x].get('label', x) for x in cyc)}: {rng} がバスの間を回り続ける"
                             " → どちらかの窓を default から外す(デコードしない範囲はエラー応答に)")
            elif len(path) <= len(g):
                dfs(to, F2, path + [to])
    for bus in g:
        dfs(bus, [SPACE], [bus])
    return w


def group_buses(c, cpu_id):
    """CPU グループのバス: CPU が直接つながるバス + owner: がその CPU のバス + 一方向ブリッジ(AXI2APB 等)で下につながるバス"""
    grp = set(cpu_buses(c, c.blocks[cpu_id])) | {bid for bid, bus in c.buses.items() if bus.get("owner") == cpu_id}
    on = lambda blk: {bid for bid, bus in c.buses.items() for a in bus.get("attach", []) if a.split(".")[0] == blk}
    changed = True
    while changed:
        changed = False
        for b in c.blocks.values():
            if b["id"].startswith("bl_"):      # bus_links(双方向)はグループを広げない
                continue
            ps = {p["id"]: p.get("role") for p in b.get("ports", [])}
            ups = {bid for bid, bus in c.buses.items() for a in bus.get("attach", [])
                   if a.split(".")[0] == b["id"] and ps.get(a.split(".")[1]) == "S"}
            downs = {bid for bid, bus in c.buses.items() for a in bus.get("attach", [])
                     if a.split(".")[0] == b["id"] and ps.get(a.split(".")[1]) == "M"}
            if ups & grp and not downs <= grp and not b.get("cpu"):
                grp |= downs
                changed = True
    return grp


def check_owner(c):
    """owner: CPU と同じバスにつながっているか(1グループの CPU が管理するマクロは同じバスに)"""
    w = []
    for b in c.blocks.values():
        own = b.get("owner")
        if not own or own not in c.blocks:
            continue
        mine = {bid for bid, bus in c.buses.items() for a in bus.get("attach", []) if a.split(".")[0] == b["id"]}
        cpu_b = group_buses(c, own)
        if mine and cpu_b and not (mine & cpu_b):
            w.append(f"[所属] {b.get('label', b['id'])} は {c.blocks[own].get('label', own)} の管理だが別のバスにある")
    return w


def reach_table(c):
    """マスタごとに届くかの表(markdown)"""
    ms = masters(c)
    g = bus_graph(c)
    targets = [(key, rr, lab) for bus in g for rr, lab, key in g[bus]["local"]]
    cpus = [b for _, b, _ in ms]
    lines = ["| 範囲 | " + " | ".join(n for n, _, _ in ms) + " |",
             "|---|" + "---|" * len(ms)]
    res = {}
    for name, b, buses in ms:
        res[b["id"]] = reach_of(c, g, buses, {k: (rr, lab) for k, rr, lab in targets})
    seen_ = set()
    for key, rr, lab in sorted(targets, key=lambda t: t[1][0][0] if t[1] and t[1][0] != "default" else 0):
        if (key.split(".")[0], str(rr)) in seen_:      # 複数ポートのスレーブは1行
            continue
        seen_.add((key.split(".")[0], str(rr)))
        row = []
        for cpu in cpus:
            gk = res[cpu["id"]].get(key)
            row.append("○" if gk and total(gk) >= total(rr) else ("△" if gk else "×"))
        lines.append(f"| {lab} | " + " | ".join(row) + " |")
    return "\n".join(lines)
