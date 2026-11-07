# ブロック間の接続図(インタフェース図)

`$SK` はリポジトリのルート。共通の手順(G0/G1・確認ポリシー・出す前の検査)は `$SK/SKILL.md`。

## どんな図か
- 中心に示したいブロック、**左に接続元・右に接続先**。同じインスタンスが両方なら左右に同じ枠を描く(例: NoC から設定バスが来て、NoC へデータを出す)。
- 各枠の**上側にモジュール名(太字)とインスタンス名(灰色)**。
- **入出力の端子は枠の中に並べ**、**枠の辺の上**に端子の札(矢印形)を置く: 入力 = 内へ向かう札、出力 = 外へ向かう札、双方向 = 両端がとがった札、バス(束)は塗りつぶし。三角だけの記号は使わない(FF のクロック入力に見えるため)。
- **幅**: 中心の枠の端子名の後ろに `[N-1:0]`(束はプロトコルと幅)。線の上に斜線と本数、束は太線にプロトコル名と幅。相手の枠は端子名だけ(幅は線で読む)。
- 線はまっすぐ: 相手の枠を、つながる端子の高さに合わせて置く。1つの出力が複数の相手へ行く時は、2つ目以降の相手の所で縦に振る。
- 固定値(`tie: 0`)は辺の外に短い線と値。

## 手順
1. G0 で中心のブロックを決める(ユーザに聞く。RTL があれば候補のインスタンスを並べて聞く)。
2. **RTL あり**: `python3 $SK/scripts/ifdiag.py rtl <rtlファイル/ディレクトリ> --top <トップ> --center <インスタンス> -o if.yaml`
   - 中心の端子ごとに、同じネットにつながる相手の **出力 → 入力** を探す(同じクロックを受けるだけの仲間は相手にしない)。どのインスタンスにもつながらずトップの端子に出ていれば「(外部端子)」の枠にする。
   - AXI(aw/w/b/ar/r の valid・ready・data…)と APB(psel/penable…)は1つの束にまとめる。valid/psel の向きで master(出力)/slave(入力)、data の幅を束の幅にする。len/last がなければ AXI4-Lite。
   - 束にまとめた推定(プロトコル・向き)はユーザに確認する。
3. **RTL なし**: `if.yaml` を書く(書式は下)。決まっていない端子は仮で置き、決定事項ログに積む。
4. `python3 $SK/scripts/ifdiag.py render if.yaml -o out/ --png` → `interface.svg/png/drawio`。lint を 0 にしてから見せる。

## 書式
```yaml
interface:
  title: NPU(u_npu)の接続
  center:
    module: npu_top
    instance: u_npu
    ports:
      - {name: clk, dir: in}
      - {name: s_axi, dir: in, bus: AXI4-Lite, width: 32}    # 束: bus にプロトコル、width にデータ幅
      - {name: m_axi, dir: out, bus: AXI4, width: 128}
      - {name: dma_req, dir: in, width: 4}
      - {name: dbg_bus, dir: inout, width: 8}
      - {name: test_mode, dir: in, tie: 0}                   # 固定値
  left:    # 接続元(省略可: nets の向きから自動)
    - {module: crg, instance: u_crg, ports: [{name: npu_clk, dir: out}]}
  right:   # 接続先
    - {module: gic, instance: u_gic, ports: [{name: spi_irq, dir: in}]}
  nets:    # 出力 → 入力(束は master → slave)。label: でネット名を線の下に出せる
    - {from: u_crg.npu_clk, to: u_npu.clk}
```

## lint
`[端子]` 定義にない端子 / `[向き]` 出力 → 入力 になっていない / `[幅]` 両端の幅が違う(線をオレンジ)/ `[束]` プロトコルが違う / `[複数駆動]` / `[未接続]` 駆動されない入力(固定値なら `tie:`)/ `[重なり]` 文字の重なり。

## 限界
- RTL の束ねは名前の規則(`<prefix>_awvalid` など)による推定。独自の名前の束は `bus:` を書いて直す。
- SystemVerilog interface 型の端子は `rtl_scan.py` が未対応(警告)。
- 1枚に置く相手は左右それぞれ 10 個程度まで。多い時は端子をグループ(クロック・リセット / 設定バス / データ / 割り込み)で分けて複数枚にする。
