# chip-spec-diagram

半導体チップの仕様書に載せる図を作るハーネスです。Claude が `SKILL.md` の手順に沿って、状況に応じて質問し、図を描き進めます。

- **構想段階**: 質問して決定事項を少しずつ増やします。決まっていないことは「仮」「TBD」として描いて先へ進めます。
- **RTLあり**: RTL を解析して図を起こし、推定に自信がない部分だけを確認します。
- **物理面**: IPの実寸によるフロアプラン、ボード上のコネクタの位置から決めるチップIOの辺、draw.io への換算出力を扱います。

## 描ける図

これまでに描いた図です(サンプルは `examples/`)。

| 図 | 手順 |
|---|---|
| バス構成図・メモリマップ・CPU とキャッシュ・多ポート DRAM・レイテンシ | `diagrams/bus-architecture/GUIDE.md` |
| ストーリーボード | `diagrams/storyboard/GUIDE.md` |
| ブロック間の接続図 | `diagrams/block-interface/GUIDE.md` |
| クロック系統図 | `diagrams/clock-tree/GUIDE.md` |
| リセット系統図 | `diagrams/reset-tree/GUIDE.md` |
| ゲートレベル図 | `diagrams/gate-level/GUIDE.md` |
| データフロー検討図 | `diagrams/dataflow-floorplan/GUIDE.md` |
| CDC 階層表・CDC 階層図・CDC 詳細図(非同期箇所) | `diagrams/cdc/GUIDE.md` |
| フロアプラン・ボード配置 | `diagrams/bus-architecture/GUIDE.md`(G4) |

### バス構成図(製品規模)

CPU のグループごとにバスを分け、バス同士を縦の線で直接つなぐ。APB は低速の周辺、色はメモリマップと共通。

<img src="docs/gallery/product_soc.png" alt="バス構成図(製品規模)" width="820">

### メモリマップ(製品規模)

バス構成図と同じ色。各マスタから全アドレスへ届くか、ループがないかを確認。

<img src="docs/gallery/product_soc_memmap.png" alt="メモリマップ(製品規模)" width="820">

### CPU・キャッシュ・多ポート DRAM

CPU 枠の中に I$/D$、L2 → L3 → バス。CPU・GPU・NPU の各 AXI から DRAM コントローラの 3 ポートへ横につなぐ。

<img src="docs/gallery/multiport_dram.png" alt="CPU・キャッシュ・多ポート DRAM" width="820">

### メモリマップ(多ポート DRAM)

複数のバスから見える DRAM の範囲を、バスの色を並べて示す。

<img src="docs/gallery/multiport_memmap.png" alt="メモリマップ(多ポート DRAM)" width="820">

### CPU とキャッシュの階層

I$・D$ は CPU 枠の中に容量付きで。L2 から I・D の 2 本、複数コアが L3 に集まる。

<img src="docs/gallery/cpu_l1_l2_l3.png" alt="CPU とキャッシュの階層" width="820">

### マクロとバスが多い例

枠を引っ付け、隙間を詰めて、スクロールを減らした配置。

<img src="docs/gallery/stress_soc.png" alt="マクロとバスが多い例" width="820">

### 動作とレイテンシの目安

CPU → AXI → AHB → APB の順に矢印を引き、到着と、リードのデータが CPU に戻るまでのサイクル数を書く。

<img src="docs/gallery/latency_apb_read.png" alt="動作とレイテンシの目安" width="820">

### ストーリーボード(NPU SoC)

同じ構成図を使い回し、ステップごとのコマを同じ縮尺で並べる。

<img src="docs/gallery/storyboard_npu_soc.png" alt="ストーリーボード(NPU SoC)" width="820">

### ストーリーボード(レイテンシ)

縦長の図は縦長のコマを横に並べる。各コマにレイテンシの目安。

<img src="docs/gallery/storyboard_latency_demo.png" alt="ストーリーボード(レイテンシ)" width="820">

### ストーリーボード(製品規模)

大きな図はコマごとに動く所へ寄せ、横長のコマを 1 列に並べる。

<img src="docs/gallery/storyboard_product_soc.png" alt="ストーリーボード(製品規模)" width="820">

### ブロック間の接続図

中心のブロックと、左に接続元・右に接続先。端子の入出力アイコンは枠の辺の上、バス幅も表示。

<img src="docs/gallery/interface_npu.png" alt="ブロック間の接続図" width="820">

### ブロック間の接続図(RTL から)

RTL の接続から起こした例。AXI/APB は束ねて 1 本に。

<img src="docs/gallery/interface_from_rtl.png" alt="ブロック間の接続図(RTL から)" width="820">

### クロック系統図(製品規模)

源 → PLL → クロック切替 → 分周(レジスタ切替・歯抜け)→ ゲート → ドメイン。CDC と比率同期を分けて描く。

<img src="docs/gallery/clock_tree_product_soc.png" alt="クロック系統図(製品規模)" width="820">

### クロック系統図(テストモード)

テストモードで外部のテスト用クロックを素通しする経路。機能の経路は薄く。

<img src="docs/gallery/clock_tree_test_product_soc.png" alt="クロック系統図(テストモード)" width="820">

### クロック系統図(NPU SoC)

PLL ごとの分周とドメイン、乗り換えの一覧。

<img src="docs/gallery/clock_tree_npu_soc.png" alt="クロック系統図(NPU SoC)" width="820">

### リセット系統図(製品規模)

リセット源ごとの列から OR へ。非同期/同期と発行クロック、解除シーケンス、RDC の説明、解除時の電源ドロップ。

<img src="docs/gallery/reset_tree_product_soc.png" alt="リセット系統図(製品規模)" width="820">

### リセット系統図(NPU SoC)

源の列・同期化・ドメイン、右に RDC、下に解除の波形。

<img src="docs/gallery/reset_tree_npu_soc.png" alt="リセット系統図(NPU SoC)" width="820">

### ゲートレベル図(パイプライン)

FF を段ごとに等間隔・同じ高さに並べ、段数(レイテンシ)を数えやすくする。

<img src="docs/gallery/pipeline.png" alt="ゲートレベル図(パイプライン)" width="820">

### ゲートレベル図(クロック切替)

クロックの配線は下の幹線から縦に上げて FF へ。戻りの接続は上を回す。

<img src="docs/gallery/glitchfree_clkmux.png" alt="ゲートレベル図(クロック切替)" width="820">

### ゲートレベル図(リセット同期化)

非同期アサート・同期解除の 2 段同期。リセットは FF の枠の下の丸へ。

<img src="docs/gallery/reset_sync.png" alt="ゲートレベル図(リセット同期化)" width="820">

### ゲートレベル図(クロックゲート)

ICG とイネーブル付きレジスタ。クロック・リセットの配線は省いて端子に網名。

<img src="docs/gallery/icg_enable.png" alt="ゲートレベル図(クロックゲート)" width="820">

### データフロー検討図

どの辺の端子から入り、PHY → バス → どの SRAM・演算器へ届くか。経路ごとの距離と帯域の表。

<img src="docs/gallery/dataflow_fp_npu_soc.png" alt="データフロー検討図" width="820">

### CDC 階層表・CDC 階層図(非同期箇所)
RTL から作ったデータベースを元に、クロックの乗り換えを階層の上に示す。目的は非同期対策を忘れている所を見つけること。
同じ内容を 2 つの形で両方出す。下は階層 9 段の確認用 RTL(`examples/rtl/cdc_deep.sv`)。

**CDC 階層表**(線を描かない。行 = 送り側の階層、列 = 受け側の階層。赤いマスが対策忘れ・誤りの候補)

<img src="docs/gallery/cdc_matrix_deep.png" alt="CDC 階層表" width="820">

**CDC 階層図**(線でつなぐ。赤の太い実線 = 対策なし、赤の一点鎖線 = 対策の誤り、橙の破線 = 要確認、緑の細線 = 対策あり)

<img src="docs/gallery/cdc_hier_deep.png" alt="CDC 階層図" width="820">

### CDC 詳細図
乗り換え 1 本の回路(送り側の FF・同期化の段・取り込みの条件)。下は非同期 FIFO のデータ。

<img src="docs/gallery/cdc_detail_fifo.png" alt="CDC 詳細図" width="820">

`SKILL.md` は共通の手順(質問して決定事項を増やす・ルールの台帳・出す前の検査)と、図の種類ごとの手順への案内です。

## 使い方

- **Claude Code**: このフォルダを `~/.claude/skills/chip-spec-diagram/`（またはプロジェクトの `.claude/skills/`）に置きます。そのうえで「仕様書の図を作りたい」と頼むと、この手順が使われます。
- **Claude アプリ**: このフォルダを zip にし、設定のスキル画面からアップロードします。
- **必要なもの**: python3 と PyYAML。PNG 出力には rsvg-convert または cairosvg も必要です。

## スクリプト

| スクリプト | 役割 |
|---|---|
| `decisions.py` | 決定事項ログ（質問キュー・決定/仮決め/未決・仕様書用の表） |
| `rtl_scan.py` | Verilog/SV の解析（階層・ポート・接続） |
| `rtl2spec.py` | RTL から図YAMLへの変換（AXI/APB/AHB/メモリIF、M/S、バス、メモリ、SDCクロックを推定） |
| `chipdiag.py` | 描画（自動配置・構造/グループ/ステップ/詳細図・lint・SVG/PNG） |
| `layout.py` | 自動配置（バスの上下・左右、SRAMを利用ブロックの隣に置く） |
| `viewer.py` | レビュー用HTML（ステップ・グループ・階層・注記の切替、決定事項表、フロアプラン、ボード） |
| `drawio.py` | draw.io 出力（ステップ・グループをレイヤに分ける） |
| `ip_size.py` | LEF/Liberty/CSV からIPの寸法を取得 |
| `floorplan.py` | 実寸フロアプラン（見積もり・自動配置・重なり/使用率チェック・draw.io換算） |
| `clocktree.py` | クロック系統図(源→PLL→MUX→分周→ゲート→ドメイン、歯抜け・切り替え分周・テストモード、周波数の検算、CDC と比率同期、SDC の下書き) |
| `rules.py` | 作図ルール台帳（指摘をルールとして追加、出す前のチェックリスト、自動化されていないルールの一覧） |
| `board.py` | ボード配置とIOの辺の推奨、論理図・フロアプランとの突き合わせ |
| `resettree.py` | リセット系統図(源・OR・同期化・ドメイン・解除シーケンス・RDC のチェック) |
| `gatediag.py` | ゲートレベル図(標準記号で自動配置・配線、構造記述ネットリストの読み込み、未接続・ループ・ゲーテッドクロック・CDC のチェック) |
| `dataflowfp.py` | データフロー検討図(フロアプラン風の配置に、辺の端子 → PHY → バス → 行き先の流れ、距離・帯域の表) |
| `gallery.py` | サンプルから全種類の図を今の描き方で描き直す(`docs/gallery/regen/` に出す。README の図の差し替え用) |
| `rtldb.py` | RTL の接続を SQLite のデータベースにする(slang でエラボレート)。クロックドメイン・非同期の乗り換え(CDC 15 分類。対策忘れの検出が主目的)・リセットの乗り換え(RDC)の検出、経路の表示(`path`)、答え付きの自己確認(`selftest`)、接続図用 YAML の出力。OpenTitan top_earlgrey(51.5 万行・階層 11 段)で確認。方式は `reference/rtldb.md` |
| `cdcdiag.py` | CDC 階層表(送り側 × 受け側の表。線を描かない)、CDC 階層図(階層 × クロックドメインに乗り換えの線)、CDC 詳細図(乗り換え 1 本の回路 → `gatediag.py`) |
| `storyboard.py` | 動作説明のストーリーボード(構成図を使い回し、ステップごとのコマを 1 枚に並べる) |
| `ifdiag.py` | ブロック間の接続図(YAML または RTL から。AXI/APB の束ね、lint、SVG/PNG/draw.io) |
| `memmap.py` `reach.py` `addrmap.py` | メモリマップの図と表、マスタごとの到達性・ループの検査 |
| `latency.py` | アクセス経路のレイテンシ目安(バス・ブリッジ・CDC・キャッシュ) |

`rules/style_rules.yaml` は作図ルールの台帳です。図への指摘をここに一般化して足していくことで、ハーネスが育ちます。

`examples/` にはサンプルが入っています（RTL、SDC、LEF/Liberty、論理図・フロアプラン・ボードのYAML）。
