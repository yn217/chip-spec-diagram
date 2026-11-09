#!/usr/bin/env python3
"""gallery — README 用の図の見本を作る(サンプルから全種類の図を作り直し、一覧画像と個別画像を docs/gallery/ に置く)

  python3 scripts/gallery.py              # docs/gallery/overview.png と docs/gallery/<種類>.png
  python3 scripts/gallery.py --width 1600 # 個別画像の幅(px)

必要: PNG 出力(rsvg-convert か cairosvg)と Pillow、日本語フォント(Noto Sans CJK など)。
"""
import os, sys, subprocess, shutil, tempfile, argparse, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EX = os.path.join(ROOT, "examples")
PY = sys.executable

# (名前, 一覧の見出し, 一言, コマンド, 出力ファイル)
ITEMS = [
    ("bus_architecture", "バス構成図", "M/S・バス幅・帯域・CDC・メモリ",
     ["chipdiag.py", "render", "npu_soc.yaml", "--png"], "00_structure.png"),
    ("cpu_cache_multiport", "CPU・キャッシュ・多ポート DRAM", "I$/D$ → L2 → L3、DRAM の 3 ポート",
     ["chipdiag.py", "render", "multiport_dram.yaml", "--png"], "00_structure.png"),
    ("memory_map", "メモリマップ", "バスの色・マスタごとの到達性",
     ["chipdiag.py", "render", "product_soc.yaml", "--png"], "30_memmap.png"),
    ("latency_step", "動作とレイテンシの目安", "CPU → AXI → AHB → APB のサイクル数",
     ["chipdiag.py", "render", "latency_demo.yaml", "--png"], "11_step1.png"),
    ("storyboard", "ストーリーボード", "同じ図を使い回してステップを並べる",
     ["storyboard.py", "npu_soc.yaml", "--png"], "storyboard.png"),
    ("block_interface", "ブロック間の接続図", "端子の入出力・バス幅・接続元/先",
     ["ifdiag.py", "render", "interface_npu.yaml", "--png"], "interface.png"),
    ("clock_tree", "クロック系統図", "PLL・切替・分周・歯抜け・CDC",
     ["clocktree.py", "product_soc.yaml", "--png"], "clock_tree.png"),
    ("clock_tree_test", "クロック系統図(テストモード)", "外部のテスト用クロックを素通し",
     ["clocktree.py", "product_soc.yaml", "--png"], "clock_tree_test.png"),
    ("reset_tree", "リセット系統図", "源ごとの列・同期化・解除順・RDC・電源ドロップ",
     ["resettree.py", "product_soc.yaml", "--png"], "reset_tree.png"),
    ("gate_pipeline", "ゲートレベル図", "FF の段をそろえて並べる",
     ["gatediag.py", "render", "gate/pipeline.yaml", "--png"], "pipeline.png"),
    ("gate_clock_mux", "ゲートレベル図(クロック切替)", "IEEE 記号・クロックの幹線",
     ["gatediag.py", "render", "gate/glitchfree_clkmux.yaml", "--png"], "glitchfree_clkmux.png"),
    ("dataflow_floorplan", "データフロー検討図", "辺の端子 → PHY → バス → SRAM・演算器",
     ["dataflowfp.py", "npu_soc.yaml", "--png"], "dataflow_fp.png"),
    ("floorplan", "フロアプラン", "IP の実寸・自動配置・使用率",
     ["floorplan.py", "floorplan.yaml", "--png"], "floorplan.png"),
    ("board", "ボード配置", "コネクタの位置からチップ IO の辺を決める",
     ["board.py", "board.yaml", "--png"], "board.png"),
]
OVERVIEW = ["bus_architecture", "memory_map", "storyboard", "block_interface", "clock_tree", "reset_tree",
            "gate_pipeline", "dataflow_floorplan", "latency_step", "cpu_cache_multiport", "floorplan", "board"]


def font(size, bold=False):
    from PIL import ImageFont
    cands = ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
             "C:/Windows/Fonts/meiryo.ttc"]
    for c in cands:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    try:
        p = subprocess.run(["fc-match", "-f", "%{file}", ":lang=ja"], capture_output=True, text=True).stdout.strip()
        if p:
            return ImageFont.truetype(p, size)
    except OSError:
        pass
    return ImageFont.load_default()


def make(width):
    from PIL import Image
    out = os.path.join(ROOT, "docs", "gallery")
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    done = {}
    for name, title, sub, cmd, fn in ITEMS:
        d = os.path.join(tmp, name)
        args = [PY, os.path.join(HERE, cmd[0])] + cmd[1:] + ["-o", d]
        r = subprocess.run(args, cwd=EX, capture_output=True, text=True)
        src = os.path.join(d, fn)
        if not os.path.exists(src):
            print(f"  失敗: {name}\n{r.stdout[-400:]}{r.stderr[-400:]}")
            continue
        im = Image.open(src).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        p = os.path.join(out, f"{name}.png")
        im.save(p, optimize=True)
        done[name] = (title, sub, p)
        print(f"  {name}.png  {im.width}x{im.height}")
    overview(done, out)
    shutil.rmtree(tmp, ignore_errors=True)


def overview(done, out):
    from PIL import Image, ImageDraw
    cols, TW, TH, CAP, PAD = 3, 620, 400, 64, 22
    names = [n for n in OVERVIEW if n in done]
    rows = (len(names) + cols - 1) // cols
    HEAD = 96
    W = cols * TW + (cols + 1) * PAD
    H = HEAD + rows * (TH + CAP) + (rows + 1) * PAD
    img = Image.new("RGB", (W, H), "#f1f5f9")
    dr = ImageDraw.Draw(img)
    dr.text((PAD, 22), "chip-spec-diagram で描ける図", font=font(34, True), fill="#0f172a")
    dr.text((PAD + 2, 66), "YAML(またはRTL)から、仕様書に載せる図とチェック結果を作る", font=font(17), fill="#475569")
    for i, n in enumerate(names):
        r, c = divmod(i, cols)
        x = PAD + c * (TW + PAD)
        y = HEAD + PAD + r * (TH + CAP + PAD)
        dr.rounded_rectangle([x, y, x + TW, y + TH + CAP], radius=12, fill="#ffffff", outline="#cbd5e1", width=2)
        im = Image.open(done[n][2]).convert("RGB")
        ratio = (TH - 16) / (TW - 20)
        if im.height / im.width > ratio * 1.25:          # 縦に長い図は上の方を切り出す(小さくなりすぎないように)
            im = im.crop((0, 0, im.width, int(im.width * ratio * 1.1)))
        k = min((TW - 20) / im.width, (TH - 16) / im.height)
        th = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
        img.paste(th, (x + (TW - th.width) // 2, y + 10 + (TH - 16 - th.height) // 2))
        dr.line([x + 12, y + TH, x + TW - 12, y + TH], fill="#e2e8f0", width=2)
        dr.text((x + 16, y + TH + 8), done[n][0], font=font(21, True), fill="#0f172a")
        dr.text((x + 16, y + TH + 37), done[n][1], font=font(15), fill="#64748b")
    p = os.path.join(out, "overview.png")
    img.save(p, optimize=True)
    print(f"  overview.png  {W}x{H}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1600)
    a = ap.parse_args()
    make(a.width)
