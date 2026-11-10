// CDC 検出の確認用: 答えがわかっている小さな回路(rtldb.py の回帰テスト)
module sync2 #(parameter W = 1) (input clk, input rst_n, input [W-1:0] d, output [W-1:0] q);
  reg [W-1:0] s1, s2;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin s1 <= 0; s2 <= 0; end
    else begin s1 <= d; s2 <= s1; end
  assign q = s2;
endmodule

// 非同期 FIFO(グレイコードのポインタを両向きに同期化。データはメモリから直接読む)
module afifo (input wclk, input rclk, input [7:0] wdata, input wr, input rd, output [7:0] rdata, output full, output empty);
  reg [7:0] storage [0:3];
  reg [2:0] wbin, rbin, wgray, rgray;
  wire [2:0] wgray_r, rgray_w;
  always @(posedge wclk) if (wr && !full) begin storage[wbin[1:0]] <= wdata; wbin <= wbin + 1; wgray <= (wbin + 1) ^ ((wbin + 1) >> 1); end
  always @(posedge rclk) if (rd && !empty) begin rbin <= rbin + 1; rgray <= (rbin + 1) ^ ((rbin + 1) >> 1); end
  sync2 #(3) u_w2r (.clk(rclk), .rst_n(1'b1), .d(wgray), .q(wgray_r));
  sync2 #(3) u_r2w (.clk(wclk), .rst_n(1'b1), .d(rgray), .q(rgray_w));
  assign rdata = storage[rbin[1:0]];
  assign empty = (rgray == wgray_r);
  assign full  = (wgray == {~rgray_w[2:1], rgray_w[0]});
endmodule

module cdc_cases (
  input clk_a, input clk_b, input clk_c, input rst_n, input sel, input en,
  input ext_irq, input ext_btn,
  input [7:0] din, output reg [7:0] dout, output o_bad, output o_div, output o_gate, output reg o_mux,
  output reg [7:0] o_new
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

  // 12. シフトレジスタ形の同期化: sh <= {sh[0], x}       → 同期化あり(ビットの範囲ごとに追う)
  reg a_bit3;
  always @(posedge clk_a) a_bit3 <= din[3];
  reg [1:0] sh;
  always @(posedge clk_b) sh <= {sh[0], a_bit3};
  wire b_sh = sh[1];

  // 13. 非同期 FIFO: データはメモリから直接読むが、ポインタを両向きに同期化 → FIFO のデータ(同期化あり)
  wire [7:0] f_rd;
  wire f_full, f_empty;
  afifo u_fifo (.wclk(clk_a), .rclk(clk_b), .wdata(din), .wr(a_req), .rd(b_sh), .rdata(f_rd), .full(f_full), .empty(f_empty));
  reg [7:0] f_q;
  always @(posedge clk_b) if (!f_empty) f_q <= f_rd;

  // 14. トグルを同期化してエッジを検出(パルスの受け渡し)  → 同期化 + エッジ検出
  reg a_tgl;
  always @(posedge clk_a) if (din[4]) a_tgl <= ~a_tgl;
  reg p1, p2, p3, b_pulse;
  always @(posedge clk_b) begin p1 <= a_tgl; p2 <= p1; p3 <= p2; b_pulse <= p2 ^ p3; end

  // 15. クロックをデータとして取り込む                    → 要確認
  reg ck_smp;
  always @(posedge clk_b) ck_smp <= clk_c;

  // 16. 非同期の入力端子(--async-in で宣言): そのまま使う → 同期化なし / 2 段で受ける → 同期化あり
  reg irq_q;
  always @(posedge clk_b) irq_q <= ext_irq & b_sh;
  wire b_btn;
  sync2 u_btn (.clk(clk_b), .rst_n(rst_n), .d(ext_btn), .q(b_btn));

  // 17. ベクタの各ビットを別々に同期化(それぞれは同期化あり)。受け側で合わせる → 再収束(要確認)
  reg [1:0] a_vec;
  always @(posedge clk_a) begin a_vec[0] <= din[5]; a_vec[1] <= din[6]; end
  wire b_v0, b_v1;
  sync2 u_v0 (.clk(clk_b), .rst_n(rst_n), .d(a_vec[0]), .q(b_v0));
  sync2 u_v1 (.clk(clk_b), .rst_n(rst_n), .d(a_vec[1]), .q(b_v1));
  reg b_both;
  always @(posedge clk_b) b_both <= b_v0 & b_v1;

  // 18. リセット(RDC)
  //  rst_sync: 非同期アサート・同期解除の同期化回路(clk_b)
  reg r1, r2;
  always @(posedge clk_b or negedge rst_n) if (!rst_n) begin r1 <= 1'b0; r2 <= 1'b0; end else begin r1 <= 1'b1; r2 <= r1; end
  //  rst_ok: 同じドメインで作ったリセットを使う
  reg [7:0] k_ok;
  always @(posedge clk_b or negedge r2) if (!r2) k_ok <= 8'h0; else k_ok <= f_q;
  //  rst_cross: clk_b で作ったリセットを clk_a の FF に使う → 要対策
  reg [7:0] k_x;
  always @(posedge clk_a or negedge r2) if (!r2) k_x <= 8'h0; else k_x <= din;
  //  rst_logic: FF 2 本の論理で作ったリセット → グリッチ(要確認)
  reg sw_rst;
  always @(posedge clk_b) sw_rst <= b_btn;
  wire rl_n = r2 & ~sw_rst;
  reg [7:0] k_l;
  always @(posedge clk_b or negedge rl_n) if (!rl_n) k_l <= 8'h0; else k_l <= f_q;
  // 19. 同期化した信号と AND してから使う(組み合わせでゲートして渡す)  → ゲートの形(要確認)
  reg [7:0] a_hold;
  always @(posedge clk_a) if (din[7]) a_hold <= din;
  wire [7:0] b_gated = a_hold & {8{b_pulse}};
  reg [7:0] b_cap;
  always @(posedge clk_b) b_cap <= b_cap | b_gated;

  // 20. 反転・バッファをはさんだ同期化(1 入力の論理はグリッチを作らない)   → 同期化あり
  reg a_bit4;
  always @(posedge clk_a) a_bit4 <= din[2];
  wire a_inv = ~a_bit4;
  wire b_inv;
  sync2 u_inv (.clk(clk_b), .rst_n(rst_n), .d(~a_inv), .q(b_inv));

  // 21. ベクタをそのまま受け渡してから 1 ビットずつ同期化(ビットの範囲を元までさかのぼる) → 同期化あり
  reg [1:0] a_two;
  always @(posedge clk_a) begin a_two[0] <= din[0]; a_two[1] <= din[1]; end
  wire [1:0] w_two = a_two;
  wire [1:0] w_two2 = w_two;
  wire b_two0;
  sync2 u_two0 (.clk(clk_b), .rst_n(rst_n), .d(w_two2[0]), .q(b_two0));
  always @(posedge clk_b) o_new <= k_ok ^ k_l ^ b_cap ^ {7'h0, b_pulse ^ irq_q ^ ck_smp ^ b_both ^ b_inv ^ b_two0};
endmodule
