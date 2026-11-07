#!/usr/bin/env python3
"""rtl_scan — Verilog/SystemVerilog から モジュール・ポート・インスタンス・接続を抽出する。

  python3 rtl_scan.py <file|dir>... -o rtl.json [--top soc_top] [--tree]

軽量パーサ(正規表現+括弧対応)。対応: ANSI/非ANSIポート、parameter、名前付き/位置接続、
2次元配列(メモリ推定用)。非対応(警告を出す): interface/modport、generate内の複雑な展開、
マクロ展開。精度が必要なら pyslang / verilator --json-only の結果に差し替える。
"""
import re, os, sys, json, argparse

KEYWORDS = {"assign", "wire", "reg", "logic", "input", "output", "inout", "always", "always_ff",
            "always_comb", "always_latch", "initial", "localparam", "parameter", "function", "task",
            "if", "else", "case", "casez", "casex", "for", "while", "begin", "end", "generate",
            "endgenerate", "genvar", "integer", "typedef", "import", "return", "assert", "property",
            "default", "endcase", "endfunction", "endtask", "bit", "byte", "int", "struct", "enum",
            "signed", "unsigned", "tri", "supply0", "supply1", "real", "string", "defparam", "module"}
DIRS = ("input", "output", "inout")


def strip(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r"\(\*.*?\*\)", " ", src, flags=re.S)
    src = re.sub(r"^\s*`(include|define|ifdef|ifndef|else|elsif|endif|timescale|default_nettype|undef)\b[^\n]*",
                 " ", src, flags=re.M)
    src = re.sub(r"`(\w+)", r"\1", src)   # マクロ参照は名前だけ残す
    return src


def match_paren(s, i):
    """s[i]=='(' の対応する ')' の index"""
    d = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            d += 1
        elif s[j] == ")":
            d -= 1
            if d == 0:
                return j
    raise ValueError("括弧が閉じていない")


def split_top(s, sep=","):
    out, d, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        if ch == sep and d == 0:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


def parse_params(s):
    params = {}
    for item in split_top(s):
        m = re.search(r"(\w+)\s*=\s*(.+)$", item.strip(), re.S)
        if m:
            params[m.group(1)] = m.group(2).strip()
    return params


def eval_width(rng, params):
    """'[DW-1:0]' → 256 (評価できなければ None)"""
    if not rng:
        return 1
    m = re.match(r"\[(.+):(.+)\]", rng.replace(" ", ""))
    if not m:
        return None
    env = {}
    for k, v in params.items():
        try:
            env[k] = int(eval(re.sub(r"(\d+)'[dD]?(\d+)", r"\2", v), {"__builtins__": {}}, env))
        except Exception:
            pass
    try:
        a = eval(m.group(1), {"__builtins__": {}}, env)
        b = eval(m.group(2), {"__builtins__": {}}, env)
        return abs(int(a) - int(b)) + 1
    except Exception:
        return None


DECL = re.compile(r"^\s*(input|output|inout)\b\s*((?:wire|reg|logic|var|signed|unsigned|tri|\s)*)\s*(\[[^\]]*\])?\s*(.*)$", re.S)


def parse_ports_ansi(plist, params):
    ports, cur = [], None
    for item in split_top(plist):
        it = item.strip()
        if not it:
            continue
        m = DECL.match(it)
        if m:
            cur = (m.group(1), m.group(3))
            name = m.group(4).strip()
        elif cur and re.match(r"^\w+$", it):
            name = it
        else:
            if re.match(r"^\w+\s*\.\s*\w+", it) or re.match(r"^\w+\s+\w+$", it):
                return None, f"interface型ポートは未対応: {it[:40]}"
            name = it
            cur = cur or (None, None)
        name = re.sub(r"\s*\[.*$", "", name).strip()
        if cur[0] is None and re.match(r"^\w+$", name):
            ports.append({"name": name, "dir": None, "range": None})
        elif re.match(r"^\w+$", name):
            ports.append({"name": name, "dir": cur[0], "range": cur[1], "width": eval_width(cur[1], params)})
    return ports, None


def parse_module(name, header_params, plist, body):
    params = parse_params(header_params or "")
    for m in re.finditer(r"\b(?:parameter|localparam)\b\s*(?:\w+\s+)?(?:\[[^\]]*\]\s*)?(\w+)\s*=\s*([^;,]+)", body):
        params.setdefault(m.group(1), m.group(2).strip())
    warn = []
    ports, w = parse_ports_ansi(plist or "", params)
    if w:
        warn.append(w); ports = []
    # 非ANSI: 本体の宣言で方向を補完
    if ports and any(p["dir"] is None for p in ports):
        idx = {p["name"]: p for p in ports}
        for st in body.split(";"):
            m = DECL.match(st)
            if not m:
                continue
            for n in split_top(m.group(4)):
                n = re.sub(r"\s*\[.*$", "", n.strip())
                if n in idx:
                    idx[n].update(dir=m.group(1), range=m.group(3), width=eval_width(m.group(3), params))
    mems = []
    for m in re.finditer(r"\b(?:reg|logic|bit)\b\s*(\[[^\]]*\])?\s*(\w+)\s*(\[[^\]]*\])\s*[;=]", body):
        mems.append({"name": m.group(2), "word": m.group(1), "depth": m.group(3)})
    insts = []
    for st in split_top(body, ";"):
        st = st.strip()
        st = re.sub(r"^(begin|end|generate|endgenerate)\b(\s*:\s*\w+)?\s*", "", st).strip()
        m = re.match(r"^(\w+)\s*(#\s*\()?", st)
        if not m or m.group(1) in KEYWORDS:
            continue
        mod = m.group(1)
        pos = m.end()
        iparams = {}
        if m.group(2):
            j = match_paren(st, st.index("(", m.start(2)))
            iparams = parse_inst_params(st[st.index("(", m.start(2)) + 1:j])
            pos = j + 1
        m2 = re.match(r"\s*(\w+)\s*(\[[^\]]*\])?\s*\(", st[pos:])
        if not m2:
            continue
        k = pos + m2.end() - 1
        try:
            j = match_paren(st, k)
        except ValueError:
            continue
        if st[j + 1:].strip():
            continue
        conns, positional = parse_conns(st[k + 1:j])
        insts.append({"module": mod, "name": m2.group(1), "array": m2.group(2), "params": iparams,
                      "conns": conns, "positional": positional})
    if re.search(r"\bgenerate\b|\bfor\s*\(\s*genvar", body):
        warn.append("generate構文あり: ループ内インスタンスは1個として抽出")
    return {"name": name, "params": params, "ports": ports, "instances": insts, "memories": mems, "warnings": warn}


def parse_inst_params(s):
    out = {}
    for it in split_top(s):
        m = re.match(r"\s*\.(\w+)\s*\((.*)\)\s*$", it, re.S)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_conns(s):
    items = [i.strip() for i in split_top(s) if i.strip()]
    if items and all(i.startswith(".") for i in items):
        conns = {}
        for it in items:
            if it == ".*":
                conns["*"] = "*"
                continue
            m = re.match(r"\.(\w+)\s*(?:\((.*)\))?\s*$", it, re.S)
            if m:
                conns[m.group(1)] = (m.group(2) if m.group(2) is not None else m.group(1)).strip()
        return conns, False
    return {str(i): it for i, it in enumerate(items)}, True


def scan(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for r, _, fs in os.walk(p):
                files += [os.path.join(r, f) for f in fs if f.endswith((".v", ".sv", ".vh", ".svh"))]
        else:
            files.append(p)
    mods, warns = {}, []
    for fpath in sorted(files):
        src = strip(open(fpath, encoding="utf-8", errors="replace").read())
        for m in re.finditer(r"\b(module|macromodule)\s+(\w+)\s*", src):
            i = m.end()
            hp = None
            if src[i:i + 1] == "#":
                k = src.index("(", i)
                j = match_paren(src, k)
                hp = src[k + 1:j]
                i = j + 1
            while src[i].isspace():
                i += 1
            plist = None
            if src[i] == "(":
                j = match_paren(src, i)
                plist = src[i + 1:j]
                i = j + 1
            e = src.find("endmodule", i)
            body = src[src.index(";", i) + 1:e]
            mod = parse_module(m.group(2), hp, plist, body)
            mod["file"] = fpath
            if mod["name"] in mods:
                warns.append(f"モジュール重複: {mod['name']} ({fpath})")
            mods[mod["name"]] = mod
        if re.search(r"\binterface\s+\w+", src):
            warns.append(f"SystemVerilog interface を含む: {fpath} (ポートは個別信号として扱えない)")
    # 位置接続をポート名に解決
    for mod in mods.values():
        for ins in mod["instances"]:
            sub = mods.get(ins["module"])
            if ins["positional"] and sub:
                ins["conns"] = {sub["ports"][int(k)]["name"]: v for k, v in ins["conns"].items()
                                if int(k) < len(sub["ports"])}
                ins["positional"] = False
            if "*" in ins["conns"] and sub:
                del ins["conns"]["*"]
                for p in sub["ports"]:
                    ins["conns"].setdefault(p["name"], p["name"])
            if not sub:
                ins["unknown"] = True
    used = {i["module"] for m in mods.values() for i in m["instances"]}
    tops = [n for n in mods if n not in used]
    tops.sort(key=lambda n: -len(mods[n]["instances"]))
    return {"modules": mods, "top_candidates": tops, "warnings": warns + [
        f"{m['name']}: {w}" for m in mods.values() for w in m["warnings"]]}


def tree(db, top, depth=0, maxd=4, inst="", out=None):
    out = out if out is not None else []
    m = db["modules"].get(top)
    tag = "" if m else "  (定義なし: ブラックボックス)"
    out.append("  " * depth + (f"{inst}: " if inst else "") + top + tag)
    if m and depth < maxd:
        for i in m["instances"]:
            tree(db, i["module"], depth + 1, maxd, i["name"], out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("-o", "--out", default="rtl.json")
    ap.add_argument("--top")
    ap.add_argument("--tree", action="store_true", help="階層ツリーを表示")
    a = ap.parse_args()
    db = scan(a.paths)
    top = a.top or (db["top_candidates"][0] if db["top_candidates"] else None)
    db["top"] = top
    json.dump(db, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"モジュール {len(db['modules'])} 個 / トップ候補: {', '.join(db['top_candidates'][:5])}")
    print(f"選択トップ: {top}  → {a.out}")
    if a.tree and top:
        print("\n".join(tree(db, top)))
    if db["warnings"]:
        print("警告:\n  " + "\n  ".join(db["warnings"]))
