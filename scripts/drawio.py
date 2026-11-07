#!/usr/bin/env python3
"""drawio — ブロック図(図YAML)を draw.io 形式で書き出す。

  python3 drawio.py spec.yaml -o diagram.drawio

  - ベース図(構造)を1レイヤ、グループごと・ステップごとに別レイヤ(初期は非表示)
    → draw.io のレイヤ表示切替で「同じ図に矢印を重ねる」をそのまま再現できる
  - 階層の詳細(内部結線)も別レイヤ
座標はレンダラ(chipdiag)と同じ px。実寸図は floorplan.py の --drawio を使う(換算付き)。
"""
import sys, os, html, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def q(s):
    return html.escape(str(s if s is not None else ""), quote=True)


class Doc:
    """mxGraphModel を組み立てる最小ヘルパ"""
    def __init__(self):
        self.pages = []

    def page(self, name):
        p = Page(name)
        self.pages.append(p)
        return p

    def xml(self):
        body = "".join(p.xml(i) for i, p in enumerate(self.pages))
        return f'<mxfile host="chip-spec-diagram" type="device">{body}</mxfile>\n'


class Page:
    def __init__(self, name):
        self.name, self.cells, self.n, self.layers = name, [], 0, []

    def _id(self, pre="c"):
        self.n += 1
        return f"{pre}{self.n}"

    def layer(self, name, visible=True):
        lid = self._id("L")
        self.layers.append(f'<mxCell id="{lid}" value="{q(name)}" parent="0"' + ("" if visible else ' visible="0"') + "/>")
        return lid

    def rect(self, layer, x, y, w, h, label="", style=""):
        cid = self._id()
        self.cells.append(f'<mxCell id="{cid}" value="{q(label)}" style="{style}" vertex="1" parent="{layer}">'
                          f'<mxGeometry x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" as="geometry"/></mxCell>')
        return cid

    def text(self, layer, x, y, w, h, label, style=""):
        return self.rect(layer, x, y, w, h, label,
                         "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;" + style)

    def poly(self, layer, pts, style="", label=""):
        cid = self._id("e")
        mids = "".join(f'<mxPoint x="{x:.2f}" y="{y:.2f}"/>' for x, y in pts[1:-1])
        arr = f'<Array as="points">{mids}</Array>' if mids else ""
        self.cells.append(
            f'<mxCell id="{cid}" value="{q(label)}" style="{style}" edge="1" parent="{layer}">'
            f'<mxGeometry relative="1" as="geometry"><mxPoint x="{pts[0][0]:.2f}" y="{pts[0][1]:.2f}" as="sourcePoint"/>'
            f'<mxPoint x="{pts[-1][0]:.2f}" y="{pts[-1][1]:.2f}" as="targetPoint"/>{arr}</mxGeometry></mxCell>')
        return cid

    def xml(self, i):
        return (f'<diagram name="{q(self.name)}" id="p{i}"><mxGraphModel grid="1" gridSize="10" page="0" '
                f'pageScale="1" math="0" shadow="0"><root><mxCell id="0"/>' + "".join(self.layers) +
                "".join(self.cells) + "</root></mxGraphModel></diagram>")


FONT = "fontFamily=Noto Sans JP;"


def diagram_to_drawio(c, path):
    """トップ + 階層ごとの詳細ページ(自動配置時)を draw.io の複数ページで出す"""
    doc = Doc()
    add_page(doc, c, c.s.get("chip", {}).get("label", "diagram"))
    for name, title, _ in c.detail_pages():
        sc = c._sub_chips.get(name[len("20_detail_"):-4])
        if sc:
            add_page(doc, sc, title)
    open(path, "w", encoding="utf-8").write(doc.xml())
    return path


def add_page(doc, c, name):
    from chipdiag import STYLE, INK, PAST, FIFO_COL, FIFO_FILL
    S = c.s
    pg = doc.page(name)
    base = pg.layer("構造(ベース)")
    detail = pg.layer("詳細: 内部結線", visible=False)
    x, y, w, h = S["chip"]["rect"]
    pg.rect(base, x, y, w, h, S["chip"]["label"],
            f"rounded=1;arcSize=1;whiteSpace=wrap;html=1;fillColor=#fcfcfd;strokeColor={INK};strokeWidth=2.5;"
            f"align=left;verticalAlign=top;spacingLeft=10;fontStyle=1;fontSize=15;{FONT}")
    for bus in c.buses.values():
        bx, by, bw, bh = bus["rect"]
        dash = "dashed=1;" if bus.get("status") in ("tbd", "assumed") else ""
        from chipdiag import bus_perf_text
        perf = bus_perf_text(bus)
        pg.rect(base, bx, by, bw, bh, bus["label"],
                f"rounded=1;arcSize=8;html=1;fillColor=#e2e8f0;strokeColor=#475569;strokeWidth=1.6;{dash}"
                f"align=right;spacingRight=12;fontStyle=1;fontSize=12;{FONT}")
        if perf:
            pg.text(base, bx + 12, by, bw / 2, bh, perf, f"fontSize=12;fontStyle=1;fontColor=#0369a1;{FONT}")
        for a in bus.get("attach", []):
            px, py, jx, jy, vert = c.stub(bus, a)
            role = (c.anch[a][4] or {}).get("role")
            pts = [(px, py), (jx, jy)]
            if role == "S":
                pts = pts[::-1]
            arrow = "endArrow=block;endSize=5;" if role in ("M", "S") else "endArrow=none;"
            pg.poly(base, pts, f"html=1;strokeColor=#475569;strokeWidth=2.4;{arrow}")
    for b in c.blocks.values():
        if not c.visible(b["id"]):
            continue
        if b.get("virtual"):      # バスどうしの直接接続: 箱なし。上下の端子を線でつなぐ
            for u, v in c.virtual_pairs(b):
                if u in c.anch and v in c.anch:
                    pg.poly(base, [c.anch[u][:2], c.anch[v][:2]], "html=1;strokeColor=#475569;strokeWidth=2.4;endArrow=none;")
            continue
        bx, by, bw, bh = b["rect"]
        if b.get("kind") == "fifo" and not b["_ext"]:
            ax = bw >= bh
            for k in range(int(b.get("cells", 4))):
                n = int(b.get("cells", 4))
                if ax:
                    pg.rect(base, bx + bw * k / n, by, bw / n, bh, "", f"html=1;fillColor={FIFO_FILL};strokeColor={FIFO_COL};strokeWidth=1.8;")
                else:
                    pg.rect(base, bx, by + bh * k / n, bw, bh / n, "", f"html=1;fillColor={FIFO_FILL};strokeColor={FIFO_COL};strokeWidth=1.8;")
            dep = f"<br>{b['depth']}段" if b.get("depth") else ""
            pg.rect(base, bx, by, bw, bh, f"<b>{q(b['label'])}</b>{dep}", f"text;html=1;align=center;fontColor={FIFO_COL};"
                    f"labelBackgroundColor=#ffffff;fontSize=13;{FONT}")
            continue
        kind = "ext" if b["_ext"] else ("child" if b["_depth"] > 0 and b.get("kind") != "memory" else b.get("kind", "logic"))
        st = STYLE[kind]
        dash = "dashed=1;" if st["dash"] or b.get("status") in ("tbd", "assumed") else ""
        label = f"<b>{q(b['label'])}</b>" + (f"<br><font color='#64748b' style='font-size:11px'>{q(b['sub'])}</font>" if b.get("sub") else "")
        if b.get("status") in ("tbd", "assumed"):
            label += f"<br><font color='#d97706'><b>{'TBD' if b['status'] == 'tbd' else '仮'}</b></font>"
        valign = "verticalAlign=top;align=right;spacingRight=8;" if b.get("children") else ""
        pg.rect(base, bx, by, bw, bh, label,
                f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;fillColor={st['fill']};strokeColor={st['stroke']};"
                f"strokeWidth={st['sw']};{dash}{valign}fontSize={13 if b['_depth'] else 14};{FONT}")
    for key, (px, py, side, bid, p) in c.anch.items():
        if bid is None or c.blocks.get(bid, {}).get("virtual"):
            continue
        role = p.get("role")
        if role in ("M", "S"):
            fill, fc = (INK, "#ffffff") if role == "M" else ("#ffffff", INK)
            pg.rect(base, px - 7, py - 7, 14, 14, role,
                    f"rounded=0;html=1;fillColor={fill};fontColor={fc};strokeColor={INK};fontSize=9;fontStyle=1;spacing=0;")
        elif p.get("dir") == "inout":
            pg.rect(base, px - 6, py - 6, 12, 12, "", f"rhombus;html=1;fillColor=#ffffff;strokeColor={INK};")
        else:
            pg.rect(base, px - 4, py - 4, 8, 8, "", "rounded=0;html=1;fillColor=#ffffff;strokeColor=#475569;")
        if p.get("name") and c.blocks[bid]["_depth"] > 0:
            pg.text(detail, px + 6, py - 18, 60, 14, p["name"], f"fontSize=10;fontColor=#64748b;{FONT}")
    for p in S.get("pads", []):
        px, py = p["at"]
        pg.rect(base, px - 8, py - 8, 16, 16, "", f"html=1;fillColor=#cbd5e1;strokeColor={INK};strokeWidth=1.6;")
        pg.text(base, px + 10, py - 26, 90, 16, p["label"], f"fontSize=11;fontStyle=1;{FONT}")
    for ln in S.get("links", []):
        if ln["from"] not in c.anch or ln["to"] not in c.anch:
            continue
        pts = c.polyline(c.nodes([ln["from"], ln["to"]]))
        arrow = {"both": "startArrow=block;startSize=5;endArrow=block;endSize=5;",
                 "fwd": "endArrow=block;endSize=5;"}.get(c.link_dir(ln), "endArrow=none;")
        pg.poly(detail if ln.get("level", 0) else base, pts, f"html=1;rounded=0;strokeColor=#475569;strokeWidth=1.6;{arrow}")
    # クロスバー側ポート(番号) と CDC
    for (bid_, a), (brole, lab) in c.bus_ports.items():
        if not lab or a not in c.anch:
            continue
        px, py, jx, jy, vert = c.stub(c.buses[bid_], a)
        fill, fc = (INK, "#ffffff") if brole == "M" else ("#ffffff", INK)
        pg.rect(base, jx - 12, jy - 7, 24, 14, lab,
                f"rounded=0;html=1;fillColor={fill};fontColor={fc};strokeColor={INK};fontSize=9;fontStyle=1;spacing=0;")
    from chipdiag import FIFO_COL, FIFO_FILL
    for x_, y_, vert, lab, _ in c.cdc_points():
        if "FIFO" in lab:   # 長辺を接した4つの長方形 + 隣に名前と段数
            gw, gh = c.fifo_dims(vert)
            for k in range(4):
                if vert:
                    pg.rect(base, x_ - gw / 2, y_ - gh / 2 + gh * k / 4, gw, gh / 4, "", f"html=1;fillColor={FIFO_FILL};strokeColor={FIFO_COL};")
                else:
                    pg.rect(base, x_ - gw / 2 + gw * k / 4, y_ - gh / 2, gw / 4, gh, "", f"html=1;fillColor={FIFO_FILL};strokeColor={FIFO_COL};")
            tx = x_ + gw / 2 + 3
            if lab.startswith("AFIFO"):   # 非同期FIFO: FIFO の横に CDC
                pg.rect(base, tx, y_ - 6.5, c.CDC_W, 13, "CDC",
                        "rounded=0;html=1;fillColor=#ffffff;strokeColor=#dc2626;dashed=1;fontColor=#dc2626;fontSize=9;fontStyle=1;spacing=0;")
                tx += c.CDC_W + 3
                lab = lab[1:]
            pg.text(base, tx + 1, y_ - 7, 90, 14, lab, f"fontSize=9;fontStyle=1;fontColor={FIFO_COL};{FONT}")
            continue
        pg.rect(base, x_ - 15, y_ - 6.5, 30, 13, lab,
                "rounded=0;html=1;fillColor=#ffffff;strokeColor=#dc2626;dashed=1;fontColor=#dc2626;fontSize=9;fontStyle=1;spacing=0;")
    # 注記(幅/クロック/帯域): 接続ごと
    perf = pg.layer("注記: 幅・クロック・帯域")
    from chipdiag import hz, fmt_bw
    for bus in c.buses.values():
        for a in bus.get("attach", []):
            px, py, jx, jy, vert = c.stub(bus, a)
            own = c.owner_clock(a) or {}
            p = c.anch[a][4] or {}
            w = c.port_width(bus, a)
            ck = p.get("clock") or own.get("clock")
            bwt = p.get("bw") or (fmt_bw(w / 8 * hz(ck)) if w and hz(ck) else None)
            t = " ".join(x for x in [f"{w}b" if w else None, f"@{ck}" if ck else None, bwt] if x)
            if not t:
                continue
            if vert:
                my = (py + jy) / 2
                pg.rect(perf, px - 75, my - 8, 150, 16, t, f"text;html=1;rotation=-90;fontSize=10;fontStyle=1;fontColor=#0369a1;"
                        f"align=center;labelBackgroundColor=#ffffff;{FONT}")
            else:
                pg.text(perf, min(px, jx), py - 22, 160, 14, t, f"fontSize=10;fontStyle=1;fontColor=#0369a1;{FONT}")
    for g in S.get("groups", []):
        L = pg.layer(f"グループ: {g['label']}", visible=False)
        for r in g["regions"]:
            for i, (gx, gy, gw, gh) in enumerate(r["rects"]):
                pg.rect(L, gx, gy, gw, gh, r["label"] if i == 0 else "",
                        f"rounded=1;arcSize=4;html=1;fillColor={r['color']};opacity=13;strokeColor={r['color']};"
                        f"dashed=1;strokeWidth=1.8;align=left;verticalAlign=top;spacingLeft=6;fontStyle=1;"
                        f"fontColor={r['color']};textOpacity=100;{FONT}")
    for i, st in enumerate(c.steps, 1):
        L = pg.layer(f"Step {i}: {st['title']}", visible=False)
        n = len(st["flows"])
        for j, fl in enumerate(st["flows"]):
            pts = c.polyline(c.nodes(fl["path"], (j - (n - 1) / 2) * 9))
            col = fl.get("color", "#e11d48")
            ctrl = fl.get("style") == "control"
            pg.poly(L, pts, f"html=1;rounded=0;endArrow=block;endFill=1;strokeColor={col};"
                    f"strokeWidth={2.6 if ctrl else 4.4};{'dashed=1;' if ctrl else ''}fontColor=#ffffff;"
                    f"labelBackgroundColor={col};fontStyle=1;fontSize=11;{FONT}", fl.get("label", ""))
        if st.get("_latency"):    # レイテンシの数字(SVG と同じ位置)
            c._lat_rec = []
            c.svg(cur=i, cumulative=False)
            for x0, y0, x1, y1, t, col, fs in c._lat_rec:
                pg.rect(L, x0, y0, x1 - x0, y1 - y0, q(t),
                        f"rounded=1;arcSize=50;html=1;fillColor=#ffffff;strokeColor={col};strokeWidth=1.6;"
                        f"fontColor={col};fontStyle=1;fontSize={fs:.0f};{FONT}")
            c._lat_rec = None
        pg.text(L, x, y - 30, 600, 24, f"<b>{i}. {q(st['title'])}</b>　{q(st.get('note', ''))}", f"fontSize=13;{FONT}")


if __name__ == "__main__":
    from chipdiag import Chip, load
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="diagram.drawio")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    diagram_to_drawio(c, a.out)
    print(f"draw.io: {a.out}  (ページ: トップ+詳細{len(c.detail_pages())} / レイヤ: ベース・詳細・注記・グループ{len(c.s.get('groups', []))}・ステップ{len(c.steps)})")
