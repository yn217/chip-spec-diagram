# chip-spec-diagram

半導体チップの仕様書に載せる図を作るハーネスです。Claude が `SKILL.md` の手順に沿って、状況に応じて質問し、図を描き進めます。

- **構想段階**: 質問して決定事項を少しずつ増やします。決まっていないことは「仮」「TBD」として描いて先へ進めます。
- **RTLあり**: RTL を解析して図を起こし、推定に自信がない部分だけを確認します。
- **物理面**: IPの実寸によるフロアプラン、ボード上のコネクタの位置から決めるチップIOの辺、draw.io への換算出力を扱います。

## 描ける図

| 図 | 手順 | 例 |
|---|---|---|
| バス構成図(アーキテクチャ)・メモリマップ・到達性/ループ検査 | `diagrams/bus-architecture/GUIDE.md` | `examples/product_soc.yaml` `examples/multiport_dram.yaml` `examples/stress_soc.yaml` |
| ブロック間の接続図(中心のブロックと接続元・接続先、端子の入出力・幅) | `diagrams/block-interface/GUIDE.md` | `examples/interface_npu.yaml` |
| 動作のストーリーボード(ステップ・レイテンシ目安) | バス構成図の G3、`reference/dsl.md` | `examples/latency_demo.yaml` |
| クロック系統図・フロアプラン・ボード配置 | バス構成図の G3.5/G4 | `examples/npu_soc.yaml` `examples/floorplan.yaml` `examples/board.yaml` |
| リセット系統図・ゲートレベル図 | 予定 | — |

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
| `clocktree.py` | クロック系統図（源→PLL→分周/ゲート→ドメイン、CDC一覧） |
| `rules.py` | 作図ルール台帳（指摘をルールとして追加、出す前のチェックリスト、自動化されていないルールの一覧） |
| `board.py` | ボード配置とIOの辺の推奨、論理図・フロアプランとの突き合わせ |
| `ifdiag.py` | ブロック間の接続図(YAML または RTL から。AXI/APB の束ね、lint、SVG/PNG/draw.io) |
| `memmap.py` `reach.py` `addrmap.py` | メモリマップの図と表、マスタごとの到達性・ループの検査 |
| `latency.py` | アクセス経路のレイテンシ目安(バス・ブリッジ・CDC・キャッシュ) |

`rules/style_rules.yaml` は作図ルールの台帳です。図への指摘をここに一般化して足していくことで、ハーネスが育ちます。

`examples/` にはサンプルが入っています（RTL、SDC、LEF/Liberty、論理図・フロアプラン・ボードのYAML）。
