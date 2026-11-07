#!/usr/bin/env python3
"""storyboard — 動作説明のストーリーボード(1枚に、同じ図を使い回したコマを時間順に並べる)。

各コマは構造図(バス構成図)をそのまま使い回し、そのステップの矢印だけを上から重ねる。
コマは動きのある所を切り出す(既定は全コマ同じ範囲・同じ縮尺。crop: auto でコマごとに寄る)。
前のステップは薄く残せる(cumulative)。コマの下に短い説明、レイテンシの目安があれば合計も。

  python3 storyboard.py spec.yaml -o out/ [--png] [--cols 2] [--crop union|auto|full] [--width 620]
  crop: union(既定)= 全ステップで動く所をまとめた範囲を、どのコマも同じ縮尺で / auto = コマごとに動く所へ寄る / full = 図全体

spec(図の YAML)に:
  scenario:
    title: 推論 1 回の流れ
    steps:
      - {title: 入力の取り込み, master: DMA, note: "...", flows: [{path: [...], label: 入力, color: "#2563eb"}]}
  latency: {accesses: [...]}          # あれば、アクセスごとのコマも並ぶ(要求=実線・戻り=点線・+N)
  storyboard: {cols: 2, crop: auto, width: 620, title: ..., arrows: true}
"""
import sys, os, re, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, load, FONT, INK, GRAY, esc, f, wlen, text_overlaps, to_png

PANEL_HEAD = 34
CAP_LINE = 17
GAP = 34


def wrap(t, n):
    """全角換算 n 文字で折り返す"""
    out, cur, w = [], "", 0
    for ch in str(t or ""):
        cw = 2 if ord(ch) > 0x2E7F else 1
        if w + cw > n * 2 and cur:
            out.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    if cur:
        out.append(cur)
    return out


def step_bbox(c, st, margin=70):
    """そのステップで動く所(矢印の通る点・つながるブロック・バスの区間)を囲む範囲"""
    xs, ys = [], []
    for fl in st.get("flows", []):
        try:
            pts = c.nodes(fl["path"])
        except Exception:
            continue
        for x, y, *_ in pts:
            xs.append(x)
            ys.append(y)
        for a in fl["path"]:
            bid = a.split(".")[0]
            if bid in c.blocks and "rect" in c.blocks[bid] and not c.blocks[bid].get("virtual"):
                x, y, w, h = c.blocks[bid]["rect"]
                xs += [x, x + w]
                ys += [y, y + h]
    if not xs:
        return None
    return [min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin]


def fit_aspect(b, aspect, canvas):
    """コマの縦横比にそろえ、図の外にはみ出さない"""
    x0, y0, x1, y1 = b
    cw, ch = canvas
    w, h = x1 - x0, y1 - y0
    if w / h < aspect:
        d = (h * aspect - w) / 2
        x0, x1 = x0 - d, x1 + d
    else:
        d = (w / aspect - h) / 2
        y0, y1 = y0 - d, y1 + d
    w, h = x1 - x0, y1 - y0
    if w > cw:
        x0, x1 = 0, cw
    else:
        x0 = min(max(x0, 0), cw - w)
        x1 = x0 + w
    if h > ch:
        y0, y1 = 0, ch
    else:
        y0 = min(max(y0, 0), ch - h)
        y1 = y0 + h
    return [x0, y0, x1 - x0, y1 - y0]


def inner(svg):
    """<svg ...>中身</svg> → (viewBox, 中身)"""
    m = re.match(r'<svg[^>]*viewBox="([^"]+)"[^>]*>(.*)</svg>\s*$', svg, re.S)
    return m.group(1), m.group(2)


def build(c, opt):
    steps = c.steps
    if not steps:
        raise SystemExit("ストーリーボード: scenario.steps(または latency.accesses)がない")
    crop = opt.get("crop", "union")
    canvas = c.s["canvas"]
    bbs = [b for b in (step_bbox(c, st) for st in steps) if b]
    ub = ([min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)]
          if bbs else [0, 0, *canvas])
    big = False
    if "crop" not in opt and crop == "union" and float(opt.get("width", 620)) / max(ub[2] - ub[0], ub[3] - ub[1]) < 0.3:
        crop = "auto"     # 大きな図で全コマ共通の範囲だと文字が読めない: コマごとに寄る
        big = True
        c.warn.append("[info][ストーリーボード] 図が大きいため crop: auto(コマごとに拡大)に切り替えた。縮尺はコマごとに異なる")
    if "aspect" in opt:
        aspect = float(opt["aspect"])
    elif crop == "full":
        aspect = canvas[0] / canvas[1]
    elif crop == "union":
        # コマの縦横比は図の動く範囲に合わせる(縦長の図を横長のコマに入れると小さくなる)
        aspect = min(1.8, max(0.55, (ub[2] - ub[0] + 60) / (ub[3] - ub[1] + 60)))
    elif big:
        # 大きな図: 動く範囲は横に長くなりやすいので、横長のコマを 1 列に並べる
        bw = max(b[2] - b[0] for b in bbs); bh = max(b[3] - b[1] for b in bbs)
        aspect = min(3.0, max(4 / 3, (bw + 60) / (bh + 60)))
    else:
        aspect = 4 / 3
    tall = aspect < 1
    cols = int(opt.get("cols", 1 if big else min(len(steps), 4 if tall else 2)))
    PW = float(opt.get("width", 1240 if big else 420 if tall else 620))
    PH = PW / aspect
    # union: 全ステップで動く所をまとめた範囲を、どのコマも同じ切り出し・同じ縮尺で使う(図の使い回し)
    union_view = fit_aspect(ub, aspect, canvas) if bbs else [0, 0, *canvas]
    caps = [wrap(st.get("note", ""), int(PW / 13)) for st in steps]
    caph = max(len(x) for x in caps) * CAP_LINE + 12 if any(caps) else 0
    cell_h = PANEL_HEAD + PH + caph + 10
    rows = (len(steps) + cols - 1) // cols
    W = cols * PW + (cols - 1) * GAP + 40
    title = opt.get("title") or c.s.get("scenario", {}).get("title") or "動作の流れ"
    H = 70 + rows * cell_h + (rows - 1) * GAP + 40
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" width="{f(W)}" height="{f(H)}" '
           f'font-family="{FONT}"><rect width="100%" height="100%" fill="#fff"/>',
           f'<text x="20" y="38" font-size="20" font-weight="800" fill="{INK}">{esc(title)}</text>']
    warn = []
    for i, st in enumerate(steps, 1):
        r, k = divmod(i - 1, cols)
        X = 20 + k * (PW + GAP)
        Y = 64 + r * (cell_h + GAP)
        if crop == "full":
            view = [0, 0, *canvas]
        elif crop == "union":
            view = union_view
        else:
            bb = step_bbox(c, st)
            view = fit_aspect(bb, aspect, canvas) if bb else [0, 0, *canvas]
            if view[2] < canvas[0] * 0.45:          # 拡大しすぎると前後のコマと縮尺が離れる: 図の 45% 幅を下限に
                cx_, cy_ = view[0] + view[2] / 2, view[1] + view[3] / 2
                w_ = canvas[0] * 0.45
                view = fit_aspect([cx_ - w_ / 2, cy_ - w_ / aspect / 2, cx_ + w_ / 2, cy_ + w_ / aspect / 2], aspect, canvas)
        svg = c.svg(view=view, cur=i, cumulative=st.get("cumulative", True))
        for a, b in text_overlaps(svg):
            warn.append(f"[重なり] コマ{i}: 「{a}」と「{b}」")
        vb, body = inner(svg)
        # コマの枠・見出し(番号は小さく。順番は並びで読む)
        out.append(f'<rect x="{f(X)}" y="{f(Y)}" width="{f(PW)}" height="{f(cell_h)}" rx="10" fill="#fff" '
                   f'stroke="#cbd5e1" stroke-width="1.5"/>')
        out.append(f'<circle cx="{f(X + 18)}" cy="{f(Y + 18)}" r="11" fill="{INK}"/>'
                   f'<text x="{f(X + 18)}" y="{f(Y + 22.5)}" font-size="12" font-weight="800" fill="#fff" '
                   f'text-anchor="middle">{i}</text>'
                   f'<text x="{f(X + 36)}" y="{f(Y + 23)}" font-size="14.5" font-weight="800" fill="{INK}">{esc(st["title"])}</text>')
        if st.get("master"):
            out.append(f'<text x="{f(X + PW - 12)}" y="{f(Y + 23)}" font-size="11.5" fill="{GRAY}" '
                       f'text-anchor="end">主体: {esc(st["master"])}</text>')
        out.append(f'<svg x="{f(X + 4)}" y="{f(Y + PANEL_HEAD)}" width="{f(PW - 8)}" height="{f(PH)}" '
                   f'viewBox="{vb}" preserveAspectRatio="xMidYMid meet">{body}</svg>')
        out.append(f'<line x1="{f(X + 10)}" y1="{f(Y + PANEL_HEAD + PH + 2)}" x2="{f(X + PW - 10)}" '
                   f'y2="{f(Y + PANEL_HEAD + PH + 2)}" stroke="#e2e8f0"/>')
        for j, ln in enumerate(caps[i - 1]):
            out.append(f'<text x="{f(X + 14)}" y="{f(Y + PANEL_HEAD + PH + 20 + j * CAP_LINE)}" font-size="12.5" '
                       f'fill="{INK}">{esc(ln)}</text>')
        if opt.get("arrows", True) and i < len(steps):      # 次のコマへの小さな矢印(読む順)
            if k < cols - 1:
                ax, ay = X + PW + 6, Y + cell_h / 2
                out.append(f'<polygon points="{f(ax)},{f(ay - 9)} {f(ax + 20)},{f(ay)} {f(ax)},{f(ay + 9)}" fill="#94a3b8"/>')
    out.append("</svg>")
    return "".join(out), warn


def main():
    ap = argparse.ArgumentParser(description="動作説明のストーリーボード")
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out_story")
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--cols", type=int)
    ap.add_argument("--crop", choices=["union", "auto", "full"])
    ap.add_argument("--width", type=float)
    a = ap.parse_args()
    spec = load(a.spec)
    c = Chip(spec)
    opt = dict(spec.get("storyboard", {}) or {})
    for k in ("cols", "crop", "width"):
        if getattr(a, k):
            opt[k] = getattr(a, k)
    svg, warn = build(c, opt)
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "storyboard.svg")
    open(p, "w", encoding="utf-8").write(svg)
    if a.png:
        to_png(p)
    print(f"生成: {p}")
    warn += [w for w in c.warn if "[info]" not in w and "既定値" not in w]
    for w in c.warn:
        if w.startswith("[info][ストーリーボード]"):
            print("  " + w)
    print("lint:", "OK" if not warn else "")
    for w in warn:
        print("  " + w)


if __name__ == "__main__":
    main()
