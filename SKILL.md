---
name: chip-spec-diagram
description: 半導体チップの仕様書に載せる図を作るハーネス。バス構成図(アーキテクチャ)・メモリマップ・ブロック間の接続図(端子の入出力・幅)・クロック系統図・リセット系統図・動作のストーリーボード(レイテンシ目安)・データフロー検討図(フロアプラン風)・ゲートレベル図・非同期箇所の図(CDC 階層図)・フロアプラン・ボード配置。仕様が未決なら質問して決定事項を増やしながら仮置きで描き進め、RTLがあれば解析して図を起こす。Use for chip/SoC block diagrams, bus architecture, memory maps, block interface/connection diagrams, clock trees, reset trees, storyboards, dataflow floorplans, gate-level schematics, CDC hierarchy diagrams, floorplans, RTL-to-diagram, draw.io export.
---

# chip-spec-diagram — 仕様書用の図を「質問しながら育てる」ハーネス

このファイルのあるディレクトリを `$SK` とする(スクリプトは `$SK/scripts/`)。
依存: python3 + PyYAML。PNG は rsvg-convert か cairosvg があれば出る。

## 原則(必ず守る)

1. **AIは絵を描かない。図の仕様(YAML)を書く。** 描画・配置・配線はスクリプトが決定的に行う。
2. **ベース図は1枚、動作は重ねる。** 構造の座標はステップ間で動かさない。データフローはステップごとのレイヤ、分類は半透明の色レイヤ。1枚に①②③と全部重ねない。
3. **図の中は短く。** ラベル+補足1行。説明・マスタ名は図の外のステップ表。
4. **データフローに効くものは必ず描く:** チップIOの辺 / チップ外の入口・出口(ホストPC、カメラ、スイッチ、DRAM)/ SRAM の位置(バス共有か演算器ローカルか)/ バス・ポートの Master/Slave(クロスバー側も M1/S1 と番号付き)/ 接続ごとのバス幅・クロック・帯域 / 周波数が変わる箇所の非同期(CDC)。
   接続線には矢印(バス接続は M→クロスバー→S の向き、IO は双方向)。数値は囲み枠を作らず、バスの枠や接続線の脇に直接書く。
5. **止まらない。** 未決は「仮」(assumed) か「TBD」として描いて進め、確認リストに残す。ユーザが「任せる」「わからない」と言ったら仮置きにする。
6. **答えをもらったら、毎回図を更新して見せる。** 質問だけ続けない。

## ユーザの指摘はルールにする(このハーネスの育て方)

ユーザが図の見た目や表現を指摘したら、**その図だけを直して終わりにしない。** 指摘は「今後どの図でも守るべきルール」の例として扱う。
1. 指摘を一般化する(例: 「このCDCを右に」→「CDCは配線の途中に置かずモジュールの中」)。一般化した文を一言で確認してよい。
2. `python3 $SK/scripts/rules.py add --title .. --rule .. --enforce render|lint|review|question --applies .. --origin "指摘の要約"` で台帳(`$SK/rules/style_rules.yaml`)に追加。
3. 機械的に守れるものは描画(chipdiag/layout)か lint に実装する(`rules.py coverage` で目視頼みのルールを把握)。
4. 既存の図を全部描き直して、新しいルールで再検査する。
台帳はチームの財産なので git 管理を勧める。

## 出す前の検査(必ず通す)
1. `chipdiag.py render ... --png` の lint が 0 件(特に `[重なり]` 文字の重なり、`[貫通]` 結線がブロックを横切る、`[線の重なり]`、`[規模]`、`[CDC位置]`、`[アドレス]`、`[perf]`、`[cdc]`、外部接続先なし、SRAM接続先不明)。重なりは `place`/`order`/`width`/結線の向き/`cdc_in` で直す。
2. PNG を開いて目で見る。
3. `rules.py checklist --type architecture|step|detail|clock|floorplan|board` の □ を一つずつ確認。
4. 満たせないルールがあれば、理由を添えてユーザに伝える(黙って出さない)。

## 作業ファイル(ユーザの作業ディレクトリ)

| ファイル | 内容 |
|---|---|
| `diagram_decisions.yaml` | 決定事項ログ(決定/仮決め/未決、根拠) — `decisions.py` で操作 |
| `diagram.yaml` | 論理図の仕様(ブロック・ポート・バス・パッド・外部・シナリオ) — 書式は `$SK/reference/dsl.md` |
| `floorplan.yaml` | フロアプラン(IP実寸) ※作る場合 |
| `board.yaml` | ボード配置(コネクタ・DRAM) ※作る場合 |
| `out/` | SVG/PNG/draw.io/ビューア |

既にあれば読み込んで続きから進める(セッションをまたいでも決定事項ログが状態を持つ)。


## 図の種類(どれを描くか決めてから、その GUIDE を読む)

| 図 | 状態 | 手順 | 主なスクリプト |
|---|---|---|---|
| バス構成図(アーキテクチャ)・メモリマップ・到達性 | 使える | `diagrams/bus-architecture/GUIDE.md` | `chipdiag.py` `layout.py` `memmap.py` `reach.py` |
| ブロック間の接続図(中心のブロックと接続元・接続先、端子の入出力・幅) | 使える | `diagrams/block-interface/GUIDE.md` | `ifdiag.py` |
| 動作のストーリーボード(ステップ・レイテンシ目安) | 使える(バス構成図を使い回してコマを並べる) | `diagrams/storyboard/GUIDE.md` | `storyboard.py`(`chipdiag.py` `latency.py`) |
| クロック系統図(源→PLL→MUX→分周→ゲート→ドメイン、CDC と比率同期) | 使える | `diagrams/clock-tree/GUIDE.md` | `clocktree.py` |
| データフロー検討図(フロアプラン風: 辺の端子 → PHY → バス → SRAM・演算器、距離・帯域) | 使える | `diagrams/dataflow-floorplan/GUIDE.md` | `dataflowfp.py` |
| RTL の接続データベース(接続図の元・クロックドメインと CDC / RDC の検出・経路の表示・自己確認) | 使える | `reference/rtldb.md` | `rtldb.py` |
| CDC 階層図・CDC 詳細図(非同期対策の忘れを見つける: 階層 × クロックドメインに乗り換えの線、1 本ずつの回路。GUIDE の「作業手順」をそのまま実行する) | 使える | `diagrams/cdc/GUIDE.md` | `cdcdiag.py` |
| フロアプラン・ボード配置 | 使える | `diagrams/bus-architecture/GUIDE.md` の G4 | `floorplan.py` `board.py` |
| リセット系統図(源→OR→同期化→ドメイン、解除シーケンス、RDC) | 使える | `diagrams/reset-tree/GUIDE.md` | `resettree.py` |
| ゲートレベル図(記号の回路図、未接続・ループ・ゲーテッドクロック・CDC のチェック) | 使える | `diagrams/gate-level/GUIDE.md` | `gatediag.py` |

ユーザが図の種類を言わなければ、G0 で目的と一緒に聞く。1つの仕様(`diagram.yaml`)から複数の図を描けるものは、名前・クロック・幅を共通にして図どうしで数字をずらさない。

## フェーズと確認ゲート(共通)

### G0 状況判定(最初に1回、必ず確認)
1. 作業ディレクトリを調べる: RTL(`*.v *.sv`)、SDC、LEF/Liberty、既存の `diagram*.yaml` / `diagram_decisions.yaml`。
2. 見つかったものを一言で伝え、AskUserQuestion で **状況** と **目的** を聞く(1回の質問にまとめる):
   - 状況: 構想段階(RTLなし) / RTLあり / 一部RTLあり / 既存の図を更新
   - 目的(複数可): 構造の全体像 / データフロー / 電源・クロック / 内部の詳細(+フロアプラン・ボードは後で聞く)
3. `python3 $SK/scripts/decisions.py init --mode concept|rtl|mixed --project 名前`、目的を `set purpose`。

### G1 質問ラウンド(繰り返し)
1. `decisions.py next -n 4` → 出てきた質問を **AskUserQuestion 1回(最大4問)** で聞く。
   - `why`(図のどこに効くか)を質問文の末尾に短く添える。`confirm: true` はRTL推定の確認。
   - 選択肢が1個以下の自由記述項目は、例を入れた選択肢を2〜3個作る。「仮置きで進める」を必ず入れる。
2. 回答を記録: `decisions.py set ID "値"`、任せる/不明 → `decisions.py assume ID`。回答から新しい論点が出たら `decisions.py add`。
3. **図を更新して見せる**(下の「描画」)。PNGを SendUserFile で送り、次のラウンドへ。
4. 未決が「後回しでよいもの」だけになったら G3 へ。ユーザが「とりあえず全部仮で」と言ったら残りを一括 assume。

### G2 以降
図の種類ごとの GUIDE に従う。

## 確認ポリシー

| 区分 | 例 | 振る舞い |
|---|---|---|
| 止まって確認 | 状況・目的(G0)、トップモジュール、図の範囲、M/S矛盾、`--apply` で既存の値を変える、ユーザのファイルを上書き | AskUserQuestion で聞いてから進む |
| 仮置きで進めて後で確認 | IOの辺、外部接続先、SRAM容量・位置、クロック・帯域、ダイサイズ、IPサイズ不明、ボード部品位置 | `assumed`/`tbd` で描き、次の質問ラウンドに入れる |
| 確認不要 | 確度 high の RTL推定、配色、自動配置、lint で直せる書式 | そのまま進め、最後にまとめて報告 |

ユーザが「どんどん進めて」と言えば仮置きを増やし、「ひとつずつ確認して」と言えば G1 の1ラウンドを小さくする(1〜2問)。

## 限界(ユーザに聞かれたら正直に伝える)
- RTLパーサは軽量版: SystemVerilog interface、複雑な generate、マクロ展開は非対応(警告を出す)。大規模・複雑なら pyslang / `verilator --json-only` の結果に差し替える。
- 自動配置: バスは何本でも段に分けて配置するが、メッシュNoC(格子状)の形は未対応。線の交差は減らす工夫のみで、残る場合は `order`/`place`/端子の `side` で直す。
- ボードのプリセット寸法は代表値(仮)。規格書・筐体図で確認する。
- draw.io 出力は標準の mxGraph XML。レイヤ表示はdraw.ioで切り替える。
