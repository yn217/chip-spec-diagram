#!/usr/bin/env python3
"""milsym — MIL 論理記号の部品集(symbols/mil806b.yaml)を読んで、ゲート記号を描く

  python3 milsym.py catalog -o out/ [--png]   # 部品の一覧(寸法線つき)。規格の寸法図と見比べる
  python3 milsym.py check                     # 外形が寸法どおりかを数値で確かめる

回路図を描くスクリプトからの使い方:
  import milsym
  g = milsym.geom("nand", 3, scale=45)     # → {"w", "h", "pins": {"i0": (dx, dy), …, "y": (dx, dy)}}(px、記号の左上から)
  svg = milsym.svg("nand", x, y, 3, scale=45, stroke="#0f172a", fill="#fff")

・形・寸法はすべて symbols/mil806b.yaml にある。ここには形を書かない(部品集を直せば、どの図も同じように変わる)。
・入力の端子は記号の左端(x = 0)に置き、OR 系は入力線を後ろの円弧まで伸ばして描く(配線は左端で止めればよい)。
・入力が多くて外形の高さに収まらない時は、入力の辺を上下に伸ばす(MIL-STD-806B で許される延長線)。外形の大きさは変えない。
"""
import os, sys, math, re, argparse
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(HERE, "..", "symbols", "mil806b.yaml")
_LIB = None


def lib():
    global _LIB
    if _LIB is None:
        raw = yaml.safe_load(open(LIB_PATH, encoding="utf-8"))
        parts = {}
        def resolve(k):
            p = dict(raw["parts"][k])
            if "like" in p:
                base = resolve(p.pop("like"))
                base = {kk: vv for kk, vv in base.items() if kk not in ("title", "bubble")}
                base.update(p)
                p = base
            return p
        for k in raw["parts"]:
            parts[k] = resolve(k)
        _LIB = dict(raw, parts=parts)
    return _LIB


def kinds():
    return list(lib()["parts"])


def has(kind):
    return kind in lib()["parts"]


def part(kind):
    return lib()["parts"][kind]


def f(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _layout(kind, n):
    """単位の座標で: (全体の奥行き, 全体の高さ, 外形の上端の y, 入力の y の並び, 出力の点)"""
    L, p = lib(), part(kind)
    n = max(1, min(n, p.get("max_in", 99)))
    bw, bh = p["size"]
    sh = p.get("shift", 0)
    pitch, mar = L["pin_pitch"], L["pin_margin"]
    if p.get("max_in") == 1:
        span, need = 0, bh
    else:
        span = (n - 1) * pitch
        need = span + 2 * mar
    H = max(bh, need)
    top = (H - bh) / 2
    ys = [H / 2 + (i - (n - 1) / 2) * pitch for i in range(n)] if n > 1 else [H / 2]
    ox, oy = p["out"]
    ox += sh
    W = bw + sh
    if p.get("bubble"):
        ox += L["bubble_d"]
        W += L["bubble_d"]
    return W, H, top, ys, (ox, oy + top)


def input_x(kind, yb):
    """外形の座標(外形の上端 = 0)の y で、入力線が外形(OR 系は後ろの円弧)に当たる x"""
    e = part(kind)["inputs"]
    bh = part(kind)["size"][1]
    if e["type"] == "line" or yb <= 0 or yb >= bh:
        return e.get("x", 0)
    return e["cx"] + math.sqrt(max(e["r"] ** 2 - (yb - e["cy"]) ** 2, 0))


def geom(kind, n=2, scale=45.0):
    W, H, top, ys, (ox, oy) = _layout(kind, n)
    pins = {f"i{i}": (0.0, y * scale) for i, y in enumerate(ys)}
    pins["y"] = (ox * scale, oy * scale)
    return {"w": W * scale, "h": H * scale, "pins": pins, "top": top * scale, "body_w": (part(kind)["size"][0] + part(kind).get("shift", 0)) * scale}


def svg(kind, x, y, n=2, scale=45.0, stroke="#0f172a", fill="#fff", sw=1.6, stub=True):
    """記号を描く。x, y = 記号の左上(px)。stub: OR 系の入力線を左端から後ろの円弧まで引く"""
    L, p = lib(), part(kind)
    W, H, top, ys, (ox, oy) = _layout(kind, n)
    s = scale
    w_ = sw / s
    sh = p.get("shift", 0)
    o = [f'<g transform="translate({f(x)},{f(y + top * s)}) scale({f(s)})" stroke="{stroke}" stroke-width="{f(w_)}" stroke-linejoin="round">']
    o.append(f'<path d="{p["body"]}" fill="{fill}"' + (f' transform="translate({f(sh)},0)"' if sh else "") + "/>")
    for d in p.get("extra", []):
        o.append(f'<path d="{d}" fill="none"/>')
    bh = p["size"][1]
    if top > 1e-6:                                        # 入力の辺の延長線(上下)
        ex = p["inputs"].get("x", 0) if p["inputs"]["type"] == "line" else 0
        o.append(f'<path d="M{f(ex)},{f(-top)} V0 M{f(ex)},{f(bh)} V{f(bh + top)}" fill="none"/>')
    if stub:
        for yy in ys:
            yb = yy - top
            xb = input_x(kind, yb)
            if xb > 1e-3:
                o.append(f'<path d="M0,{f(yb)} H{f(xb)}" fill="none"/>')
    if p.get("bubble"):
        r = L["bubble_d"] / 2
        o.append(f'<circle cx="{f(ox - r)}" cy="{f(oy - top)}" r="{f(r)}" fill="{fill}"/>')
    o.append("</g>")
    return "".join(o)


# ======================= 確かめる =======================
def check():
    """外形が寸法の控え(dims)どおりかを確かめる → 問題のリスト"""
    bad = []
    for k in kinds():
        p = part(k)
        d = p.get("dims") or {}
        bw, bh = p["size"]
        if "width" in d and abs(p["out"][0] - d["width"]) > 0.01:
            bad.append(f"{k}: 先端の x {p['out'][0]:.3f} が奥行き {d['width']} と合わない")
        if "height" in d and abs(bh - d["height"]) > 1e-6:
            bad.append(f"{k}: 高さ {bh} が {d['height']} と合わない")
        nums = [float(v) for v in re.findall(r"-?\d+\.?\d*", p["body"])]
        if k in ("and",):
            if abs((bw - d["front_r"]) + d["front_r"] - d["width"]) > 1e-6 or abs(2 * d["front_r"] - bh) > 1e-6:
                bad.append(f"{k}: 前の半円(半径 {d['front_r']})が高さ・奥行きと合わない")
        if k == "or":
            tip = d["straight"] + math.sqrt(d["front_r"] ** 2 - (bh / 2) ** 2)
            if abs(tip - p["out"][0]) > 1e-3:
                bad.append(f"or: 前の円弧の先端 {tip:.4f} とパスの先端 {p['out'][0]} が合わない")
            e = p["inputs"]
            corner = e["cx"] + math.sqrt(e["r"] ** 2 - (0 - e["cy"]) ** 2)
            if abs(corner) > 1e-3:
                bad.append(f"or: 後ろの円弧が角 (0, 0) を通らない(x = {corner:.4f})")
        if k == "buf":
            side = math.hypot(bw, bh / 2)
            if abs(side - d["side"]) > 1e-3 or abs(bh - d["side"]) > 1e-6:
                bad.append(f"buf: 三角形の辺 {side:.4f} / {bh} が {d['side']} の正三角形でない")
        if not nums:
            bad.append(f"{k}: body が空")
    if abs(lib()["bubble_d"] - 0.16) > 1e-6:
        bad.append("否定の丸の直径が 0.16 でない")
    return bad


# ======================= カタログ(寸法線つきの一覧)=======================
def catalog(out, png=False):
    sys.path.insert(0, HERE)
    from chipdiag import esc, FONT, to_png
    S = 120                                               # 1 単位 = 120px
    INK, DIM, GRAY = "#0f172a", "#334155", "#64748b"
    o = []
    def arrow_line(x1, y1, x2, y2):
        a = 7
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy) or 1
        ux, uy = dx / n, dy / n
        r = [f'<path d="M{f(x1)},{f(y1)} L{f(x2)},{f(y2)}" stroke="{DIM}" stroke-width="1"/>']
        for (ex, ey, sx, sy) in ((x2, y2, ux, uy), (x1, y1, -ux, -uy)):
            r.append(f'<path d="M{f(ex)},{f(ey)} L{f(ex - sx * a - sy * 2.6)},{f(ey - sy * a + sx * 2.6)} L{f(ex - sx * a + sy * 2.6)},{f(ey - sy * a - sx * 2.6)} z" fill="{DIM}"/>')
        return "".join(r)
    def hdim(x1, x2, y, ext_from, t):
        up = y < ext_from
        r = [f'<path d="M{f(x1)},{f(ext_from)} V{f(y + (-4 if up else 4))} M{f(x2)},{f(ext_from)} V{f(y + (-4 if up else 4))}" stroke="{DIM}" stroke-width=".8"/>',
             arrow_line(x1, y, x2, y)]
        if x2 - x1 > 60:                                  # 長い寸法: 線の中に文字
            r.append(f'<rect x="{f((x1 + x2) / 2 - 18)}" y="{f(y - 9)}" width="36" height="16" fill="#fff"/>'
                     f'<text x="{f((x1 + x2) / 2)}" y="{f(y + 5)}" font-size="14" fill="{INK}" text-anchor="middle">{t}</text>')
        else:                                             # 短い寸法: 線の外(下)に文字
            r.append(f'<text x="{f((x1 + x2) / 2)}" y="{f(y + 18)}" font-size="14" fill="{INK}" text-anchor="middle">{t}</text>')
        return "".join(r)
    def vdim(y1, y2, x, ext_from, t):
        r = [f'<path d="M{f(ext_from)},{f(y1)} H{f(x - 4)} M{f(ext_from)},{f(y2)} H{f(x - 4)}" stroke="{DIM}" stroke-width=".8"/>', arrow_line(x, y1, x, y2)]
        r.append(f'<rect x="{f(x - 9)}" y="{f((y1 + y2) / 2 - 18)}" width="18" height="36" fill="#fff"/>'
                 f'<text x="{f(x + 5)}" y="{f((y1 + y2) / 2)}" font-size="14" fill="{INK}" text-anchor="middle" transform="rotate(-90 {f(x + 5)} {f((y1 + y2) / 2)})">{t}</text>')
        return "".join(r)
    def rlead(cx, cy, r, ang, t):
        """半径の寸法: 中心から円弧へ引出線、文字は線に沿わせて線の上に置く"""
        a = math.radians(ang)
        ex, ey = cx + r * math.cos(a), cy - r * math.sin(a)
        mx, my = cx + (ex - cx) * .5, cy + (ey - cy) * .5
        return (f'<path d="M{f(cx)},{f(cy)} L{f(ex)},{f(ey)}" stroke="{DIM}" stroke-width=".9"/><circle cx="{f(cx)}" cy="{f(cy)}" r="2" fill="{DIM}"/>'
                f'<text x="{f(mx)}" y="{f(my - 4)}" font-size="13" fill="{INK}" text-anchor="middle" transform="rotate({f(-ang)} {f(mx)} {f(my)})">{t}</text>')
    o.append(f'<text x="20" y="32" font-size="20" font-weight="800" fill="{INK}">MIL 論理記号の部品集(symbols/mil806b.yaml)</text>')
    o.append(f'<text x="20" y="54" font-size="12" fill="{GRAY}">MIL-STD-806B の寸法(1.00 = AND の奥行き)。回路図はこの部品を拡大・縮小して使う。寸法線は部品集の数値から描いている(手で描いていない)</text>')
    # 寸法つきの 4 つ(規格の寸法図と同じ並び)
    X0, Y0 = 70, 110
    L = lib()
    # AND
    x, y = X0, Y0 + 30
    o.append(svg("and", x, y, 2, S, INK, "#fff", 2.4, stub=False))
    o.append(hdim(x, x + S, y - 22, y, "1.00"))
    o.append(vdim(y, y + .8 * S, x - 30, x, "0.80"))
    o.append(rlead(x + .6 * S, y + .4 * S, .4 * S, 40, "0.40R"))
    # OR
    x, y = X0 + 230, Y0 + 30
    o.append(svg("or", x, y, 2, S, INK, "#fff", 2.4, stub=False))
    o.append(hdim(x, x + S, y - 22, y, "1.00"))
    o.append(vdim(y, y + .8 * S, x + S + 34, x + S, "0.80"))
    o.append(hdim(x, x + .3 * S, y + .8 * S + 28, y + .8 * S, "0.30"))
    o.append(rlead(x + .3 * S, y + .8 * S, .8 * S, 62, "0.80R"))
    o.append(rlead(x - .6928 * S, y + .4 * S, .8 * S, 20, "0.80R"))
    # BUF
    x, y = X0 + 500, Y0 + 30
    o.append(svg("buf", x, y, 1, S, INK, "#fff", 2.4, stub=False))
    o.append(vdim(y, y + .7 * S, x - 30, x, "0.70"))
    o.append(f'<text x="{f(x + .45 * S)}" y="{f(y + .05 * S)}" font-size="13" fill="{INK}">0.70(3 辺とも)</text>')
    # 丸
    x, y = X0 + 700, Y0 + 50
    r = L["bubble_d"] / 2 * S
    o.append(f'<circle cx="{f(x + r)}" cy="{f(y + r)}" r="{f(r)}" fill="#fff" stroke="{INK}" stroke-width="2.4"/>')
    o.append(vdim(y, y + 2 * r, x + 2 * r + 26, x + 2 * r, "0.16"))
    o.append(f'<text x="{f(x - 6)}" y="{f(y + 2 * r + 28)}" font-size="13" fill="{INK}">否定の丸</text>')
    # 全部の部品(回路図で使う大きさ = 1 単位 45px、2 入力と 4 入力)
    y = Y0 + 220
    o.append(f'<text x="20" y="{y - 14}" font-size="14" font-weight="700" fill="{INK}">回路図で使う大きさ(1 単位 = 45px)。上 = 2 入力、下 = 4 入力(入力の辺を上下に延長。外形の大きさは変えない)。● = 端子</text>')
    x = 40
    for k in kinds():
        p = part(k)
        for row, n in enumerate((2, 4)):
            if p.get("max_in") == 1 and row:
                continue
            yy = y + row * 80
            g = geom(k, n, 45)
            o.append(svg(k, x, yy, n, 45))
            for pn, (dx, dy) in g["pins"].items():
                o.append(f'<circle cx="{f(x + dx)}" cy="{f(yy + dy)}" r="2.2" fill="#dc2626"/>')
        o.append(f'<text x="{f(x)}" y="{f(y + 172)}" font-size="12" font-weight="700" fill="{INK}">{esc(k)}</text>'
                 f'<text x="{f(x)}" y="{f(y + 188)}" font-size="10.5" fill="{GRAY}">{esc(p.get("title", ""))}</text>')
        x += 112
    bad = check()
    o.append(f'<text x="20" y="{y + 216}" font-size="12" fill="{"#dc2626" if bad else "#15803d"}">check: '
             f'{"寸法と外形が合っている(" + str(len(kinds())) + " 部品)" if not bad else " / ".join(bad)}</text>')
    W, H = 960, y + 230
    s = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}"><rect width="100%" height="100%" fill="#fff"/>' + "".join(o) + "</svg>"
    os.makedirs(out, exist_ok=True)
    pth = os.path.join(out, "mil_symbols.svg")
    open(pth, "w", encoding="utf-8").write(s)
    if png:
        to_png(pth)
    return pth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["catalog", "check"])
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    if a.cmd == "catalog":
        print(catalog(a.out, a.png))
    bad = check()
    print("check: OK(%d 部品)" % len(kinds()) if not bad else "check: NG\n  " + "\n  ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
