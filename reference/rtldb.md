# RTL の接続データベース(rtldb.py)

RTL の接続を SQLite のデータベースにし、図(ブロック間の接続図・クロック/CDC の表)と検査の元にする。

```
python3 scripts/rtldb.py build -o design.db --top TOP -I incdir a.v b.sv ...   # RTL → DB
python3 scripts/rtldb.py analyze design.db -o out/ --png [--static '正規表現']  # クロック・CDC → cdc.md, cdc_map.svg
python3 scripts/rtldb.py iface design.db --inst top.u_x -o iface.yaml          # → ifdiag.py render iface.yaml
```
必要: `pip install pyslang`(slang。SystemVerilog 2017 のエラボレータ)。

## 方式
1. **エラボレート**: slang でパラメータ・generate・マクロ・インスタンス配列を展開する。見つからないモジュールはブラックボックス。
2. **節点 = 信号**: 端子・ネット・変数を階層パス(`top.u_a.u_b.sig`)で一意にする。信号単位(ビット単位ではない)。
3. **辺 = 駆動**(`dst` ← `src`):

   | kind | 元の記述 | 付ける印 |
   |---|---|---|
   | comb | assign / always @* / ゲート / `wire x = 式` | role(data = 右辺、ctrl = if・case の条件・添字)、direct(論理を通らない) |
   | seq | クロックのエッジで叩く代入(dst は FF) | 同上。FF のクロック信号とエッジは `ff` 表 |
   | conn | インスタンスの端子接続 | direct |
   | arst | 非同期リセット(イベントにあり、先頭の if の条件に出る信号) | |
4. **クロックの源**: FF のクロック信号から駆動を逆にたどる。
   - トップの入力 = primary、ブラックボックスの出力 = blackbox(PLL など)、FF の出力 = generated(分周)
   - 合流点でクロックらしい源が 1 本(残りはイネーブル)= ゲート → 元のドメインのまま
   - クロックらしい源が 2 本以上 = mux(切替)→ 別のドメイン
   - generated は元のドメインと同じ「系」(同期)とし、CDC にしない
5. **CDC**: FF ごとに、入力の論理の円錐を FF・入力まで逆にたどり(メモ化)、系の違う FF があれば乗り換え。受け側の形で分類:

   | 分類 | 条件 | 扱い |
   |---|---|---|
   | sync | 1 本を論理なしで受け、同じドメインの次の FF へ直結(2 段以上)。`if (x) q<=1; else q<=0;` の形も同じ | 同期化あり |
   | sync_gray | 同上で多ビット、名前に gray | 同期化あり |
   | sync_bus | 同上で多ビット | 要確認(グレイかハンドシェイクか) |
   | data_enable | 受け側ドメインの条件で取り込むデータ | 要確認(イネーブルが同期化されているか) |
   | mem | 別クロックで書くメモリを読む(非同期 FIFO の RAM) | 要確認(ポインタの同期化) |
   | sync_logic | 同期化の前に組み合わせ論理 | 要対策(グリッチ) |
   | sync_tap | 1 段目の出力を次の FF 以外でも使用 | 要対策 |
   | no_sync | 上のどれでもない | 要対策 |
   | static | `--static` で準静的と宣言した送り側(動作中に変えない設定レジスタ) | 対象外 |

## 表
`instance`(階層)/ `signal`(節点)/ `edge`(駆動)/ `ff`(クロック・ドメイン)/ `clock`(ドメイン・種類・系)/ `cdc`(乗り換え)/
ビュー `conn`(ブロック間の端子接続)。SQL でそのまま引ける:
```sql
SELECT src_inst, src_port, dst_inst, dst_port, width FROM conn WHERE dst_inst='top.u_npu';
SELECT d.path, c.class FROM cdc c JOIN signal d ON d.id=c.dst WHERE c.class='no_sync';
```

## 検証(2026-11)
| 対象 | 規模 | 結果 |
|---|---|---|
| `examples/rtl/cdc_cases.v`(答えのわかる 11 通り) | — | 全て期待どおり(同期化・グレイ・多ビット・グリッチ・1 段目使用・メモリ・イネーブル・分周・ゲート・切替) |
| OpenCores ethmac(Verilog, 約 1.2 万行) | 66 インスタンス / FF 398 | ドメイン 3(wb・mtx・mrx)を検出。設定レジスタを準静的と宣言後、要対策 59・要確認 10・同期化あり 20。ソースと照合した例: 同期化の前に論理(`PauseTimerEq0_sync1 <= ~|PauseTimer`)、1 段目の使用(`ShiftEndedSync1 & ~ShiftEndedSync2`)、ループバックで tx → rx |
| verilog-ethernet Arty の fpga_core(約 4.6 万行を読み込み) | 41 インスタンス / FF 625 | ドメイン 3。非同期 FIFO のグレイポインタ 4・メモリ 2・コミットのハンドシェイク 2・リセット/状態の同期化 15、要対策 0 |
| Ibex(SystemVerilog) | 37 インスタンス / FF 115 | ドメイン 1(クロックゲートは同じドメイン)、乗り換え 0(誤検出なし) |
| ethmac ×60(負荷) | 3961 インスタンス / 信号 9.3 万 / 辺 35 万 | 抽出 4 秒 + 解析 1.2 秒、DB 37MB |

FF の数は信号単位(バスは 1 つ)。

## 限界
- 信号単位なので、1 本のバスのビットごとにドメインや用途が違う場合はまとめて扱う(構造体のメンバも同じ)。
- 分類は構造だけで決める。グレイコードは名前で判断。ハンドシェイクの正しさ・再収束(同期化した複数の信号を合わせる)は見ない。
- 分周クロックは元と同期とみなす。入力端子のドメイン(外部からの非同期入力)は未対応。
- ラッチは組み合わせとして扱う。関数の中はたどらず、引数を依存にする。
- リセットの乗り換え(RDC)は辺(arst)を持つだけで、分類はまだない。
