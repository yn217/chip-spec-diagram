// 10GbE MAC (non-ANSI style ports)
module eth_mac (clk, rst_n, eth_rxd, eth_txd, m_axis_tvalid, m_axis_tready, m_axis_tdata, m_axis_tlast);
  input clk;
  input rst_n;
  input  [7:0] eth_rxd;
  output [7:0] eth_txd;
  output m_axis_tvalid;
  input  m_axis_tready;
  output [63:0] m_axis_tdata;
  output m_axis_tlast;
endmodule
