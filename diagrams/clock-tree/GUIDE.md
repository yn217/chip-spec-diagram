# クロック系統図

クロック源 → PLL → 切替(MUX)→ 分周 → クロックゲート → クロックドメイン を左から右へ描く。
右にドメイン間の乗り換え(非同期 CDC / 比率同期)。バス構成図・リセット系統図と同じ `diagram.yaml` を使い、
ドメインの色・名前・周波数を図どうしで揃える。

## 入力(`clocks:`、書式は `reference/dsl.md`)
- `sources` … `kind`: osc(水晶/発振器、チップ外)/ pad(外部 REFCLK)/ rc(チップ内 RC 発振)
- `plls` … `in`・`freq`。`mult`/`refdiv`/`postdiv` を書くと周波数を検算
- `muxes` … クロック切替。`in`(2 本以上)・`default`(通常動作の入力)・`sel`(選択の仕方)
- `domains` … `from`(源・PLL・MUX・別ドメイン)・`div`・`gate`(true / sw / auto)・`freq`・`dvfs`・`pin`(SDC 用の出力ピン)
  - 分周をレジスタで切り替える: `div: {values: [4, 6, 8], default: 6, reg: "CCM_IPG_DIV[1:0]"}`。
    図は「÷4/6/8」の箱の下に既定とレジスタ名、ドメイン枠に設定ごとの周波数(既定は [ ])
  - 歯抜けクロック(パルスを間引く): `gap: 4/5`(5 周期のうち 4 個だけ残す)。図は間引いたパルスの絵、
    枠に「平均 800MHz・パルスの間隔は 1GHz(タイミングはこちら)」
- `test` … テストモード: `mode_pin`(例 TEST_MODE)・`sources`(外部端子のテスト用クロック)・`default`・`case`(他の端子の値)。
  ドメインに `test_from`(省略時 default、`none` ならテスト用なし。理由を `test_note`)
- SDC 用: 源・PLL・MUX・ドメインの `pin`、`jitter`(例 25ps)、`duty`、`latency`
- ブロック・バスに `clock_domain:`

## 手順
1. SDC があれば `create_clock` / `create_generated_clock` から源・分周を読む。なければ質問(question_bank の clocks / clock_tree)。
2. 未決は仮置き(`assumed: true` で「(仮)」表示)。源が未決のドメインは「源 未決」と出る。
3. 生成: `python3 scripts/clocktree.py diagram.yaml -o out/ --png` → `clock_tree.svg`・`clock.md`(ドメイン表・乗り換え表・
   SDC に必要な情報の表)・`clock.sdc`(SDC の下書き)。`test:` があれば `clock_tree_test.svg`・`clock_test.sdc` も出る。
4. lint が OK になるまで直す。PNG で線と文字を確認。リセット系統図の [PLL] チェックもクロックの `from` を使う。

## 乗り換えの種類
- **比率同期(青破線 `1:n`)**: 共通の祖先から分周だけで作り、比が整数。同期の乗り換えで同期化 FF は不要
  (タイミング制約は要る)。書いた周波数が分周比と合わなければ非同期として扱う。
- **非同期(赤破線 `CDC×n`)**: それ以外。PLL が違う・MUX を通る・比が整数でない。同期化 FF/非同期 FIFO が要る。

## チェック(lint)
| タグ | 内容 |
|---|---|
| [周波数] | 源から計算した周波数と書いた周波数の不一致。周波数が範囲・未定の PLL から分周なしで違う周波数のドメインを作っている |
| [未定義] | 存在しない源・PLL・MUX・ドメインの参照 |
| [MUX] | 入力が 2 本未満、default が入力にない |
| [循環] | クロックの接続が循環 |
| [テスト] | テストモードのクロックがないドメイン(test_note なし) |

## 描き方の決まり(R49)
- 列: 源 → PLL → MUX → 分周(÷n)→ ゲート(CG)→ ドメイン。ドメインは源から木をたどる順に並べ、別ドメインから分周するドメインは親のすぐ下。
- 源・PLL・MUX の出力は縦の幹線から各行へ分岐(分岐点に黒丸)。MUX は台形、通常の入力は実線・切替先は破線。
- ドメイン枠の右上に源までの道筋(例: `NoC ← SYS PLL1 ← XTAL`)、入口の上に周波数。DVFS は枠の下に動作点。
- 外部の源は破線でチップ枠の外、枠の辺にパッド。

## テストモード
各ドメインの分周の後ろにテストモード切替(TM、橙の台形)を置き、`mode_pin`=1 で外部端子のテスト用クロックを素通しする。
テスト用クロックは一番下から回して各 TM の下の入力へ(橙)。`clock_tree_test.svg` は機能の経路を薄く、テストの経路を太く描き、
周波数・道筋もテスト時の値にする。クロックゲートは TE=1(開けたまま)。同じテスト用クロックのドメインどうしは同期(CDC なし)。
テスト用クロックのないドメインは [テスト] で知らせる(意図的なら `test_note`)。

## SDC
`clock.sdc` は図と同じ YAML から作る下書き。分からない所は `<...>` で残し、`clock.md` の「SDC に必要な情報」に未決として並べる。
- 源: `create_clock`(周期・端子・デューティ)/ PLL: `create_generated_clock -multiply_by/-divide_by`
- クロック切替: 入力ごとに `-add`、`set_clock_groups -physically_exclusive`
- 分周: `-divide_by`。レジスタで切り替える分周は値ごとに `-add` で定義し排他
- 歯抜け: 平均ではなく元の周期で見る(`-divide_by` は分周だけ)
- 源が違うクロック: `set_clock_groups -asynchronous`(同じ源から分周したものは同じ組)
- ジッタ: `set_clock_uncertainty`
- テストモード(`clock_test.sdc`): `set_case_analysis`、テスト用クロックの `create_clock`、機能クロックも定義(テスト用なしのドメインのため)
- 範囲外: 入出力の遅延(`set_input_delay`/`set_output_delay`)、CDC の個別の例外(同期化 FF の `set_false_path`/非同期 FIFO の `set_max_delay -datapath_only`)は RTL の構造から
