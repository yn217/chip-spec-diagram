// CDC 検出の確認用: 答えがわかっている小さな回路(rtldb.py の回帰テスト)
module sync2 #(parameter W = 1) (input clk, input rst_n, input [W-1:0] d, output [W-1:0] q);
  reg [W-1:0] s1, s2;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin s1 <= 0; s2 <= 0; end
    else begin s1 <= d; s2 <= s1; end
  assign q = s2;
endmodule

module cdc_cases (
  input clk_a, input clk_b, input clk_c, input rst_n, input sel, input en,
  input [7:0] din, output reg [7:0] dout, output o_bad, output o_div, output o_gate, output reg o_mux
);
  // 送り側(clk_a)
  reg a_bit, a_bit2, a_req;
  reg [7:0] a_bus, a_gray, a_data;
  always @(posedge clk_a) begin
    a_bit <= din[0]; a_bit2 <= din[1]; a_req <= din[2];
    a_bus <= din; a_gray <= din ^ (din >> 1); a_data <= din;
  end

  // 1. sync: 1 ビットを 2 段で                      → 同期化あり
  wire b_bit;
  sync2 u_ok (.clk(clk_b), .rst_n(rst_n), .d(a_bit), .q(b_bit));

  // 2. no_sync: 同期化なしで論理に使う               → 同期化なし
  reg b_bad;
  always @(posedge clk_b) b_bad <= a_bit2 & b_bit;
  assign o_bad = b_bad;

  // 3. sync_logic: 同期化の前に組み合わせ論理        → グリッチ
  wire b_glitch;
  sync2 u_logic (.clk(clk_b), .rst_n(rst_n), .d(a_bit & a_bit2), .q(b_glitch));

  // 4. sync_bus: 多ビットをそのまま 2 段             → 要確認
  wire [7:0] b_bus;
  sync2 #(8) u_bus (.clk(clk_b), .rst_n(rst_n), .d(a_bus), .q(b_bus));

  // 5. sync_gray: グレイコードを 2 段                → 同期化あり(グレイ)
  wire [7:0] b_gray;
  sync2 #(8) u_gray (.clk(clk_b), .rst_n(rst_n), .d(a_gray), .q(b_gray));

  // 6. data_enable: 同期化した要求でデータを取り込む → 要確認(ハンドシェイク)
  wire b_req;
  sync2 u_req (.clk(clk_b), .rst_n(rst_n), .d(a_req), .q(b_req));
  always @(posedge clk_b) if (b_req) dout <= a_data;

  // 7. sync_tap: 1 段目の出力を論理でも使う          → 要対策
  reg t1, t2, t_use;
  always @(posedge clk_b) begin t1 <= a_bit; t2 <= t1; t_use <= t1 | b_glitch; end

  // 8. mem: clk_a で書き clk_b で読むメモリ          → 要確認
  reg [7:0] mem [0:3];
  reg [1:0] wp, rp;
  reg [7:0] rd;
  always @(posedge clk_a) begin mem[wp] <= din; wp <= wp + 1; end
  always @(posedge clk_b) begin rd <= mem[rp]; rp <= rp + 1; end

  // 9. 分周クロック(clk_a の 1/2): 同じ系 → CDC にしない
  reg div2;
  always @(posedge clk_a) div2 <= ~div2;
  reg d_ff;
  always @(posedge div2) d_ff <= a_bit;
  assign o_div = d_ff;

  // 10. ゲートされたクロック(clk_b & en_lat): clk_b と同じドメイン → CDC にしない
  reg en_lat;
  always @(clk_b or en) if (!clk_b) en_lat <= en;
  wire gclk = clk_b & en_lat;
  reg g_ff;
  always @(posedge gclk) g_ff <= b_bit ^ rd[0] ^ t2 ^ t_use ^ b_bus[0] ^ b_gray[0];
  assign o_gate = g_ff;

  // 11. クロック切替(clk_b / clk_c): 別のドメイン。clk_b の FF を同期化なしで受ける → 同期化なし
  wire mclk = sel ? clk_c : clk_b;
  always @(posedge mclk) o_mux <= g_ff;
endmodule
