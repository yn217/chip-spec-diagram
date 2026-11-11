# cdc_cases.v 用のタイミング制約(rtldb.py selftest で突き合わせを確かめる)
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 8  [get_ports clk_b]
# クロック切替の出力 mclk(clk_b / clk_c の切替)に create_generated_clock を書かない → 「定義のないクロックの系」として出る
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}   ;# a と b は非同期 → STA が見ない中の対策なしが「危ない」として出る
set_false_path -from [get_ports ext_irq]
set_false_path -from [get_ports ext_btn]
foreach p {din[0] din[1]} { set_false_path -from [get_ports $p] }   ;# 読めない行の例
