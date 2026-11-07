#!/usr/bin/env python3
"""rtl2spec — rtl_scan の結果から図YAML(座標なし=自動配置)と推定レポートを作る。

  python3 rtl2spec.py rtl.json -o diagram.yaml -r inference.yaml [--top soc_top]
          [--expand u_npu,u_x] [--exclude-inst 'u_dbg.*'] [--bus u_xbar]

推定するもの(すべて inference.yaml に根拠と確度を残す):
  - 信号のIFグルーピング: AXI / AXI-Stream / APB / AHB / メモリIF / valid-ready / その他
  - Master/Slave: AXIは awvalid/arvalid、APBは psel、AHBは htrans の方向
  - バス: 名前(xbar/noc/interconnect...) または M/S 両方のバスIFを3個以上持つインスタンス
  - メモリ: モジュール名(sram/ram/mem/fifo...) または 2次元配列
  - IOの辺: トップポート名から(確度low → 必ずユーザに確認)
"""
import re, sys, os, json, argparse, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rtl_scan import eval_width

SKIP = re.compile(r"(^|_)(clk|clock|aclk|hclk|pclk|rst|rstn|rst_n|reset|resetn|aresetn|hresetn|presetn|"
                  r"test|testmode|scan\w*|dft\w*|tm|se)(_|$)", re.I)
PROTOS = [
    ("axi", re.compile(r"^(?P<p>.*?)_?(?P<k>aw|w|b|ar|r)(?P<f>valid|ready|addr|data|id|len|size|burst|resp|last|strb|prot|cache|lock|qos|user|region)$", re.I),
     lambda ks: "awvalid" in ks or "arvalid" in ks),
    ("axis", re.compile(r"^(?P<p>.*?)_?t(?P<f>valid|ready|data|last|keep|strb|user|id|dest)$", re.I),
     lambda ks: "valid" in ks),
    ("apb", re.compile(r"^(?P<p>.*?)_?p(?P<f>sel|selx|enable|write|addr|wdata|rdata|ready|slverr|prot|strb)$", re.I),
     lambda ks: "sel" in ks or "selx" in ks),
    ("ahb", re.compile(r"^(?P<p>.*?)_?h(?P<f>sel|trans|addr|write|wdata|rdata|ready|readyout|resp|size|burst|prot)$", re.I),
     lambda ks: "trans" in ks),
    ("mem", re.compile(r"^(?P<p>.*?)_?(?P<f>addr|ce|cen|cs|csn|en|we|wen|wdata|rdata|din|dout|be|bwe)$", re.I),
     lambda ks: "addr" in ks and (ks & {"rdata", "dout", "wdata", "din"})),
    ("vr", re.compile(r"^(?P<p>.*?)_?(?P<f>valid|ready|vld|rdy|data)$", re.I),
     lambda ks: ks & {"valid", "vld"}),
]
BUS_PROTOS = ("axi", "ahb", "apb")
BUS_NAME = re.compile(r"xbar|crossbar|interconnect|noc|fabric|(^|_)ic($|_)|switch|axi_?mux|(^|_)bus($|_)", re.I)
MEM_NAME = re.compile(r"sram|(^|_)ram|rom|(^|_)mem|fifo|(^|_)buf|cache|tcm|regfile|rf_", re.I)
PAD_SIDE = [(re.compile(r"pcie|host|usb|sata|mipi|csi|cam", re.I), "W"),
            (re.compile(r"eth|net|sfp|serdes|xaui|sgmii|dsi|hdmi|dp_", re.I), "E"),
            (re.compile(r"ddr|dram|lpddr|hbm|dq|flash|qspi", re.I), "S"),
            (re.compile(r"jtag|gpio|i2c|spi|uart|dbg|debug|pwm|irq|int", re.I), "N")]
EXT = [(re.compile(r"ddr|dram|lpddr", re.I), dict(id="dram", label="DRAM", kind="memory")),
       (re.compile(r"hbm", re.I), dict(id="hbm", label="HBM", kind="memory")),
       (re.compile(r"pcie", re.I), dict(id="host", label="Host")),
       (re.compile(r"eth|sfp", re.I), dict(id="net", label="Network")),
       (re.compile(r"mipi|csi|cam", re.I), dict(id="cam", label="Camera"))]


def read_sdc(paths):
    """create_clock から {ポート/ネット名: (clock名, Hz)}"""
    out = {}
    for path in paths:
        src = open(path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"create_clock\b([^\n]*)", src):
            line = m.group(1)
            per = re.search(r"-period\s+([\d.]+)", line)
            nm = re.search(r"-name\s+(\S+)", line)
            tg = re.search(r"get_(?:ports|pins|nets)\s+\{?\s*([\w/]+)", line)
            if per and tg:
                name = tg.group(1).split("/")[-1]
                out[name] = (nm.group(1) if nm else name, 1e9 / float(per.group(1)))
    return out


def fmt_hz(h):
    return f"{h/1e9:g}GHz" if h >= 1e9 else f"{h/1e6:g}MHz"


def ident(s):
    s = re.sub(r"[^\w]", "_", s).strip("_")
    return s or "p"


def nets_of(expr):
    expr = re.sub(r"\[[^\]]*\]", "", expr)
    expr = re.sub(r"\d+'[bBhHdDoO][\w]+", "", expr)
    return {n for n in re.findall(r"[A-Za-z_]\w*", expr) if not SKIP.search(n)}


def fmt_bytes(bits):
    b = bits / 8
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:g}{u}" if b == int(b) else f"{b:.1f}{u}"
        b /= 1024


class Infer:
    def __init__(self):
        self.items = []

    def add(self, kind, target, value, conf, evidence):
        self.items.append(dict(kind=kind, target=target, value=value, confidence=conf, evidence=evidence))


def group_ifaces(ports, params, inf=None, owner=""):
    """ポート一覧 → IF の辞書 {iface_id: {proto, role, dir, signals:[...], width}}"""
    left = [p for p in ports if not SKIP.search(p["name"])]
    ifs = {}
    for proto, rx, ok in PROTOS:
        cand = {}
        for p in left:
            m = rx.match(p["name"])
            if m:
                key = (m.group("k") if "k" in rx.groupindex else "") + m.group("f")
                cand.setdefault(m.group("p").rstrip("_").lower() or proto, []).append((key.lower(), p))
        for pre, sigs in cand.items():
            ks = {k for k, _ in sigs}
            if not ok(ks):
                continue
            iid = ident(pre)
            while iid in ifs:
                iid += "_" + proto
            d = {k: p for k, p in sigs}
            role, dirn, ev = None, None, ""
            if proto == "axi":
                v = d.get("awvalid") or d.get("arvalid")
                role = "M" if v["dir"] == "output" else ("S" if v["dir"] == "input" else None)
                ev = f"{v['name']} が {v['dir']}"
            elif proto == "apb":
                v = d.get("sel") or d.get("selx")
                role = "M" if v["dir"] == "output" else "S"; ev = f"{v['name']} が {v['dir']}"
            elif proto == "ahb":
                v = d["trans"]; role = "M" if v["dir"] == "output" else "S"; ev = f"{v['name']} が {v['dir']}"
            elif proto == "mem":
                v = d["addr"]; dirn = "out" if v["dir"] == "output" else "in"
                ev = f"{v['name']} が {v['dir']}（{'要求側' if dirn == 'out' else 'メモリ側'}）"
            else:
                v = d.get("valid") or d.get("vld"); dirn = "out" if v["dir"] == "output" else "in"
                ev = f"{v['name']} が {v['dir']}"
            data = d.get("wdata") or d.get("data") or d.get("rdata") or d.get("din")
            width = eval_width(data.get("range"), params) if data else None
            ifs[iid] = dict(proto=proto, role=role, dir=dirn, width=width,
                            signals=[p["name"] for _, p in sigs])
            if inf is not None and owner:
                conf = "high" if proto in ("axi", "apb", "ahb", "axis") and v.get("dir") else "mid"
                if v.get("dir") is None:
                    conf = "low"; ev += "（方向不明: モジュール定義なし）"
                inf.add("interface", f"{owner}.{iid}",
                        f"{proto.upper()} {role or dirn or '?'}" + (f" {width}b" if width else ""), conf,
                        f"{len(sigs)}信号 / {ev}")
            used = {p["name"] for _, p in sigs}
            left = [p for p in left if p["name"] not in used]
    # 残り: 先頭トークンでまとめる(IOピンなど)
    misc = {}
    for p in left:
        misc.setdefault(ident(p["name"].split("_")[0].lower()), []).append(p)
    for pre, ps in misc.items():
        iid = pre if pre not in ifs else pre + "_io"
        dirs = {p["dir"] for p in ps}
        dirn = "inout" if len(dirs) > 1 or "inout" in dirs else ("in" if dirs == {"input"} else "out")
        ifs[iid] = dict(proto="misc", role=None, dir=dirn, width=None, signals=[p["name"] for p in ps])
    return ifs


def mod_ports(db, inst):
    """インスタンスのポート(定義がなければ接続名から)"""
    mod = db["modules"].get(inst["module"])
    if mod:
        params = dict(mod["params"]); params.update(inst.get("params", {}))
        return mod["ports"], params
    return [{"name": k, "dir": None, "range": None} for k in inst["conns"]], {}


def analyze(db, modname, inf, prefix="", exclude=None, bus_hint=None):
    """モジュール内部の接続グラフ: (nodes, edges, own_ifs)"""
    mod = db["modules"][modname]
    own = group_ifaces(mod["ports"], mod["params"])
    nodes = {}
    netmap = {}
    for io, d in own.items():
        for s in d["signals"]:
            for n in nets_of(s):
                netmap.setdefault(n, set()).add(("@", io))
    for ins in mod["instances"]:
        if exclude and exclude.search(ins["name"]):
            inf.add("exclude", prefix + ins["name"], "除外", "high", "--exclude-inst 指定")
            continue
        ports, params = mod_ports(db, ins)
        ifs = group_ifaces(ports, params, inf, prefix + ins["name"])
        sub = db["modules"].get(ins["module"])
        nodes[ins["name"]] = dict(inst=ins, ifs=ifs, params=params, sub=sub)
        if ins.get("unknown"):
            inf.add("blackbox", prefix + ins["name"], ins["module"], "low",
                    "モジュール定義が見つからない: 方向・M/S が推定できない")
        p2if = {s: iid for iid, d in ifs.items() for s in d["signals"]}
        for pname, expr in ins["conns"].items():
            if pname in p2if:
                for n in nets_of(expr):
                    netmap.setdefault(n, set()).add((ins["name"], p2if[pname]))
    edges = {}
    for n, ends in netmap.items():
        ends = sorted(ends)
        for i in range(len(ends)):
            for j in range(i + 1, len(ends)):
                if ends[i][0] != ends[j][0]:
                    edges.setdefault((ends[i], ends[j]), set()).add(n)
    # バス判定
    buses = {}
    for name, nd in nodes.items():
        bifs = [d for d in nd["ifs"].values() if d["proto"] in BUS_PROTOS]
        roles = {d["role"] for d in bifs}
        by_name = bool(BUS_NAME.search(name) or BUS_NAME.search(nd["inst"]["module"]))
        if (bus_hint and name in bus_hint) or (by_name and len(bifs) >= 2) or (len(bifs) >= 3 and {"M", "S"} <= roles):
            conf = "high" if (bus_hint and name in bus_hint) or (by_name and {"M", "S"} <= roles) else "mid"
            proto = max({d["proto"] for d in bifs}, key=lambda p: sum(1 for d in bifs if d["proto"] == p))
            width = max([d["width"] or 0 for d in bifs] or [0])
            buses[name] = dict(proto=proto, width=width)
            nm = sum(1 for d in bifs if d["role"] == "S"); ns = sum(1 for d in bifs if d["role"] == "M")
            inf.add("bus", prefix + name, f"{proto.upper()} バス（マスタ {nm} / スレーブ {ns}）", conf,
                    ("名前が一致" if by_name else "M/S両方のバスIFを持つ") + f": {nd['inst']['module']}, バスIF {len(bifs)}個")
    return nodes, edges, own, buses


def mem_info(nd):
    sub = nd["sub"]
    name = nd["inst"]["module"]
    ev = []
    size = None
    if sub and sub["memories"]:
        m = sub["memories"][0]
        ev.append(f"2次元配列 {m['name']}{m['depth']}")
        wbits = eval_width(m["word"], nd["params"]) if m["word"] else 1
        dep = eval_width(m["depth"], nd["params"])
        if wbits and dep:
            size = fmt_bytes(wbits * dep)
    if MEM_NAME.search(name):
        ev.append(f"モジュール名 {name}")
    return (bool(ev), size, " / ".join(ev))


def build(db, top, expand=(), exclude=None, bus_hint=None, sdc=None):
    inf = Infer()
    sdc = sdc or {}
    spec = {"chip": {"label": top}, "external": [], "pads": [], "buses": [], "blocks": [], "links": []}
    nodes, edges, own, buses = analyze(db, top, inf, "", exclude, bus_hint)
    used_ports = {}

    def port_of(inst, iid, d, to_bus=False):
        key = (inst, iid)
        if key not in used_ports:
            p = {"id": iid}
            if d["role"] and (to_bus or d["proto"] in BUS_PROTOS):
                p["role"] = d["role"]
                if d.get("width"):
                    p["width"] = d["width"]
            elif d["dir"] == "inout" or d["proto"] == "misc":
                p["dir"] = "inout" if d["dir"] == "inout" else None
            p["name"] = iid
            used_ports[key] = {k: v for k, v in p.items() if v is not None}
        return used_ports[key]

    def inst_clock(ins):
        for pn, expr in ins["conns"].items():
            if re.search(r"clk|clock", pn, re.I):
                for n in re.findall(r"[A-Za-z_]\w*", expr):
                    if n in sdc:
                        inf.add("clock", ins["name"], fmt_hz(sdc[n][1]), "mid",
                                f"SDC {sdc[n][0]} (ポート/ネット {n}) が .{pn} に接続")
                        return sdc[n][0]
        return None
        return None

    bus_ids = {}
    for bname, b in buses.items():
        bid = ident(re.sub(r"^(u|i|inst)_", "", bname))
        bus_ids[bname] = bid
        label = f"{b['proto'].upper()} {re.sub(r'^(u|i)_', '', bname)}"
        ent = {"id": bid, "label": label}
        if b["width"]:
            ent["width"] = b["width"]
        ck = inst_clock(nodes[bname]["inst"])
        if ck:
            ent["clock_domain"] = ck
        ent["attach"] = []
        spec["buses"].append(ent)
    # トップIO → パッド
    pad_of = {}
    for io, d in own.items():
        pid = "pad_" + io
        side, conf = "W", "low"
        for rx, s in PAD_SIDE:
            if rx.search(io):
                side = s; break
        pad = {"id": pid, "label": io.upper(), "side": side}
        spec["pads"].append(pad)
        pad_of[io] = pid
        inf.add("pad_side", pid, side, conf, f"ポート名 {io}* からの推定（フロアプランで確認が必要）")
        hit = False
        for rx, e in EXT:
            if rx.search(io) and not any(x["id"] == e["id"] for x in spec["external"]):
                ext = dict(e); ext["ports"] = [{"id": "p"}]; ext["status"] = "assumed"
                spec["external"].append(ext)
                spec["links"].append({"from": pid, "to": f"{ext['id']}.p", "dir": "both"})
                inf.add("external", ext["id"], ext["label"], "low", f"ポート名 {io}* から外部デバイスを推定")
                hit = True
                break
        if not hit:
            if side == "N":   # 制御・デバッグ系はデータフローに乗らないので外部要素を描かない
                pad["external"] = "none"
            else:
                ext = {"id": f"ext_{io}", "label": f"{io.upper()} 接続先", "status": "tbd", "ports": [{"id": "p"}]}
                spec["external"].append(ext)
                spec["links"].append({"from": pid, "to": f"ext_{io}.p", "dir": "both"})
                inf.add("external", ext["id"], "不明(TBD)", "low", f"{io}* のデータがチップ外のどこから来るか不明")
    # エッジ
    attach_seen = set()
    for (a, b), nets in edges.items():
        (ia, fa), (ib, fb) = a, b
        if ia == "@" or ib == "@":
            io, (ins, fi) = (fa, b) if ia == "@" else (fb, a)
            if ins in buses or ins not in nodes:
                continue
            d = nodes[ins]["ifs"][fi]
            p = port_of(ins, fi, d)
            spec["links"].append({"from": f"{ident(ins)}.{p['id']}", "to": pad_of[io]})
            continue
        if ia in buses or ib in buses:
            if ia in buses and ib in buses:
                inf.add("warning", f"{ia}-{ib}", "バス同士の接続", "low", "自動配置はバス1本のみ: 手動配置が必要")
                continue
            bus, (ins, fi), bf = (ia, b, fa) if ia in buses else (ib, a, fb)
            d = nodes[ins]["ifs"][fi]
            bd = nodes[bus]["ifs"][bf]
            if d["proto"] not in BUS_PROTOS:
                continue
            p = port_of(ins, fi, d, True)
            key = f"{ident(ins)}.{p['id']}"
            if key not in attach_seen:
                attach_seen.add(key)
                mm = re.match(r"([msMS])_?(\d+)", bf)   # クロスバー側のRTLポート名(s0_axi 等)の番号を使う
                ent = {"port": key, "bus_port": f"{mm.group(1).upper()}{mm.group(2)}"} if mm else key
                spec["buses"][list(buses).index(bus)]["attach"].append(ent)
                if d["role"] and bd["role"] and d["role"] == bd["role"]:
                    inf.add("conflict", key, f"{d['role']} 同士の接続", "low",
                            f"{bus}.{bf} も {bd['role']}: 方向の推定が誤っている可能性")
            continue
        da, db_ = nodes[ia]["ifs"][fa], nodes[ib]["ifs"][fb]
        pa, pb = port_of(ia, fa, da), port_of(ib, fb, db_)
        src, dst = (ia, pa, da), (ib, pb, db_)
        if da["role"] == "S" or da["dir"] == "in":
            src, dst = dst, src
        ln = {"from": f"{ident(src[0])}.{src[1]['id']}", "to": f"{ident(dst[0])}.{dst[1]['id']}"}
        if src[2]["role"] or src[2]["dir"] in ("out", "in"):
            ln["dir"] = "fwd"
        spec["links"].append(ln)
        inf.add("link", f"{ln['from']} → {ln['to']}", src[2]["proto"].upper(), "mid" if src[2]["proto"] != "misc" else "low",
                f"共有ネット: {', '.join(sorted(nets)[:3])}{' ...' if len(nets) > 3 else ''}")
    # ブロック
    def make_block(name, nd, prefix=""):
        b = {"id": ident(name), "label": re.sub(r"^(u|i|inst)_", "", name), "sub": nd["inst"]["module"]}
        is_mem, size, ev = mem_info(nd)
        if is_mem:
            b["kind"] = "memory"
            if size:
                b["sub"] = size
            inf.add("memory", prefix + name, f"メモリ{(' ' + size) if size else ''}", "mid" if len(ev.split("/")) == 1 else "high", ev)
        b["ports"] = [p for (i, _), p in used_ports.items() if i == name]
        if nd["inst"].get("unknown"):
            b["status"] = "assumed"
        ck = inst_clock(nd["inst"])
        if ck:
            b["clock_domain"] = ck
        return b
    for name, nd in nodes.items():
        if name in buses:
            continue
        b = make_block(name, nd)
        if name in expand and nd["sub"]:
            cnodes, cedges, cown, _ = analyze(db, nd["inst"]["module"], inf, name + "/", exclude)
            kids, kports = [], {}
            def kport(ci, iid, d):
                k = (ci, iid)
                if k not in kports:
                    p = {"id": iid, "name": iid}
                    if d["role"]:
                        p["role"] = d["role"]
                    kports[k] = p
                return kports[k]
            for (a, c), nets in cedges.items():
                (ia, fa), (ic, fc) = a, c
                if ia == "@" or ic == "@":
                    pio, (ci, cf) = (fa, c) if ia == "@" else (fc, a)
                    if pio not in nd["ifs"] and pio not in cown:
                        continue
                    pp = next((p for p in b["ports"] if p["id"] == pio), None)
                    if not pp:
                        d0 = cown[pio]
                        pp = {"id": pio, "name": pio}
                        if d0["role"]:
                            pp["role"] = d0["role"]
                        b["ports"].append(pp)
                    kp = kport(ci, cf, cnodes[ci]["ifs"][cf])
                    spec["links"].append({"from": f"{b['id']}.{pp['id']}", "to": f"{ident(ci)}.{kp['id']}", "level": 1})
                    continue
                da, dc = cnodes[ia]["ifs"][fa], cnodes[ic]["ifs"][fc]
                pa, pc = kport(ia, fa, da), kport(ic, fc, dc)
                s, t = (ia, pa, da), (ic, pc, dc)
                if da["role"] == "S" or da["dir"] == "in":
                    s, t = t, s
                ln = {"from": f"{ident(s[0])}.{s[1]['id']}", "to": f"{ident(t[0])}.{t[1]['id']}", "level": 1}
                if s[2]["dir"] in ("out",) or s[2]["role"]:
                    ln["dir"] = "fwd"
                spec["links"].append(ln)
            for ci, cnd in cnodes.items():
                kb = {"id": ident(ci), "label": re.sub(r"^(u|i|inst)_", "", ci), "sub": cnd["inst"]["module"]}
                is_mem, size, ev = mem_info(cnd)
                if is_mem:
                    kb["kind"] = "memory"
                    if size:
                        kb["sub"] = size
                    inf.add("memory", f"{name}/{ci}", f"メモリ{(' ' + size) if size else ''}", "mid", ev)
                kb["ports"] = [p for (i, _), p in kports.items() if i == ci]
                kids.append(kb)
            b["children"] = kids
        elif nd["sub"] and nd["sub"]["instances"]:
            inf.add("hierarchy", name, f"下位 {len(nd['sub']['instances'])} インスタンス", "high",
                    "--expand で詳細図を作れる")
        spec["blocks"].append(b)
    # 未接続ブロックの検出
    linked = {l["from"].split(".")[0] for l in spec["links"]} | {l["to"].split(".")[0] for l in spec["links"]}
    linked |= {(a["port"] if isinstance(a, dict) else a).split(".")[0] for bb in spec["buses"] for a in bb["attach"]}
    for b in spec["blocks"]:
        if b["id"] not in linked:
            inf.add("unconnected", b["id"], "接続なし", "mid", "クロック/リセット以外の接続が見つからない")
    if len(spec["blocks"]) > 14:
        inf.add("warning", top, f"ブロック {len(spec['blocks'])} 個", "high",
                "1枚には多すぎる: --exclude-inst や サブシステム単位の図に分けることを推奨")
    if sdc:   # クロック系統: SDC の create_clock をドメインに(ソース=クロック入力ポート)
        srcs, doms = [], []
        for port, (name, h) in sdc.items():
            srcs.append({"id": f"src_{port}", "label": port, "freq": fmt_hz(h), "kind": "pad"})
            doms.append({"id": name, "label": name, "freq": fmt_hz(h), "from": f"src_{port}"})
        spec["clocks"] = {"sources": srcs, "domains": doms}
        inf.add("clock_tree", top, f"ドメイン {len(doms)}", "low",
                "SDC の create_clock のみから作成。PLL/分周/ゲートは未反映(仕様を確認)")
    spec["scenario"] = {"steps": []}
    return spec, inf


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("rtl_json")
    ap.add_argument("-o", "--out", default="diagram.yaml")
    ap.add_argument("-r", "--report", default="inference.yaml")
    ap.add_argument("--top")
    ap.add_argument("--expand", default="", help="詳細化するインスタンス名(カンマ区切り)")
    ap.add_argument("--exclude-inst", default=None, help="除外するインスタンス名の正規表現")
    ap.add_argument("--bus", default="", help="バスとして扱うインスタンス名(カンマ区切り)")
    ap.add_argument("--sdc", action="append", default=[], help="SDC(create_clock から周波数を読む)")
    a = ap.parse_args()
    db = json.load(open(a.rtl_json, encoding="utf-8"))
    top = a.top or db.get("top")
    spec, inf = build(db, top, set(filter(None, a.expand.split(","))),
                      re.compile(a.exclude_inst) if a.exclude_inst else None,
                      set(filter(None, a.bus.split(","))), read_sdc(a.sdc))
    head = (f"# rtl2spec で {top} から生成。座標なし = chipdiag の自動配置。\n"
            f"# 配置を変えたい時は block に place: N|S|W|E / order: n、pad に side を書く。\n")
    with open(a.out, "w", encoding="utf-8") as fo:
        fo.write(head)
        yaml.safe_dump(spec, fo, allow_unicode=True, sort_keys=False, default_flow_style=None, width=110)
    rep = {"top": top, "summary": {}, "items": inf.items}
    for it in inf.items:
        rep["summary"].setdefault(it["confidence"], 0)
        rep["summary"][it["confidence"]] += 1
    yaml.safe_dump(rep, open(a.report, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False, width=110)
    print(f"図YAML: {a.out}  ブロック {len(spec['blocks'])} / バス {len(spec['buses'])} / パッド {len(spec['pads'])} / 結線 {len(spec['links'])}")
    print(f"推定レポート: {a.report}  確度 {rep['summary']}")
    need = [it for it in inf.items if it["confidence"] != "high" and it["kind"] not in ("link",)]
    if need:
        print("要確認(high以外):")
        for it in need:
            print(f"  [{it['confidence']}] {it['kind']}: {it['target']} = {it['value']}  ({it['evidence']})")
