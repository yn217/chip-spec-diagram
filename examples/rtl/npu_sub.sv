// NPU subsystem
module sram_1p #(parameter AW=14, parameter DW=512) (
  input wire clk,
  input wire ce, input wire we,
  input wire [AW-1:0] addr,
  input wire [DW-1:0] wdata,
  output wire [DW-1:0] rdata
);
  reg [DW-1:0] mem [0:(1<<AW)-1];
endmodule

module npu_ctrl #(parameter AW=40, parameter DW=256) (
  input wire clk, input wire rst_n,
    input  wire s_axi_awvalid,
    output wire s_axi_awready,
    input  wire [AW-1:0] s_axi_awaddr,
    input  wire s_axi_wvalid,
    output wire s_axi_wready,
    input  wire [DW-1:0] s_axi_wdata,
    input  wire s_axi_wlast,
    output wire s_axi_bvalid,
    input  wire s_axi_bready,
    input  wire s_axi_arvalid,
    output wire s_axi_arready,
    input  wire [AW-1:0] s_axi_araddr,
    output wire s_axi_rvalid,
    input  wire s_axi_rready,
    output wire [DW-1:0] s_axi_rdata,
    output wire s_axi_rlast,
  output wire start, input wire done
);
endmodule

module pe_array (
  input wire clk, input wire rst_n,
  input wire start, output wire done,
  output wire buf_ce, output wire buf_we,
  output wire [13:0] buf_addr, output wire [511:0] buf_wdata, input wire [511:0] buf_rdata
);
endmodule

module npu_dma #(parameter AW=40, parameter DW=256) (
  input wire clk, input wire rst_n,
    output wire m_axi_awvalid,
    input  wire m_axi_awready,
    output wire [AW-1:0] m_axi_awaddr,
    output wire m_axi_wvalid,
    input  wire m_axi_wready,
    output wire [DW-1:0] m_axi_wdata,
    output wire m_axi_wlast,
    input  wire m_axi_bvalid,
    output wire m_axi_bready,
    output wire m_axi_arvalid,
    input  wire m_axi_arready,
    output wire [AW-1:0] m_axi_araddr,
    input  wire m_axi_rvalid,
    output wire m_axi_rready,
    input  wire [DW-1:0] m_axi_rdata,
    input  wire m_axi_rlast,
  output wire lb_ce, output wire lb_we, output wire [13:0] lb_addr,
  output wire [511:0] lb_wdata, input wire [511:0] lb_rdata
);
endmodule

module npu_top #(parameter AW=40, parameter DW=256) (
  input wire clk, input wire rst_n,
    output wire m_axi_awvalid,
    input  wire m_axi_awready,
    output wire [AW-1:0] m_axi_awaddr,
    output wire m_axi_wvalid,
    input  wire m_axi_wready,
    output wire [DW-1:0] m_axi_wdata,
    output wire m_axi_wlast,
    input  wire m_axi_bvalid,
    output wire m_axi_bready,
    output wire m_axi_arvalid,
    input  wire m_axi_arready,
    output wire [AW-1:0] m_axi_araddr,
    input  wire m_axi_rvalid,
    output wire m_axi_rready,
    input  wire [DW-1:0] m_axi_rdata,
    input  wire m_axi_rlast,
    input  wire s_axi_awvalid,
    output wire s_axi_awready,
    input  wire [AW-1:0] s_axi_awaddr,
    input  wire s_axi_wvalid,
    output wire s_axi_wready,
    input  wire [DW-1:0] s_axi_wdata,
    input  wire s_axi_wlast,
    output wire s_axi_bvalid,
    input  wire s_axi_bready,
    input  wire s_axi_arvalid,
    output wire s_axi_arready,
    input  wire [AW-1:0] s_axi_araddr,
    output wire s_axi_rvalid,
    input  wire s_axi_rready,
    output wire [DW-1:0] s_axi_rdata,
    output wire s_axi_rlast
);
  wire start, done;
  wire pe_ce, pe_we; wire [13:0] pe_addr; wire [511:0] pe_wdata, pe_rdata;
  wire dm_ce, dm_we; wire [13:0] dm_addr; wire [511:0] dm_wdata, dm_rdata;
  npu_ctrl #(.AW(AW),.DW(DW)) u_ctrl (.clk(clk), .rst_n(rst_n),
    .s_axi_awvalid(s_axi_awvalid),
    .s_axi_awready(s_axi_awready),
    .s_axi_awaddr(s_axi_awaddr),
    .s_axi_wvalid(s_axi_wvalid),
    .s_axi_wready(s_axi_wready),
    .s_axi_wdata(s_axi_wdata),
    .s_axi_wlast(s_axi_wlast),
    .s_axi_bvalid(s_axi_bvalid),
    .s_axi_bready(s_axi_bready),
    .s_axi_arvalid(s_axi_arvalid),
    .s_axi_arready(s_axi_arready),
    .s_axi_araddr(s_axi_araddr),
    .s_axi_rvalid(s_axi_rvalid),
    .s_axi_rready(s_axi_rready),
    .s_axi_rdata(s_axi_rdata),
    .s_axi_rlast(s_axi_rlast),
    .start(start), .done(done));
  npu_dma u_ndma (.clk(clk), .rst_n(rst_n),
    .m_axi_awvalid(m_axi_awvalid),
    .m_axi_awready(m_axi_awready),
    .m_axi_awaddr(m_axi_awaddr),
    .m_axi_wvalid(m_axi_wvalid),
    .m_axi_wready(m_axi_wready),
    .m_axi_wdata(m_axi_wdata),
    .m_axi_wlast(m_axi_wlast),
    .m_axi_bvalid(m_axi_bvalid),
    .m_axi_bready(m_axi_bready),
    .m_axi_arvalid(m_axi_arvalid),
    .m_axi_arready(m_axi_arready),
    .m_axi_araddr(m_axi_araddr),
    .m_axi_rvalid(m_axi_rvalid),
    .m_axi_rready(m_axi_rready),
    .m_axi_rdata(m_axi_rdata),
    .m_axi_rlast(m_axi_rlast),
    .lb_ce(dm_ce), .lb_we(dm_we), .lb_addr(dm_addr), .lb_wdata(dm_wdata), .lb_rdata(dm_rdata));
  pe_array u_pe (.clk(clk), .rst_n(rst_n), .start(start), .done(done),
    .buf_ce(pe_ce), .buf_we(pe_we), .buf_addr(pe_addr), .buf_wdata(pe_wdata), .buf_rdata(pe_rdata));
  // dual-bank local buffer
  sram_1p u_lbuf0 (.clk(clk), .ce(dm_ce), .we(dm_we), .addr(dm_addr), .wdata(dm_wdata), .rdata(dm_rdata));
  sram_1p u_lbuf1 (.clk(clk), .ce(pe_ce), .we(pe_we), .addr(pe_addr), .wdata(pe_wdata), .rdata(pe_rdata));
endmodule
