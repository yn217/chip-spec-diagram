#!/usr/bin/env python3
"""board — チップ外のボード(コネクタ・DRAM等)を描き、チップIOの辺を決める/突き合わせる。

  python3 board.py board.yaml -o out/ [--diagram diagram.yaml] [--floorplan fp.yaml] [--apply] [--png]

  - ボード外形・コネクタ・部品・チップを mm で描く(仮置きは破線+「仮」)
  - 各IFの相手部品の方向から「チップのどの辺にIOを置くべきか」を求める
  - 論理図のパッド(side) / フロアプランのPHY(side) と突き合わせて食い違いを出す
  - --apply: 論理図の pad.side をボードからの推奨に書き換える(status: decided のパッドは変えない)

board.yaml
  board:
    preset: pcie_lp | pcie_fh | m2_2280 | custom   外形・エッジ端子の位置を既定値で置く(仮)
    size: [w, h]                                 mm (custom 時)
    chip: {id: soc, at: [x, y], size: [15, 15], rot: 0}   パッケージ中心座標(mm)。rot=ダイの回転
    parts:
      - {id: dram0, kind: memory, label: LPDDR5, at: [x, y], size: [w, h]}
      - {id: sfp, kind: connector, label: SFP+, at: [..], size: [..]}
    nets:
      - {pad: pad_pcie, to: edge, fp: pcie_phy, bw: 8GB/s}   pad=論理図のパッドID / fp=フロアプランのブロックID
座標は mm、原点はボード左下。部品に status: decided がなければ「仮置き」として扱う。
"""
import os, sys, math, argparse, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PRESETS = {
    # 寸法は規格の代表値。採用時に必ず確認(status は assumed)
    "pcie_lp": dict(size=[167.65, 68.9], label="PCIe Low Profile (仮寸法)",
                    parts=[dict(id="edge", kind="connector", label="PCIe エッジ端子", at=[45, 2.5], size=[39, 5]),
                           dict(id="bracket", kind="bracket", label="ブラケット", at=[1, 34.45], size=[2, 68.9])]),
    "pcie_fh": dict(size=[167.65, 111.15], label="PCIe Full Height (仮寸法)",
                    parts=[dict(id="edge", kind="connector", label="PCIe エッジ端子", at=[45, 2.5], size=[39, 5]),
                           dict(id="bracket", kind="bracket", label="ブラケット", at=[1, 55.6], size=[2, 111.15])]),
    "m2_2280": dict(size=[80, 22], label="M.2 2280 (仮寸法)",
                    parts=[dict(id="edge", kind="connector", label="M.2 端子", at=[2.5, 11], size=[5, 18])]),
    "custom": dict(size=[100, 80], label="カスタムボード", parts=[]),
}
COL = {"connector": ("#e2e8f0", "#334155"), "memory": ("#fff7ed", "#c2410c"), "bracket": ("#cbd5e1", "#475569"),
       "part": ("#f1f5f9", "#475569"), "chip": ("#1e293b", "#0f172a")}
SIDES = ["N", "E", "S", "W"]


def esc(s):
    import html
    return html.escape(str(s), quote=True)


def side_of(dx, dy):
    """ボード座標(上が+y)の方向ベクトル → 辺"""
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "N" if dy > 0 else "S"


def rotate_side(side, rot):
    """ボード上の辺 → ダイの辺(ダイを rot 度 反時計回りに回して実装した場合)"""
    k = (int(rot) // 90) % 4
    return SIDES[(SIDES.index(side) + k) % 4]


class Board:
    def __init__(self, spec):
        b = spec["board"]
        pre = PRESETS.get(b.get("preset", "custom"), PRESETS["custom"])
        self.size = b.get("size") or pre["size"]
        self.label = b.get("title") or pre["label"]
        self.size_assumed = "size" not in b
        ids = {p["id"] for p in b.get("parts", [])}
        self.parts = [dict(p, status=p.get("status", "assumed"), preset=True) for p in pre["parts"] if p["id"] not in ids]
        self.parts += [dict(p, status=p.get("status", "assumed")) for p in b.get("parts", [])]
        ch = b.get("chip", {})
        self.chip = dict(id=ch.get("id", "chip"), label=ch.get("label", "SoC"),
                         at=ch.get("at", [self.size[0] * 0.55, self.size[1] * 0.55]),
                         size=ch.get("size", [15, 15]), rot=ch.get("rot", 0),
                         status=ch.get("status", "assumed" if "at" not in ch else "decided"))
        self.nets = b.get("nets", [])
        self.ask = []
        if self.size_assumed:
            self.ask.append(f"ボード外形 {self.size[0]}x{self.size[1]}mm はプリセット値(規格・筐体で確認)")
        if self.chip["status"] != "decided":
            self.ask.append("チップのボード上の位置が仮置き")
        for p in self.parts:
            if p["status"] != "decided" and not p.get("preset"):
                self.ask.append(f"{p['id']}({p.get('label', '')}) の位置が仮置き")

    def part(self, pid):
        return next((p for p in self.parts if p["id"] == pid), None)

    def io_sides(self):
        """各ネットの推奨IO辺"""
        cx, cy = self.chip["at"]
        res = []
        for n in self.nets:
            p = self.part(n["to"])
            if not p:
                res.append(dict(net=n, board_side=None, die_side=None, note=f"部品 {n['to']} が未定義"))
                continue
            px, py = p["at"]
            pw, ph = p.get("size", [6, 6])
            W, H = self.size
            gaps = {"W": px - pw / 2, "E": W - (px + pw / 2), "S": py - ph / 2, "N": H - (py + ph / 2)}
            edge = min(gaps, key=gaps.get)
            # ボード端に接する部品(エッジ端子・ブラケット側コネクタ) → その端の方向にIOを向ける
            bs = edge if gaps[edge] <= p.get("edge_tol", 6) else side_of(px - cx, py - cy)
            res.append(dict(net=n, part=p, board_side=bs, die_side=rotate_side(bs, self.chip["rot"]),
                            dist=math.hypot(px - cx, py - cy), assumed=p["status"] != "decided" or self.chip["status"] != "decided"))
        return res

    def svg(self, title=None):
        W, H = self.size
        s = min(1000 / W, 700 / H)
        pl, pt = 40, 56 if title else 24
        X = lambda x: pl + x * s
        Y = lambda y: pt + (H - y) * s
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W*s+pl+40:.0f} {H*s+pt+70:.0f}" '
             f'font-family="\'Noto Sans JP\',sans-serif"><rect width="100%" height="100%" fill="#fff"/>',
             '<defs><marker id="bah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
             '<path d="M0,0 L10,5 L0,10 z" fill="#2563eb"/></marker></defs>']
        if title:
            o.append(f'<text x="10" y="26" font-size="17" font-weight="800" fill="#1e293b">{esc(title)}</text>')
        dash = ' stroke-dasharray="7 5"' if self.size_assumed else ""
        o.append(f'<rect x="{X(0):.1f}" y="{Y(H):.1f}" width="{W*s:.1f}" height="{H*s:.1f}" rx="6" fill="#ecfdf5" '
                 f'stroke="#047857" stroke-width="2.2"{dash}/>')
        o.append(f'<text x="{X(W)-8:.1f}" y="{Y(H)+18:.1f}" font-size="12" fill="#047857" text-anchor="end" font-weight="700">'
                 f'{esc(self.label)}  {W:g}×{H:g} mm</text>')
        cx, cy = self.chip["at"]
        # 結線(ラッツネスト)
        for r in self.io_sides():
            if not r.get("part"):
                continue
            p = r["part"]
            hw, hh = self.chip["size"][0] / 2, self.chip["size"][1] / 2
            bs = r["board_side"]
            sx, sy = {"N": (cx, cy + hh), "S": (cx, cy - hh), "E": (cx + hw, cy), "W": (cx - hw, cy)}[bs]
            px, py = p["at"]
            pw, ph = p.get("size", [6, 6])
            tx = min(max(sx, px - pw / 2), px + pw / 2) if bs in "NS" else (px - pw / 2 if px > sx else px + pw / 2)
            ty = (py - ph / 2 if py > sy else py + ph / 2) if bs in "NS" else min(max(sy, py - ph / 2), py + ph / 2)
            d = (f"M{X(sx):.1f},{Y(sy):.1f} L{X(sx):.1f},{Y(ty):.1f} L{X(tx):.1f},{Y(ty):.1f}" if bs in "NS"
                 else f"M{X(sx):.1f},{Y(sy):.1f} L{X(tx):.1f},{Y(sy):.1f} L{X(tx):.1f},{Y(ty):.1f}")
            dd = ' stroke-dasharray="6 4"' if r["assumed"] else ""
            o.append(f'<path d="{d}" fill="none" stroke="#2563eb" stroke-width="2.4"{dd} marker-start="url(#bah)" marker-end="url(#bah)"/>')
            lab = r["net"].get("label") or r["net"]["pad"].replace("pad_", "").upper()
            if r["net"].get("bw"):
                lab += f" {r['net']['bw']}"
            mx, my = ((sx + tx) / 2, ty) if bs in "EW" else (sx, (sy + ty) / 2)
            o.append(f'<text x="{X(mx)+(0 if bs in "EW" else 6):.1f}" y="{Y(my)-6:.1f}" font-size="11.5" font-weight="700" fill="#2563eb" '
                     f'text-anchor="{"middle" if bs in "EW" else "start"}" paint-order="stroke" stroke="#ecfdf5" stroke-width="3">{esc(lab)}</text>')
        for p in self.parts:
            pw, ph = p.get("size", [6, 6])
            px, py = p["at"]
            fill, st = COL.get(p.get("kind", "part"), COL["part"])
            dd = ' stroke-dasharray="5 3"' if p["status"] != "decided" else ""
            o.append(f'<rect x="{X(px-pw/2):.1f}" y="{Y(py+ph/2):.1f}" width="{pw*s:.1f}" height="{ph*s:.1f}" rx="2" fill="{fill}" stroke="{st}" stroke-width="1.6"{dd}/>')
            vert = ph * s > pw * s * 2.5 and pw * s < 40
            small = len(str(p.get("label", p["id"]))) * 9 > pw * s
            if small and not vert:
                o.append(f'<text x="{X(px):.1f}" y="{Y(py+ph/2)-6:.1f}" font-size="11" font-weight="700" fill="{st}" text-anchor="middle">{esc(p.get("label", p["id"]))}</text>')
            elif vert:
                o.append(f'<text x="{X(px)+pw*s/2+5:.1f}" y="{Y(py)+4:.1f}" font-size="11" font-weight="700" fill="{st}">{esc(p.get("label", p["id"]))}</text>')
            else:
                o.append(f'<text x="{X(px):.1f}" y="{Y(py)+4:.1f}" font-size="11.5" font-weight="700" fill="{st}" text-anchor="middle">{esc(p.get("label", p["id"]))}</text>')
            if p["status"] != "decided" and not p.get("preset"):
                o.append(f'<text x="{X(px+pw/2)-3:.1f}" y="{Y(py+ph/2)+12:.1f}" font-size="10" font-weight="800" fill="#d97706" text-anchor="end">仮</text>')
        w, h = self.chip["size"]
        o.append(f'<rect x="{X(cx-w/2):.1f}" y="{Y(cy+h/2):.1f}" width="{w*s:.1f}" height="{h*s:.1f}" fill="#1e293b" stroke="#0f172a"'
                 + (' stroke-dasharray="5 3"' if self.chip["status"] != "decided" else "") + "/>")
        o.append(f'<text x="{X(cx):.1f}" y="{Y(cy)+5:.1f}" font-size="13" font-weight="800" fill="#fff" text-anchor="middle">{esc(self.chip["label"])}</text>')
        o.append(f'<path d="M{X(cx-w/2):.1f},{Y(cy+h/2):.1f} l10,0 l-10,10 z" fill="#f59e0b"/>')   # 1ピン/向きマーク
        if self.chip["status"] != "decided":
            o.append(f'<text x="{X(cx+w/2)+4:.1f}" y="{Y(cy+h/2)+12:.1f}" font-size="10" font-weight="800" fill="#d97706">仮</text>')
        sb = 10 * s
        o.append(f'<rect x="{X(0):.1f}" y="{Y(0)+26:.1f}" width="{sb:.1f}" height="5" fill="#1e293b"/>'
                 f'<text x="{X(0)+sb+6:.1f}" y="{Y(0)+32:.1f}" font-size="11">10 mm</text>'
                 f'<text x="{X(0)+sb+60:.1f}" y="{Y(0)+32:.1f}" font-size="11" fill="#64748b">破線・「仮」= 仮置き / 矢印 = チップIOの出入り方向</text></svg>')
        return "".join(o)


def check(board, diagram=None, floorplan=None):
    """ボード推奨辺 vs 論理図パッド vs フロアプランPHY"""
    dpads = {p["id"]: p for p in (diagram or {}).get("pads", [])}
    fpb = {b["id"]: b for b in (floorplan or {}).get("floorplan", {}).get("blocks", [])}
    rows, bad = [], []
    for r in board.io_sides():
        n = r["net"]
        want = r["die_side"]
        dp = dpads.get(n["pad"])
        fb = fpb.get(n.get("fp")) if n.get("fp") else None
        ds = dp.get("side") if dp else None
        fs = fb.get("side") if fb else None
        ok = all(x in (None, want) for x in (ds, fs))
        rows.append(dict(pad=n["pad"], to=n["to"], board=want, diagram=ds, floorplan=fs, ok=ok, assumed=r.get("assumed")))
        if not ok:
            bad.append(f"{n['pad']}: ボードからは {want} 辺 / 論理図 {ds} / フロアプラン {fs}")
    return rows, bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="board_out")
    ap.add_argument("--diagram")
    ap.add_argument("--floorplan")
    ap.add_argument("--apply", action="store_true", help="論理図/フロアプランの side をボード推奨に更新")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    spec = yaml.safe_load(open(a.spec, encoding="utf-8"))
    bd = Board(spec)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "board.svg")
    open(path, "w", encoding="utf-8").write(bd.svg(spec["board"].get("title") or "ボード配置"))
    if a.png:
        from chipdiag import to_png
        to_png(path)
    dia = yaml.safe_load(open(a.diagram, encoding="utf-8")) if a.diagram else None
    fp = yaml.safe_load(open(a.floorplan, encoding="utf-8")) if a.floorplan else None
    rows, bad = check(bd, dia, fp)
    print(f"ボード図: {path}")
    print("IO辺の突き合わせ (ボード推奨 / 論理図 / フロアプラン):")
    for r in rows:
        mark = "OK " if r["ok"] else "NG "
        print(f"  {mark}{r['pad']:12s} → {r['to']:10s} ボード:{r['board']}  論理図:{r['diagram']}  FP:{r['floorplan']}"
              + ("  (仮置き由来)" if r["assumed"] else ""))
    if a.apply:
        changed = []
        if dia:
            for p in dia.get("pads", []):
                r = next((x for x in rows if x["pad"] == p["id"]), None)
                if r and r["board"] and p.get("side") != r["board"] and p.get("status") != "decided":
                    changed.append(f"論理図 {p['id']}: {p.get('side')} → {r['board']}")
                    p["side"] = r["board"]
                    if "at" in p:   # 座標固定の図は辺だけ変えても位置が合わない
                        changed[-1] += "（座標指定の図: at を削除して自動配置に戻すこと）"
            yaml.safe_dump(dia, open(a.diagram, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False,
                           default_flow_style=None, width=110)
        if fp:
            for b in fp["floorplan"].get("blocks", []):
                n = next((n for n in spec["board"].get("nets", []) if n.get("fp") == b["id"]), None)
                r = next((x for x in rows if n and x["pad"] == n["pad"]), None)
                if r and r["board"] and b.get("side") != r["board"] and not b.get("at"):
                    changed.append(f"フロアプラン {b['id']}: {b.get('side')} → {r['board']}")
                    b["side"] = r["board"]
            yaml.safe_dump(fp, open(a.floorplan, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False,
                           default_flow_style=None, width=110)
        print("更新:\n  " + ("\n  ".join(changed) if changed else "なし"))
    elif bad:
        print("食い違い:\n  " + "\n  ".join(bad))
    if bd.ask:
        print("仮置き・要確認:\n  " + "\n  ".join(bd.ask))
