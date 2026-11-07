# バス構成図(アーキテクチャ図)・メモリマップ・クロック・フロアプラン

`$SK` はリポジトリのルート(SKILL.md のあるディレクトリ)。共通の手順(G0/G1・確認ポリシー・出す前の検査)は `$SK/SKILL.md`。

### G2 RTL解析(RTLありの時)
```
python3 $SK/scripts/rtl_scan.py <RTLディレクトリ/ファイル> -o rtl.json --tree
```
1. ツリーとトップ候補・警告(interface未対応、generate、定義なし)を要約して伝える。インスタンスが14個を超えるならサブシステム分割か除外を提案。
2. トップ・詳細化するインスタンス・除外を質問(決定事項 `top_module` / `expand` / `exclude`)。
3. 変換と推定レポート:
```
python3 $SK/scripts/rtl2spec.py rtl.json -o diagram.yaml -r inference.yaml --top TOP --expand u_npu [--sdc top.sdc] [--exclude-inst 'dbg|test'] [--bus u_xbar]
python3 $SK/scripts/decisions.py import-rtl inference.yaml
```
   - 確度 high(AXI/APB/AHB の M/S など)は確認せず採用。mid/low(IOの辺、外部接続先、メモリ判定、方向不明、M/S矛盾)は G1 の質問に回る。
   - 推定の誤りはRTLを書き換えずに `diagram.yaml` を直す(role / kind / side / place)。
4. ラベルは読者に合わせる(`audience`): 顧客向けなら機能名に、設計者向けならインスタンス名のまま。

### 構想段階(RTLなし)の diagram.yaml の書き方
決定事項から Claude が書く。**座標は書かない**(自動配置)。配置ヒントだけ: `place: N|S|W|E`、`order`、パッドの `side`。
- 未決のブロック/IF/バスは `status: tbd`、仮決めは `status: assumed`(図で破線+「TBD」「仮」)。
- SRAM は `sram_location` に従い、バス共有なら `buses.attach` に、演算器ローカルなら `children` か演算器と直結の `links` に置く(自動配置が隣に並べる)。
- すべての非制御パッドに `external`(チップ外の相手)を結ぶ。不明なら `status: tbd` の外部要素を置く。JTAG/GPIO等は `external: none`。
- クロックは `clocks:`(源・PLL・ドメイン)を書き、ブロック/バスに `clock_domain` を付ける(周波数・CDC・クロックドメインの色分けが自動で出る)。ドメイン未定なら `clock: 1.2GHz` を直接。
- バス `width: 256`、幅が違うポートは `width: 128`(線が橙=幅変換)。パッド・結線 `bw: 8GB/s`。
- クロスバー側ポート番号は attach の順に M1../S1..(RTLの番号に合わせる時は `index_base: 0` か `{port: cpu.m, bus_port: S0}`)。
- FIFO は接続に `fifo: 16`(段数)。置き場所は CDC と同じ規則(モジュールの中)。CDCと同じ場所(非同期FIFO)は FIFO記号の横に CDC。CDC は引き出し線で遠くに置かない(`[CDC位置]` 警告が出たら接続の間隔を空ける)。FIFO自体を機能ブロックとして描くなら `kind: fifo`。
- 複数個あるブロック(PE Array など)は `count: 4`(斜めに重ねて ×4)。機能を示すだけの枠は ports を書かない(結線のない端子は自動で非表示)。
- グループは `regions: [{label, color, blocks: [id, ...]}]` と所属ブロックで書けば矩形は自動。
- 配置は自動で詰める(枠は文字に合わせた最小、間隔は注記が入る最小)。数が多い時は階層(children)とサブシステム図に分ける。
- CDC は両端のドメイン(なければ周波数)が違えば自動。同期ブリッジ等で不要なら `cdc: false`、方式を書くなら `cdc: "async FIFO"`。
- **CDC枠は必ずモジュールの中に置く**(配線の途中には置かない。実設計でCDC用モジュールを配線の途中に置くことはないため)。バス接続は既定でクロスバーの中(そのポートの内側)、ブロック側で乗り換える設計なら `cdc_in: block`。ブロック間の結線は既定で受け側ブロックの中、送り側なら `cdc_in: from`。どちらで乗り換えるか不明なら質問する。

### 製品規模の図(ブロックが多い・階層が深い・バスが複数)
- **階層ごとのページ**: トップ図は1段下まで。子を持つブロックごとに独立配置の詳細ページが自動で出る(深さ不問)。詳細ページの枠の端子には外側の相手(例 `ace → AXI Main S1`)が書かれる。トップが30ブロックを超える・子が16個を超えるなら `[規模]` 警告 → 中間の階層(サブシステム)を作る。
- **繰り返し構造**: `count: N` で代表1つ+×N。バス側の番号は範囲(S1-4、PSEL1-2)。
- **名前付き接続**: 結線に `label`(例 I-bus、D-bus、snoop)を付けると幅・周波数・帯域と一緒に書く。
- **複数バス**: すべて `buses:` に書けば自動で段に分ける(主バス=先頭 or `level: 0`、ブリッジで辿って段を決める)。AHB/APB は `type: shared`(ラベルにAPB/AHBがあれば自動)。ブリッジは両方のバスに attach するブロックにし、端子ごとに `clock_domain` を付ける(CDC はブリッジの中に描かれる)。
- **多数の周辺**: 1つのブリッジ(AIPS/APB 等)に多数ぶら下がる周辺は、ブリッジのブロックに `pack: true` を付け子として並べる(枠を接した表。各セルに名前・先頭アドレス・サイズ)。
- **アドレス**: インターコネクトのスレーブ側 attach に `addr` を書く(クロスバー内に先頭–末尾を表示)。表のセルは `addr`。`memmap.py` でメモリマップ図・表を作り、重なり・はみ出しは `[アドレス]` 警告。RTL/RDL/デバイスツリー/仕様書のアドレス表があればそこから取る。
- **バスを分ける**(R34-R37): 1本に全部ぶら下げない。CPU グループ(`cpu_groups`)ごとにバスを分け、そのグループが管理するマクロ(`owner:`)を同じバスへ。遅い周辺は APB(R36)。分けたバスは `bus_links` で双方向につなぎ、窓(`to_b`/`to_a`、`default`)を書く。図はモジュールを作らずバスどうしを縦に直接つなぐ線2本(上→下・下→上)。`[到達性]`(リード/ライトを出す**マスタのマクロごと**に全範囲へ届くか)・`[ループ]`(窓どうしで回り続ける)・`[所属]` を 0 にし、memmap.md のマスタごとの到達性の表をユーザに見せる。バスの色はメモリマップと同じ(R37)。
- **CPU コアとキャッシュ**(R42): I$/D$ はコアの `attached`(コアの枠の中の小枠、名前と容量)、そこから L2$(容量)へ結線、L2$ から I/D の出力で L3$(複数コア共有、容量)へ、L3$ からバスへ。
- **DRAM など複数ポートのスレーブ**(R41): CPU / GPU / NPU などのバスから DRAM コントローラへそれぞれ接続がある時は、コントローラにバスごとのスレーブ端子を作り各バスに attach する(自動で右端の縦長の枠・横の接続)。`dram_ports` の質問でポート数と割り当てを確認する。
- **共有バスの周辺は枠を接する**(R40): APB/AHB にだけぶら下がる周辺は間隔 0 で並ぶ(自動)。
- **詰める**(R44): 同じバスのブロックは枠を接し、マスタは上・スレーブは下、pack は 3 行以内、下の段のバスは左の空きへ。大規模の確認は `examples/gen_stress.py`(CPU×8+L2/L3、NoC+2サブバス+AHB+APB、約 60 マクロ)。
- **間隔**(R38): 大きくなってよいが、空きは作らない(下の段のバスごとのまとまりを、上の行のブリッジ・マスタと同じ列にそろえる)。重なりは 0。
- 例に出てくる構造(CPUのキャッシュ、コア数など)は例にすぎない。特定の構造専用の書き方は作らず、上の汎用要素(階層・繰り返し・名前付き接続・複数バス・端子のクロック)で表す。

### 描画(毎ラウンド)
```
python3 $SK/scripts/chipdiag.py render diagram.yaml -o out --png     # 構造・グループ・各ステップ・詳細図
```
- `lint` の警告を読む: M/S未指定、文字数、矢印本数、未定義の経路点、**外部接続先なし、SRAMの接続先不明、[perf]未記入**。警告は質問か修正に変える(ユーザに丸投げしない)。
- PNG を目で確認してから送る(重なり・はみ出し)。崩れていれば `place`/`order`/`side` で直す。どうしても自動配置で収まらない時は `chipdiag.py freeze` で座標を書き出して手で調整。

### G3 シナリオ(データフローを描く時)
1. `chipdiag.py anchors diagram.yaml` で経路点一覧を出す。
2. 決定事項(`scenario`)から **ステップ案を Claude が作る**: 各ステップのタイトル・マスタ・説明1文・経路(パッド→コントローラ→バス→メモリ/演算器の順にポートを辿る)。1ステップの矢印は3本まで。同じデータは全ステップ同じ色、制御は `style: control`。
3. ステップ表(タイトル/マスタ/説明)を見せて確認してから描く。
4. **レイテンシの目安**(R39): CPU からのアクセスを説明する時は `latency.accesses` に {from, to, op} を書く(経路は自動)。AXI→AHB→APB のように段ごとに1つずつ並べると比較しやすい。値が決まっていなければ既定値で仮置きし(`latency` の質問)、`[レイテンシ]` の仮項目を決定ログに積む。図は要素ごとの +N と合計だけ、内訳(クロック・CDC・ns)は latency.md / ビューアの表。

### G3.5 クロック(アーキテクチャ図に周波数・CDCを出す時)
- `clocks` の決定(SDCがあれば rtl2spec が create_clock からドメインを作る。PLL・分周・ゲートは仕様から聞いて追記)。
- `clocktree.py` でクロック系統図を作り、CDC一覧(ドメインの組と経路)を見せて「同期ブリッジで CDC 不要」なども確認する。

### G4 フロアプラン・ボード(`floorplan=作る` / ボードが論点の時)
```
python3 $SK/scripts/ip_size.py <LEF/Liberty/CSV> -o ip_sizes.json --find <セル名>
python3 $SK/scripts/floorplan.py floorplan.yaml -o out/fp --png --drawio [--px-per-mm 1000]
python3 $SK/scripts/board.py board.yaml -o out/board --diagram diagram.yaml --floorplan floorplan.yaml --png
```
- IPの寸法は **必ずライブラリで調べる**(LEF の SIZE を優先。Liberty は面積のみ → 正方形仮定で「仮」)。見つからないセルは候補名を添えて質問。
- ブロックIDは論理図と揃える(突き合わせに使う)。
- ボードのコネクタ位置(PCIeカードならエッジ端子=下辺、ブラケット側=SFP等)からIOの辺を求め、論理図/フロアプランとの **食い違い** を表で見せる。`--apply` で反映する前に必ず確認する(status: decided のものは変えない)。
- draw.io など実寸で描けない出力は換算を決めて(`drawio_scale`、既定 1mm=1000px)図中に換算とスケールバーを入れる。

### G5 出力
`output` の決定に従う:
```
python3 $SK/scripts/viewer.py diagram.yaml -o out/viewer.html --decisions diagram_decisions.yaml [--floorplan floorplan.yaml] [--board board.yaml]
python3 $SK/scripts/drawio.py diagram.yaml -o out/diagram.drawio      # ステップ/グループ/詳細/注記 = レイヤ
python3 $SK/scripts/clocktree.py diagram.yaml -o out/clock_tree.svg --png   # クロック系統図(源→PLL→分周/CG→ドメイン、CDC一覧)
python3 $SK/scripts/memmap.py diagram.yaml -o out --png                    # メモリマップ図・表(memmap.md)と範囲の検査
python3 $SK/scripts/decisions.py status --md > out/decisions.md       # 仕様書の「決定事項/未決事項」表
```
最後に: 何を作ったか、**仮置き・TBDの一覧**(次に決めるべきこと)、ファイル。

## 作図ルール
正本は `$SK/rules/style_rules.yaml`(`rules.py list`)。以下は要点(★ = lint が検査)。
1. チップ境界・パッド・チップ外要素を最初に置く。IOの辺はボード/フロアプランに合わせる。★外部接続先なしは警告
2. バスは独立要素。★バスに接続するポートは role: M/S
3. ★ラベル16字以内。説明は図の外
4. 1枚の階層は2段まで。それ以上は詳細図(同じ座標系のズーム)。境界ポートIDは親子で共通
5. ★1ステップの矢印は3本まで。データ=太い実線、制御=細い破線。同じデータは同じ色
6. ★SRAMの接続先(バス/演算器)を明示
7. ★クロック周波数・バス幅×クロック・IO帯域を記入(未記入は [perf] 警告)。周波数が違う接続には CDC 枠(★判定できない接続は [cdc] 警告)。詳細はクロック系統図へ
10. 接続には矢印。クロスバー側にもポート(M1/S1…)を描く
8. グループ(電源/クロック/機能)は半透明レイヤで観点ごと
9. 未決は TBD、仮決めは「仮」で描き、決定事項表と一致させる

