#!/usr/bin/env python3
"""floorplan — IPの実寸でフロアプラン(物理配置)の検討図を作る。

  python3 floorplan.py fp.yaml -o out/ [--drawio] [--px-per-mm 1000] [--png]

入力(fp.yaml の floorplan:)
  unit: um                       座標・寸法は µm、原点はダイ左下(物理座標の慣習)
  die: [w, h] | null             null → マクロ面積と target_util から見積もる
  core_margin: 200               ダイ端からコアまで(IOリング・シールリング)
  target_util: 0.55              見積もり用のマクロ+ソフトブロック使用率
  libs: [path, ...]              LEF / Liberty / CSV (fp.yaml からの相対パス)
  drawio: {px_per_mm: 1000}      draw.io 出力の換算(1mm = 1000px)
  blocks:
    - {id: sram, cell: SRAM_SP_32KX128, count: 2}            ライブラリで寸法を引く
    - {id: npu, kind: soft, area_mm2: 6.0, aspect: 1.2}      ソフトブロック(面積と縦横比)
    - {id: pcie_phy, cell: PCIE_PHY_X4, side: W}             ダイの辺に沿わせる
    - {id: lbuf, cell: SRAM_SP_16KX512, near: npu}           近くに置く
    - {id: pll, cell: PLL, at: [300, 300], orient: R90}      固定配置(左下座標)
    - {id: x, size: [800, 600]}                              寸法を直接指定(データシート値)
寸法が引けないブロックは TBD(仮寸法 500x500)として描き、要確認リストに出す。
"""
import os, sys, math, json, argparse, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ip_size import scan as scan_libs, lookup
from drawio import Doc

HALO = 30   # マクロ間の最小間隔(µm)
COL = {"memory": ("#fff7ed", "#c2410c"), "hard": ("#e0f2fe", "#0369a1"), "soft": ("#f3e8ff", "#7e22ce"),
       "tbd": ("#f8fafc", "#94a3b8"), "io": ("#ecfdf5", "#047857")}
MEMRX = ("SRAM", "RAM", "ROM", "MEM", "RF", "FIFO", "TCM")


def esc(s):
    import html
    return html.escape(str(s), quote=True)


class FP:
    def __init__(self, spec, base_dir="."):
        self.f = spec["floorplan"]
        self.warn, self.ask = [], []
        libs = [os.path.join(base_dir, p) for p in self.f.get("libs", [])]
        self.lib = scan_libs(libs) if libs else {}
        self.blocks = []
        for b in self.f.get("blocks", []):
            n = b.get("count", 1)
            for i in range(n):
                bb = dict(b)
                bb["id"] = b["id"] if n == 1 else f"{b['id']}_{i}"
                if n > 1 and isinstance(b.get("at"), list) and isinstance(b["at"][0], list):
                    bb["at"] = b["at"][i]
                self.blocks.append(self.size(bb))
        self.die()
        self.place()
        self.check()

    # ---- 寸法 ----
    def size(self, b):
        b["status"] = b.get("status", "ok")
        src = ""
        if "size" in b:
            w, h = b["size"]; src = "指定"
            b.setdefault("kind", "hard")
        elif b.get("kind") == "soft" or "area_mm2" in b:
            a = b["area_mm2"] * 1e6
            r = b.get("aspect", 1.0)
            w = math.sqrt(a * r); h = a / w; src = f"面積 {b['area_mm2']}mm² / 縦横比 {r}"
            b["kind"] = "soft"
        elif b.get("cell"):
            rec, sug = lookup(self.lib, b["cell"])
            if rec and rec.get("w"):
                w, h = rec["w"], rec["h"]; src = f"{rec['source'].upper()} {os.path.basename(rec['file'])}"
            elif rec and rec.get("area"):
                w = h = math.sqrt(rec["area"]); src = "Liberty面積のみ(正方形と仮定)"
                b["status"] = "assumed"
                self.ask.append(f"{b['id']}: {b['cell']} は面積 {rec['area']:.0f}µm² のみ。縦横寸法(LEF/データシート)を確認")
            else:
                w = h = 500; b["status"] = "tbd"; src = "不明(仮 500x500)"
                self.ask.append(f"{b['id']}: セル {b['cell']} がライブラリにない" + (f"(候補: {', '.join(sug)})" if sug else ""))
            if not b.get("kind"):
                b["kind"] = "memory" if any(k in b["cell"].upper() for k in MEMRX) else "hard"
        else:
            w = h = 500; b["status"] = "tbd"; src = "寸法情報なし(仮 500x500)"
            self.ask.append(f"{b['id']}: cell / size / area_mm2 のいずれかが必要")
            b.setdefault("kind", "hard")
        if b.get("orient", "R0") in ("R90", "R270", "MXR90", "MYR90"):
            w, h = h, w
        elif "orient" not in b and b.get("side") and ((b["side"] in "WE" and w > h) or (b["side"] in "NS" and h > w)):
            w, h = h, w   # 辺に沿うPHYは長辺を辺に沿わせる
            b["orient"] = "R90"
            self.ask.append(f"{b['id']}: {b['side']}辺に沿わせるため R90 回転を仮定(PHYの回転可否を確認)")
        b["w"], b["h"], b["src"] = float(w), float(h), src
        return b

    def die(self):
        f = self.f
        m = f.get("core_margin", 200)
        total = sum(b["w"] * b["h"] for b in self.blocks)
        self.macro_area = total
        if f.get("die"):
            self.W, self.H = map(float, f["die"])
            self.die_est = False
        else:
            util = f.get("target_util", 0.55)
            side = math.sqrt(total / util)
            self.W = self.H = math.ceil((side + 2 * m) / 10) * 10
            self.die_est = True
            self.ask.append(f"ダイサイズは未指定: 使用率 {util:.0%} から {self.W/1000:.2f} x {self.H/1000:.2f} mm と見積もった")
        self.core = (m, m, self.W - 2 * m, self.H - 2 * m)

    # ---- 配置 ----
    def free(self, x, y, w, h, placed):
        cx, cy, cw, ch = self.core
        if x < cx - 1e-6 or y < cy - 1e-6 or x + w > cx + cw + 1e-6 or y + h > cy + ch + 1e-6:
            return False
        for p in placed:
            if x < p["x"] + p["w"] + HALO and p["x"] < x + w + HALO and y < p["y"] + p["h"] + HALO and p["y"] < y + h + HALO:
                return False
        return True

    def place(self):
        cx, cy, cw, ch = self.core
        placed = []
        for b in self.blocks:
            if b.get("at"):
                b["x"], b["y"] = map(float, b["at"]); b["placed_by"] = "固定"
                placed.append(b)
        for side in "WESN":
            grp = [b for b in self.blocks if b.get("side") == side and "x" not in b]
            if not grp:
                continue
            L = sum((b["h"] if side in "WE" else b["w"]) for b in grp) + HALO * (len(grp) - 1)
            t = ((ch if side in "WE" else cw) - L) / 2
            for b in grp:
                if side == "W":
                    b["x"], b["y"] = cx, cy + t
                elif side == "E":
                    b["x"], b["y"] = cx + cw - b["w"], cy + t
                elif side == "S":
                    b["x"], b["y"] = cx + t, cy
                else:
                    b["x"], b["y"] = cx + t, cy + ch - b["h"]
                t += (b["h"] if side in "WE" else b["w"]) + HALO
                b["placed_by"] = f"辺{side}"
                placed.append(b)
        rest = [b for b in self.blocks if "x" not in b]
        rest.sort(key=lambda b: (1 if b.get("near") else 0, -b["w"] * b["h"]))
        for b in rest:
            cands = [(cx, cy)]
            for p in placed:
                cands += [(p["x"] + p["w"] + HALO, p["y"]), (p["x"], p["y"] + p["h"] + HALO),
                          (p["x"] - b["w"] - HALO, p["y"]), (p["x"], p["y"] - b["h"] - HALO),
                          (p["x"] + p["w"] + HALO, cy), (cx, p["y"] + p["h"] + HALO)]
            tgt = next((p for p in placed if b.get("near") and (p["id"] == b["near"] or p["id"].startswith(b["near"] + "_"))), None)
            if b.get("near") and not tgt:
                self.warn.append(f"{b['id']}: near={b['near']} が未配置/不明")
            ok = [(x, y) for x, y in cands if self.free(x, y, b["w"], b["h"], placed)]
            if not ok:
                b["x"], b["y"] = cx + cw + HALO * 3, cy   # 入らない → ダイの外に置いて警告
                b["status"] = "overflow"
                self.warn.append(f"{b['id']}: コア内に入らない(ダイ拡大か配置見直しが必要)")
            else:
                if tgt:
                    tx, ty = tgt["x"] + tgt["w"] / 2, tgt["y"] + tgt["h"] / 2
                    b["x"], b["y"] = min(ok, key=lambda p: abs(p[0] + b["w"] / 2 - tx) + abs(p[1] + b["h"] / 2 - ty))
                else:
                    b["x"], b["y"] = min(ok, key=lambda p: (p[1] + p[0] * 0.3))
                b["placed_by"] = "自動" + (f"(near {b['near']})" if tgt else "")
            placed.append(b)

    def check(self):
        cx, cy, cw, ch = self.core
        bl = [b for b in self.blocks if b.get("status") != "overflow"]
        for i, a in enumerate(bl):
            if a["x"] < cx - 1e-6 or a["y"] < cy - 1e-6 or a["x"] + a["w"] > cx + cw + 1e-6 or a["y"] + a["h"] > cy + ch + 1e-6:
                self.warn.append(f"{a['id']}: コア領域からはみ出している")
            for b in bl[i + 1:]:
                if a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]:
                    self.warn.append(f"重なり: {a['id']} と {b['id']}")
        self.util = self.macro_area / (cw * ch)
        if self.util > 0.75:
            self.warn.append(f"使用率 {self.util:.0%}: 配線余裕が少ない可能性")

    # ---- 出力 ----
    def summary(self):
        return [f"ダイ {self.W/1000:.2f} × {self.H/1000:.2f} mm" + ("（見積もり）" if self.die_est else ""),
                f"コア {self.core[2]/1000:.2f} × {self.core[3]/1000:.2f} mm",
                f"ブロック面積 {self.macro_area/1e6:.2f} mm²（使用率 {self.util:.0%}）"]

    def svg(self, title=None, show_size=True):
        Wpx = 1000
        s = Wpx / self.W
        pad_l, pad_t, pad_b = 60, 60 if title else 30, 70
        Hpx = self.H * s
        right_extra = max([0] + [(b["x"] + b["w"] - self.W) * s + 40 for b in self.blocks if b.get("status") == "overflow"])
        vw, vh = Wpx + pad_l + 40 + right_extra, Hpx + pad_t + pad_b
        X = lambda x: pad_l + x * s
        Y = lambda y: pad_t + (self.H - y) * s
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw:.0f} {vh:.0f}" font-family="\'Noto Sans JP\',sans-serif">',
             f'<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
             f'<line x1="0" y1="0" x2="0" y2="8" stroke="#7e22ce" stroke-width="1" opacity=".35"/></pattern></defs>',
             f'<rect width="100%" height="100%" fill="#fff"/>']
        if title:
            o.append(f'<text x="10" y="26" font-size="17" font-weight="800" fill="#1e293b">{esc(title)}</text>')
        # mm グリッド・目盛
        step = 1000 if self.W <= 12000 else 2000
        for k in range(0, int(self.W) + 1, step):
            o.append(f'<line x1="{X(k):.1f}" y1="{Y(0):.1f}" x2="{X(k):.1f}" y2="{Y(self.H):.1f}" stroke="#e2e8f0" stroke-width=".8"/>'
                     f'<text x="{X(k):.1f}" y="{Y(0)+16:.1f}" font-size="10" fill="#64748b" text-anchor="middle">{k//1000}</text>')
        for k in range(0, int(self.H) + 1, step):
            o.append(f'<line x1="{X(0):.1f}" y1="{Y(k):.1f}" x2="{X(self.W):.1f}" y2="{Y(k):.1f}" stroke="#e2e8f0" stroke-width=".8"/>'
                     f'<text x="{X(0)-8:.1f}" y="{Y(k)+3:.1f}" font-size="10" fill="#64748b" text-anchor="end">{k//1000}</text>')
        o.append(f'<text x="{X(self.W):.1f}" y="{Y(0)+16:.1f}" font-size="10" fill="#64748b" text-anchor="end" dx="22">mm</text>')
        o.append(f'<rect x="{X(0):.1f}" y="{Y(self.H):.1f}" width="{self.W*s:.1f}" height="{self.H*s:.1f}" fill="none" stroke="#1e293b" stroke-width="2.5"/>')
        cx, cy, cw, ch = self.core
        o.append(f'<rect x="{X(cx):.1f}" y="{Y(cy+ch):.1f}" width="{cw*s:.1f}" height="{ch*s:.1f}" fill="none" stroke="#64748b" stroke-width="1" stroke-dasharray="5 4"/>')
        for b in self.blocks:
            kind = "tbd" if b["status"] in ("tbd", "overflow") else b["kind"]
            fill, st = COL.get(kind, COL["hard"])
            dash = ' stroke-dasharray="6 4"' if b["status"] in ("tbd", "assumed", "overflow") else ""
            bx, by, bw, bh = X(b["x"]), Y(b["y"] + b["h"]), b["w"] * s, b["h"] * s
            o.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{fill}" stroke="{st}" stroke-width="1.6"{dash}/>')
            if kind == "soft":
                o.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="url(#hatch)"/>')
            if kind in ("hard", "memory"):   # 向きマーク(原点角)
                o.append(f'<path d="M{bx:.1f},{by+bh:.1f} l9,0 l-9,-9 z" fill="{st}"/>')
            fs = max(9, min(14, bw / max(len(b["id"]), 4) * 1.4))
            o.append(f'<text x="{bx+bw/2:.1f}" y="{by+bh/2+(0 if show_size else 4):.1f}" font-size="{fs:.1f}" font-weight="700" fill="#1e293b" text-anchor="middle">{esc(b["id"])}</text>')
            if show_size and bh > 30:
                o.append(f'<text x="{bx+bw/2:.1f}" y="{by+bh/2+14:.1f}" font-size="{max(8.5, fs-3):.1f}" fill="#64748b" text-anchor="middle">{b["w"]:.0f}×{b["h"]:.0f}</text>')
            if b["status"] in ("tbd", "assumed"):
                o.append(f'<rect x="{bx+bw-28:.1f}" y="{by+3:.1f}" width="25" height="14" rx="3" fill="#f59e0b"/>'
                         f'<text x="{bx+bw-15.5:.1f}" y="{by+14:.1f}" font-size="9.5" font-weight="800" fill="#fff" text-anchor="middle">{"TBD" if b["status"]=="tbd" else "仮"}</text>')
        # スケールバー(1mm)と注記
        sb = 1000 * s
        y0 = Y(0) + 40
        o.append(f'<rect x="{X(0):.1f}" y="{y0:.1f}" width="{sb:.1f}" height="6" fill="#1e293b"/>'
                 f'<text x="{X(0)+sb+8:.1f}" y="{y0+7:.1f}" font-size="11" fill="#1e293b">1 mm</text>'
                 f'<text x="{X(0)+sb+60:.1f}" y="{y0+7:.1f}" font-size="11" fill="#64748b">{esc(" / ".join(self.summary()))}</text>')
        o.append("</svg>")
        return "".join(o)

    def drawio(self, path, px_per_mm=1000, title=None):
        k = px_per_mm / 1000.0          # px per µm
        doc = Doc()
        pg = doc.page(title or "floorplan")
        base = pg.layer("フロアプラン")
        ann = pg.layer("寸法・注記")
        Y = lambda y, h=0: (self.H - y - h) * k
        pg.rect(base, 0, 0, self.W * k, self.H * k, "", "html=1;fillColor=none;strokeColor=#1e293b;strokeWidth=2.5;")
        cx, cy, cw, ch = self.core
        pg.rect(base, cx * k, Y(cy, ch), cw * k, ch * k, "", "html=1;fillColor=none;strokeColor=#64748b;dashed=1;")
        for b in self.blocks:
            kind = "tbd" if b["status"] in ("tbd", "overflow") else b["kind"]
            fill, st = COL.get(kind, COL["hard"])
            extra = "dashed=1;" if b["status"] in ("tbd", "assumed", "overflow") else ""
            if kind == "soft":
                extra += "fillStyle=hatch;"
            tag = {"tbd": " [TBD]", "assumed": " [仮]", "overflow": " [入らない]"}.get(b["status"], "")
            pg.rect(base, b["x"] * k, Y(b["y"], b["h"]), b["w"] * k, b["h"] * k,
                    f"<b>{esc(b['id'])}</b>{tag}<br><font color='#64748b'>{b['w']:.0f}×{b['h']:.0f} µm</font>",
                    f"rounded=0;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={st};strokeWidth=1.6;{extra}"
                    f"fontSize={max(10, int(14 * k * 1000 / 1000)) if k >= 0.5 else 10};")
        sb = px_per_mm
        pg.rect(ann, 0, self.H * k + 40, sb, 8, "", "html=1;fillColor=#1e293b;strokeColor=none;")
        pg.text(ann, sb + 10, self.H * k + 30, 200, 28, "1 mm", "fontSize=14;")
        upx = 1000 / px_per_mm
        pg.text(ann, 0, self.H * k + 70, 900, 28,
                f"<b>換算: 1 mm = {px_per_mm:g} px（1 px = {upx:g} µm）</b>　座標原点 = ダイ左下", "fontSize=14;")
        pg.text(ann, 0, self.H * k + 100, 1200, 28, " / ".join(self.summary()), "fontSize=13;fontColor=#64748b;")
        xml = doc.xml().replace('gridSize="10"', f'gridSize="{max(1, round(px_per_mm / 10))}"')
        open(path, "w", encoding="utf-8").write(xml)

    def placed_yaml(self):
        out = []
        for b in self.blocks:
            out.append({"id": b["id"], "cell": b.get("cell"), "kind": b["kind"], "status": b["status"],
                        "at": [round(b["x"], 1), round(b["y"], 1)], "size": [round(b["w"], 1), round(b["h"], 1)],
                        "source": b["src"], "placed_by": b.get("placed_by")})
        return {"die": [self.W, self.H], "core": list(self.core), "util": round(self.util, 3),
                "blocks": out, "warnings": self.warn, "to_confirm": self.ask}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="fp_out")
    ap.add_argument("--drawio", action="store_true")
    ap.add_argument("--px-per-mm", type=float, default=None)
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    spec = yaml.safe_load(open(a.spec, encoding="utf-8"))
    fp = FP(spec, os.path.dirname(os.path.abspath(a.spec)))
    os.makedirs(a.out, exist_ok=True)
    title = spec["floorplan"].get("title", "フロアプラン")
    p = os.path.join(a.out, "floorplan.svg")
    open(p, "w", encoding="utf-8").write(fp.svg(title))
    if a.png:
        from chipdiag import to_png
        to_png(p)
    yaml.safe_dump(fp.placed_yaml(), open(os.path.join(a.out, "floorplan_placed.yaml"), "w", encoding="utf-8"),
                   allow_unicode=True, sort_keys=False, width=110)
    ppm = a.px_per_mm or spec["floorplan"].get("drawio", {}).get("px_per_mm", 1000)
    if a.drawio:
        fp.drawio(os.path.join(a.out, "floorplan.drawio"), ppm, title)
    print("\n".join(fp.summary()))
    print(f"出力: {a.out}/floorplan.svg, floorplan_placed.yaml" + (f", floorplan.drawio (1mm={ppm:g}px)" if a.drawio else ""))
    for b in fp.blocks:
        print(f"  {b['id']:14s} {b['w']:8.0f} x {b['h']:<8.0f} @({b['x']:.0f},{b['y']:.0f})  {b['status']:8s} {b['src']}")
    if fp.warn:
        print("警告:\n  " + "\n  ".join(fp.warn))
    if fp.ask:
        print("要確認:\n  " + "\n  ".join(fp.ask))
