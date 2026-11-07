# 図YAMLの書式

全体のスタイル(任意):
```yaml
style:
  fifo_label: side | over   # FIFOの名前・段数を 記号の隣(既定・接続上) / 記号の上に重ねる(既定・ブロック)
  fifo_scale: 1.0           # 接続上のFIFO記号の大きさ
  abut_shared: true         # APB/AHB(共有バス)にだけぶら下がる周辺の枠を隣と接して並べる(既定 true。バス単位は abut: false)
  abut: true                # クロスバー/NoC でも、同じバスにだけつながるブロックの枠を接して並べる(既定 true)
```

## diagram.yaml(論理図)

```yaml
chip: {label: NPU-SoC}                 # rect: [x,y,w,h] は自動配置なら不要
external:                               # チップ外(データの入口/出口)
  - {id: host, label: Host PC, ports: [{id: p}], status: assumed}
  - {id: dram, label: LPDDR5, sub: 8GB, kind: memory, ports: [{id: p}]}
pads:                                   # チップ境界のIO
  - {id: pad_pcie, label: PCIe Gen4 x4, side: W, bw: 8GB/s}
  - {id: pad_jtag, label: JTAG, side: N, external: none}   # 制御系: 外部要素なしでよい
buses:
  - id: axi
    label: AXI Interconnect
    width: 256                          # バスの枠に「256bit / 1GHz / 32GB/s」と直接表示
    clock_domain: core_clk              # または clock: 1.0GHz
    index_base: 1                       # クロスバー側ポート番号 M1/S1..(RTLに合わせるなら 0)
    arrow: request                      # request(M→S) | both | none
    attach:                             # 順番 = クロスバー側の番号順
      - cpu.m                           # cpu は M → クロスバー側は S1
      - {port: npu.s, bus_port: M4, cdc: "async FIFO"}   # 番号・CDCを明示(CDC枠はクロスバーの中に描く)
      - {port: pcie.m, cdc_in: block}  # ブロック側で乗り換える設計なら、CDC枠をブロックの中に描く
      - {port: sram.s, cdc: false}      # 同期なので CDC 不要
      - {port: ddrc.s, addr: "0x4000_0000-0xFFFF_FFFF"}   # デコード範囲(クロスバー内に表示・メモリマップへ)
      - {port: pcie.m, fifo: 16}        # FIFO(段数)。長方形4つの記号+「FIFO 16段」。CDCもあれば FIFO記号の横に CDC を並べる
  - id: apb                             # 2本目以降も同じように書く(自動で段に分ける)
    label: APB                          # APB/AHB を含むか type: shared → 細い共有バス、スレーブ側は PSEL0..
    type: shared                        # crossbar(既定) | shared
    select_prefix: PSEL                 # 共有バスの選択信号の名前(省略時 APB→PSEL, AHB→HSEL)
    level: 1                            # 段(省略時はブリッジから自動)
    attach: [apbbr.m, uart.s]
clocks:                                 # クロック系統(clocktree.py)。clock_domain から周波数とCDCを自動判定
  sources: [{id: xtal, label: XTAL, freq: 25MHz, kind: osc}, {id: pcie_ref, label: PCIe REFCLK, freq: 100MHz, kind: pad}]
  plls:    [{id: pll_sys, label: SYS PLL, in: xtal, freq: 2.0GHz}]
  domains: [{id: core_clk, label: core, freq: 1GHz, from: pll_sys, div: 2, gate: true, color: "#2563eb"}]
blocks:
  - id: npu
    label: NPU                          # 16字以内
    sub: 32×32 MAC                      # 補足1行(任意)
    clock_domain: npu_clk               # または clock: 1.2GHz
    kind: logic | memory                # memory = SRAM等の専用記号
    status: tbd | assumed               # 未決 / 仮決め(破線+バッジ)
    place: N | S | W | E                # 自動配置ヒント(N=バスの上, S=下, W/E=左右)
    order: 1                            # 行内の順
    ports:
      - {id: m, role: M, width: 128}    # role: M / S(バス接続は必須)。width がバスと違えば線が橙(幅変換)
      - {id: s, role: S, clock_domain: per}   # 端子ごとのクロック(ブリッジ等)。端子間でクロックが違えばブロック内にCDC
      - {id: s, role: S, bw: 32GB/s}    # ポート帯域(バスへの線に表示)
      - {id: phy, dir: inout}           # IO
      - {id: x, side: N, at: 0.3, name: axi_m}   # side/at は自動配置なら省略可。name は詳細図で表示
    count: 4                            # 複数個(斜めに重ねて ×4)
    pack: true                          # 子(多数の周辺)を枠を接した表で並べる。子に addr: "0x3020_0000/64K"
    cols: 4                             # 子を並べる列数(配置ヒント。データの流れ順に1列に並べたい時など)
    # FIFO をブロックとして描く: kind: fifo, depth: 64(段数), cells: 4, flow: x|y(並ぶ向き)
    #   入ってくる結線の端子は入口側(W/N)、出ていく端子は出口側(E/S)に自動で置く
    children:                           # 階層(トップでは箱だけ、詳細図で内部結線)。機能だけの枠は ports なし
      - {id: lbuf, label: Local Buf, sub: 1MB, kind: memory, ports: [{id: a}]}
links:
  - {from: l1i.o, to: l2.i, level: 1, label: I-bus, width: 128, clock: 2GHz}   # 名前付き接続
  - {from: pad_pcie, to: pcie.phy}
  - {from: pad_pcie, to: host.p, dir: both}       # dir: fwd | both
  - {from: lbuf.a, to: pe.a, level: 1, width: 1024, clock: 1.2GHz, fifo: 8}   # level 1 = 詳細図のみ。fifo は受け側ブロック内
  # 矢印は既定で付く(IOは双方向、それ以外は from→to)。dir: none で消す。両端のクロックが違えば CDC 枠(既定は受け側ブロックの中。cdc_in: from で送り側)。cdc: false で抑止
groups:                                 # 半透明の色レイヤ(座標は rect 指定。自動配置時は freeze 後に書く)
  - id: power
    label: 電源ドメイン
    regions: [{label: PD_NPU, color: "#9333ea", blocks: [npu]}]   # 所属ブロックから矩形を自動(rects: 直接指定も可)
scenario:
  steps:
    - title: DMA転送
      master: DMA
      note: CPUがDMAを起動し、DMAがDRAMからLocal Bufへ転送する      # 図の外の表に出る
      flows:
        - {path: [cpu.m, axi, dma.s], style: control, color: "#475569", label: 起動}
        - {path: [dram.p, pad_ddr, ddrc.phy, ddrc.s, axi, npu.s, lbuf.a], color: "#2563eb", label: 重み}
```
### バスを分ける(CPU グループ・用途・速度)
```yaml
buses:
  - {id: noc, label: Main NoC (A53), color: "#2563eb", owner: a53, width: 128, clock_domain: noc,
     attach: [a53.ace, {port: ddrc.s, addr: "0x4000_0000-0xFFFF_FFFF"}, {port: apbbr.s, addr: ["0x3020_0000-0x303A_FFFF", "0x3082_0000/512K"]}]}
  - {id: apb, label: Peripheral APB, type: shared, owner: a53, width: 32, clock_domain: ipg,
     attach: [apbbr.m, {port: uart.s, addr: "0x3086_0000/256K"}]}
  - {id: rt, label: RT Bus (M7), color: "#16a34a", owner: m7, attach: [m7.m, {port: can.s, addr: "0x308C_0000/128K"}]}
bus_links:          # バス間の双方向接続。モジュールは描かず、バスどうしを縦に直接つなぐ線2本(上→下・下→上)。
                    #   両方のバスに端子番号、CDC は受け側のバスの中。実在するブリッジとして箱を描くなら block: true
  - {between: [noc, rt], to_b: ["0x3040_0000/4M", "0x308C_0000/128K"], to_a: default}   # default = その側で他に当たらない全て
blocks:
  - {id: a53, cpu: true, ...}       # cpu: true → どのアドレスに届くか検査([到達性])・到達性の表(memmap.md)
  - {id: can, owner: m7, ...}       # owner → その CPU のバス(owner: が同じバス・一方向ブリッジの先)でなければ [所属]
```
- `addr` は `"0xA-0xB"` / `"0xA/4M"` / `{base, size}` / リスト(複数範囲) / `default`。
- バスの `color` は帯・所属ブロックの上辺・メモリマップで共通(省略時は自動の色)。

### CPU コアとキャッシュ(L1 はコアの枠の中、L2$ → L3$ → バス)
```yaml
blocks:
  - id: cpu
    label: CPU Core
    count: 4
    cpu: true
    attached:            # コアの枠の中(下側)に描く小枠: 名前と容量
      - {id: l1i, label: I$, sub: 32KB, kind: memory, ports: [{id: m, role: M, width: 128}]}
      - {id: l1d, label: D$, sub: 32KB, kind: memory, ports: [{id: m, role: M, width: 128}]}
  - {id: l2, label: L2$, sub: 256KB, count: 4, kind: memory,
     ports: [{id: i, role: S}, {id: d, role: S}, {id: io, role: M}, {id: do, role: M}]}   # I/D それぞれ出力
  - {id: l3, label: L3$, sub: 4MB, kind: memory, ports: [{id: i, role: S}, {id: d, role: S}, {id: m, role: M, width: 128}]}
links:
  - {from: l1i.m, to: l2.i}
  - {from: l1d.m, to: l2.d}
  - {from: l2.io, to: l3.i}
  - {from: l2.do, to: l3.d}
buses: [{..., attach: [l3.m, ...]}]     # バスへは L3$ から(L3 がなければ L2$ から)
```
- バスに出ていないブロックの鎖(コア → L2$ → L3$)を、バスに出るブロックの上に自動で縦に積む(バスが下なら上へ)。線の列はコアの I$/D$ の位置から下へそろう。
- 到達性・レイテンシはコアから D$ → L2$ → L3$ → バスの順に数える。

### 到達性・ループの検査
- バスにマスタ端子で出るブロック(ブリッジ・直接接続は除く。CPU コアは L2 経由)ごとに、全てのスレーブ範囲へ届くかを `[到達性]` で出す。届かなくてよいマスタは `access: [届く先のブロックID]`(検査しないなら `access: none`)。
- 窓どうしでアドレスが回り続ける所は `[ループ]`(例: 両方向とも `default`)。
- memmap.md に「マスタごとの到達性」の表(○ / △ / ×)。

### 複数ポートのスレーブ(DRAM コントローラに AXI が複数)
```yaml
buses:
  - {id: cbus, label: CPU AXI, attach: [cpu.m, {port: ddrc.p0, addr: "0x8000_0000-0xFFFF_FFFF"}]}
  - {id: gbus, label: GPU AXI, attach: [gpu.m, {port: ddrc.p1, addr: "0x8000_0000-0xFFFF_FFFF"}]}
  - {id: nbus, label: NPU AXI, attach: [npu.m, {port: ddrc.p2, addr: "0x8000_0000-0xFFFF_FFFF"}]}
blocks:
  - {id: ddrc, label: DRAM Ctrl, ports: [{id: p0, role: S}, {id: p1, role: S}, {id: p2, role: S}, {id: phy, dir: inout}]}
```
- 複数のバスにスレーブとしてだけつながるブロックは自動で、バスを上から1本ずつ積み、**右端に縦長の枠**を置いて各バスから横に接続する(`place: W` で左端、`place: S` 等で通常の配置)。
- 離れた段のバスどうしの直接接続(例 CPU AXI⇄NPU AXI で間に GPU AXI)は、間の行を右へずらして左端に通り道を作り、縦にそのまま通す。
- 同じブロック・同じ範囲のアドレスはメモリマップで1行にまとめ、経由するバスを色付きで並べる。到達性はどれか1つのポートに届けば○。

### レイテンシの目安(動作図)
```yaml
latency:
  unit: cpu            # 図の数字: cpu(CPU クロック換算・既定) / ns / local(各要素のクロック数)
  cdc: 2               # CDC 1回の同期段数(受け側クロック)
  accesses:            # 1つずつステップになる(要求=実線、戻り=点線、要素ごとに +N、CPU に合計)
    - {from: cpu, to: uart, op: read, title: "CPU → UART(APB)リード"}
buses:  [{..., latency: {req: 2, resp: 1}}]        # 調停・デコード(バスのクロック数)
blocks:
  - {id: x2h, latency: {req: 2, resp: 1}, ...}     # ブリッジ(要求は出口側、応答は入口側のクロック)
  - {id: sram, latency: {read: 2, write: 1}, ...}  # スレーブ
  - {id: cpu, latency: {issue: 1}, ...}            # マスタの発行(任意)
```
- 経路はバスとブリッジから自動で探す(書かなくてよい)。CDC はドメインが変わる所で自動に足す。
- 書いていない値は既定値(AXI 2/1、AHB 1/1、APB 2/0、ブリッジ 1〜2/1、スレーブ 読2/書1)で仮置きし、`[レイテンシ]` 警告と表の「仮」に出る。内訳は `latency.md` とビューアの表。

- `path` の要素: `ブロック.ポート` / パッドID / バスID(バス上を通る)/ ブロックID(中心)。`chipdiag.py anchors` で一覧。
- YAMLの値に `? : # [ ]` を含む時は `"..."` で囲む。

## floorplan.yaml
```yaml
floorplan:
  title: 案A
  die: [6500, 6500] | null      # µm。null → target_util から見積もり
  core_margin: 200
  target_util: 0.55
  libs: [lib]                    # LEF / Liberty / CSV(cell,w_um,h_um)
  drawio: {px_per_mm: 1000}      # draw.io 換算
  blocks:
    - {id: pcie_phy, cell: PCIE_PHY_X4, side: W}     # 辺に沿わせる(長辺を辺に合わせて自動R90)
    - {id: sram, cell: SRAM_SP_32KX128, count: 2}
    - {id: lbuf, cell: SRAM_SP_16KX512, near: npu}   # 近くに置く
    - {id: npu, kind: soft, area_mm2: 7.5, aspect: 1.3}
    - {id: pll, cell: PLL_1G, at: [300, 300], orient: R90}
    - {id: dsp, size: [800, 600]}                    # データシート値
```

## board.yaml
```yaml
board:
  title: PCIeカード案A
  preset: pcie_lp | pcie_fh | m2_2280 | custom   # 外形・エッジ端子(仮の代表値)
  chip: {label: NPU-SoC, at: [95, 38], size: [17, 17], rot: 0}   # mm、ボード左下原点
  parts:
    - {id: sfp, kind: connector, label: SFP+, at: [12, 48], size: [14, 10]}
    - {id: dram0, kind: memory, label: LPDDR5, at: [128, 38], size: [12.4, 15], status: decided}
  nets:
    - {pad: pad_pcie, to: edge, fp: pcie_phy, bw: 8GB/s}   # pad=論理図, fp=フロアプラン
```
ボード端に接する部品はその端の方向、それ以外はチップから見た方向で、IOの辺を推奨する。
