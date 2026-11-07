#!/usr/bin/env python3
"""ip_size — ライブラリからIP(マクロ)の実寸を調べる。

  python3 ip_size.py <LEF/Liberty/CSV/ディレクトリ>... -o ip_sizes.json [--find 名前]

  LEF     : MACRO <name> ... CLASS ... SIZE <w> BY <h> ;   → 幅・高さ(µm)   [確度 high]
  Liberty : cell (<name>) { area : <a> ; }                 → 面積のみ(縦横比不明) [確度 mid]
  CSV     : cell,w_um,h_um[,note]  データシートから手入力した値 [確度 high]
同じセルが複数にあれば LEF > CSV > Liberty を優先する。
"""
import re, os, sys, json, glob, argparse, difflib

PRI = {"lef": 3, "csv": 2, "liberty": 1}


def scan_lef(path, out):
    src = open(path, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"^\s*MACRO\s+(\S+)(.*?)^\s*END\s+\1\b", src, re.S | re.M):
        body = m.group(2)
        sz = re.search(r"\bSIZE\s+([\d.]+)\s+BY\s+([\d.]+)", body)
        if not sz:
            continue
        cls = re.search(r"\bCLASS\s+(\w+)", body)
        w, h = float(sz.group(1)), float(sz.group(2))
        put(out, m.group(1), dict(w=w, h=h, area=round(w * h, 3), cls=cls.group(1) if cls else None,
                                  source="lef", file=path))


def scan_lib(path, out):
    src = open(path, encoding="utf-8", errors="replace").read()
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    unit = re.search(r'area_unit\s*:\s*"?([^";]+)', src)
    for m in re.finditer(r"\bcell\s*\(\s*\"?([\w$]+)\"?\s*\)\s*\{", src):
        tail = src[m.end():m.end() + 4000]
        a = re.search(r"\barea\s*:\s*([\d.eE+-]+)\s*;", tail)
        if a:
            area = float(a.group(1))
            put(out, m.group(1), dict(w=None, h=None, area=area, cls=None, source="liberty", file=path,
                                      note=f"面積のみ(単位 {unit.group(1) if unit else 'um^2 と仮定'})・縦横比不明"))


def scan_csv(path, out):
    for line in open(path, encoding="utf-8"):
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 3 or cols[0].lower() in ("cell", "name"):
            continue
        try:
            w, h = float(cols[1]), float(cols[2])
        except ValueError:
            continue
        put(out, cols[0], dict(w=w, h=h, area=round(w * h, 3), cls=None, source="csv", file=path,
                               note=cols[3] if len(cols) > 3 else None))


def put(out, name, rec):
    old = out.get(name)
    if not old or PRI[rec["source"]] > PRI[old["source"]]:
        if old and old.get("area") and rec.get("area") and old["source"] != rec["source"]:
            rec["crosscheck"] = f"{old['source']} area={old['area']}"
        out[name] = rec


def scan(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for r, _, fs in os.walk(p):
                files += [os.path.join(r, f) for f in fs]
        else:
            files += glob.glob(p) or [p]
    out = {}
    for f in sorted(files):
        fl = f.lower()
        if fl.endswith(".lef"):
            scan_lef(f, out)
    for f in sorted(files):
        fl = f.lower()
        if fl.endswith(".lib"):
            scan_lib(f, out)
        elif fl.endswith(".csv"):
            scan_csv(f, out)
    return out


def lookup(db, name):
    if name in db:
        return db[name], []
    low = {k.lower(): k for k in db}
    if name.lower() in low:
        return db[low[name.lower()]], []
    return None, difflib.get_close_matches(name, list(db), n=5, cutoff=0.5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("-o", "--out", default="ip_sizes.json")
    ap.add_argument("--find", action="append", default=[])
    a = ap.parse_args()
    db = scan(a.paths)
    json.dump(db, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    big = sorted([(k, v) for k, v in db.items() if v["area"]], key=lambda kv: -kv[1]["area"])
    print(f"{len(db)} セル → {a.out}  (LEF {sum(v['source']=='lef' for v in db.values())} / "
          f"Liberty {sum(v['source']=='liberty' for v in db.values())} / CSV {sum(v['source']=='csv' for v in db.values())})")
    print("面積の大きい順(マクロ候補):")
    for k, v in big[:15]:
        wh = f"{v['w']:g} x {v['h']:g} um" if v["w"] else "縦横不明"
        print(f"  {k:28s} {wh:22s} {v['area']/1e6:.4f} mm2  [{v['source']}]")
    for n in a.find:
        r, sug = lookup(db, n)
        print(f"検索 {n}: " + (json.dumps(r, ensure_ascii=False) if r else f"なし 候補: {sug}"))
