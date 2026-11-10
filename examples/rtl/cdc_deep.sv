// CDC 検出の確認用(深い階層): 送り側と受け側がそれぞれ 8 段下にあり、途中を構造体・構造体の配列・generate で渡す。
// 答えは cdc_expected.yaml。rtldb.py selftest で確かめる。
package deep_pkg;
  typedef struct packed { logic a; logic b; } clks_t;                 // クロックを構造体で配る
  typedef struct packed { logic valid; logic [7:0] data; } ch_t;      // 送り側 → 受け側 の 1 本
endpackage

module sync2s (input logic clk, input logic d, output logic q);      // シフトレジスタ形の 2 段
  logic [1:0] s;
  always_ff @(posedge clk) s <= {s[0], d};
  assign q = s[1];
endmodule

module leaf_tx (input logic clk, input logic [7:0] din, output deep_pkg::ch_t ch);
  always_ff @(posedge clk) begin ch.valid <= din[0]; ch.data <= din; end
endmodule

module leaf_rx #(parameter bit SYNC = 1) (input logic clk, input deep_pkg::ch_t ch, output logic [7:0] q);
  logic v;
  if (SYNC) begin : g_sync
    sync2s u_s (.clk(clk), .d(ch.valid), .q(v));
  end else begin : g_raw
    assign v = ch.valid;
  end
  always_ff @(posedge clk) if (v) q <= ch.data;
endmodule

// 非同期 FIFO(グレイコードのポインタ)
module afifo_sv (input logic wclk, input logic rclk, input logic [7:0] wdata, input logic wr, input logic rd,
                 output logic [7:0] rdata, output logic empty);
  logic [7:0] storage [4];
  logic [2:0] wbin, rbin, wgray, rgray, wgray_r1, wgray_r2, rgray_w1, rgray_w2;
  logic full;
  always_ff @(posedge wclk) if (wr && !full) begin storage[wbin[1:0]] <= wdata; wbin <= wbin + 3'd1; wgray <= (wbin + 3'd1) ^ ((wbin + 3'd1) >> 1); end
  always_ff @(posedge rclk) if (rd && !empty) begin rbin <= rbin + 3'd1; rgray <= (rbin + 3'd1) ^ ((rbin + 3'd1) >> 1); end
  always_ff @(posedge rclk) begin wgray_r1 <= wgray; wgray_r2 <= wgray_r1; end
  always_ff @(posedge wclk) begin rgray_w1 <= rgray; rgray_w2 <= rgray_w1; end
  assign rdata = storage[rbin[1:0]];
  assign empty = (rgray == wgray_r2);
  assign full  = (wgray == {~rgray_w2[2:1], rgray_w2[0]});
endmodule

// 送り側: D 段の入れ子の一番下に、clks.a の FF(ch[0])と clks.b の FF(ch[1])
module nest_tx #(parameter int D = 6) (input deep_pkg::clks_t clks, input logic [7:0] din, output deep_pkg::ch_t ch [2]);
  if (D == 0) begin : g_leaf
    leaf_tx u_a (.clk(clks.a), .din(din), .ch(ch[0]));
    leaf_tx u_b (.clk(clks.b), .din(din), .ch(ch[1]));
  end else begin : g_n
    nest_tx #(.D(D - 1)) u_n (.clks(clks), .din(din), .ch(ch));
  end
endmodule

// 受け側: D 段の入れ子の一番下で受ける。途中(D == 3)に非同期 FIFO とクロックゲート
module nest_rx #(parameter int D = 6) (input deep_pkg::clks_t clks, input deep_pkg::ch_t ch [2], input logic en,
                                       output logic [7:0] q [5]);
  if (D == 0) begin : g_leaf
    logic en_lat, clk_g;
    always_latch if (!clks.b) en_lat = en;
    assign clk_g = clks.b & en_lat;                                   // ゲートしたクロック: clks.b と同じドメイン
    leaf_rx #(.SYNC(1)) u_ok   (.clk(clks.b), .ch(ch[0]), .q(q[0]));  // a → b: valid を同期化、data は条件で取り込む
    leaf_rx #(.SYNC(0)) u_same (.clk(clks.b), .ch(ch[1]), .q(q[1]));  // b → b: 乗り換えではない
    leaf_rx #(.SYNC(0)) u_bad  (.clk(clks.a), .ch(ch[1]), .q(q[2]));  // b → a: 同期化なし
    leaf_rx #(.SYNC(1)) u_gate (.clk(clk_g),  .ch(ch[0]), .q(q[3]));  // a → ゲートした b: 同期化あり
    assign q[4] = '0;
  end else begin : g_n
    logic [7:0] qn [5];
    nest_rx #(.D(D - 1)) u_n (.clks(clks), .ch(ch), .en(en), .q(qn));
    if (D == 3) begin : g_fifo
      logic [7:0] frd, fq;
      logic fe;
      afifo_sv u_fifo (.wclk(clks.a), .rclk(clks.b), .wdata(ch[0].data), .wr(ch[0].valid), .rd(1'b1), .rdata(frd), .empty(fe));
      always_ff @(posedge clks.b) if (!fe) fq <= frd;
      assign q[0] = qn[0]; assign q[1] = qn[1]; assign q[2] = qn[2]; assign q[3] = qn[3]; assign q[4] = fq;
    end else begin : g_pass
      assign q = qn;
    end
  end
endmodule

module cdc_deep (input logic clk_a, input logic clk_b, input logic clk_c, input logic en, input logic [7:0] din,
                 output logic [7:0] dout);
  import deep_pkg::*;
  clks_t lane_clk [2];
  assign lane_clk[0].a = clk_a;  assign lane_clk[0].b = clk_b;        // レーン 0: a と b は別のクロック
  assign lane_clk[1].a = clk_c;  assign lane_clk[1].b = clk_c;        // レーン 1: どちらも clk_c → 乗り換えなし
  logic [7:0] lane_q [2][5];
  for (genvar i = 0; i < 2; i++) begin : g_lane
    ch_t ch [2];
    nest_tx #(.D(6)) u_tx (.clks(lane_clk[i]), .din(din), .ch(ch));
    nest_rx #(.D(6)) u_rx (.clks(lane_clk[i]), .ch(ch), .en(en), .q(lane_q[i]));
  end
  always_comb begin
    dout = '0;
    for (int i = 0; i < 2; i++) for (int j = 0; j < 5; j++) dout ^= lane_q[i][j];
  end
endmodule
