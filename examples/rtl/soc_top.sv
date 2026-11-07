`include "defs.svh"
// ============ SoC top ============
module soc_top (
  input  wire        clk,
  input  wire        rst_n,
  input  wire [3:0]  pcie_rx_p,
  output wire [3:0]  pcie_tx_p,
  inout  wire [31:0] ddr_dq,
  output wire [15:0] ddr_addr,
  output wire        ddr_ck,
  input  wire [7:0]  eth_rxd,
  output wire [7:0]  eth_txd,
  input  wire        jtag_tck, jtag_tdi,
  output wire        jtag_tdo,
  inout  wire [15:0] gpio
);
  wire cpu_awvalid;
  wire cpu_awready;
  wire [AW-1:0] cpu_awaddr;
  wire cpu_wvalid;
  wire cpu_wready;
  wire [DW-1:0] cpu_wdata;
  wire cpu_wlast;
  wire cpu_bvalid;
  wire cpu_bready;
  wire cpu_arvalid;
  wire cpu_arready;
  wire [AW-1:0] cpu_araddr;
  wire cpu_rvalid;
  wire cpu_rready;
  wire [DW-1:0] cpu_rdata;
  wire cpu_rlast;
  wire dma_m_awvalid;
  wire dma_m_awready;
  wire [AW-1:0] dma_m_awaddr;
  wire dma_m_wvalid;
  wire dma_m_wready;
  wire [DW-1:0] dma_m_wdata;
  wire dma_m_wlast;
  wire dma_m_bvalid;
  wire dma_m_bready;
  wire dma_m_arvalid;
  wire dma_m_arready;
  wire [AW-1:0] dma_m_araddr;
  wire dma_m_rvalid;
  wire dma_m_rready;
  wire [DW-1:0] dma_m_rdata;
  wire dma_m_rlast;
  wire pcie_m_awvalid;
  wire pcie_m_awready;
  wire [AW-1:0] pcie_m_awaddr;
  wire pcie_m_wvalid;
  wire pcie_m_wready;
  wire [DW-1:0] pcie_m_wdata;
  wire pcie_m_wlast;
  wire pcie_m_bvalid;
  wire pcie_m_bready;
  wire pcie_m_arvalid;
  wire pcie_m_arready;
  wire [AW-1:0] pcie_m_araddr;
  wire pcie_m_rvalid;
  wire pcie_m_rready;
  wire [DW-1:0] pcie_m_rdata;
  wire pcie_m_rlast;
  wire npu_m_awvalid;
  wire npu_m_awready;
  wire [AW-1:0] npu_m_awaddr;
  wire npu_m_wvalid;
  wire npu_m_wready;
  wire [DW-1:0] npu_m_wdata;
  wire npu_m_wlast;
  wire npu_m_bvalid;
  wire npu_m_bready;
  wire npu_m_arvalid;
  wire npu_m_arready;
  wire [AW-1:0] npu_m_araddr;
  wire npu_m_rvalid;
  wire npu_m_rready;
  wire [DW-1:0] npu_m_rdata;
  wire npu_m_rlast;
  wire sram_s_awvalid;
  wire sram_s_awready;
  wire [AW-1:0] sram_s_awaddr;
  wire sram_s_wvalid;
  wire sram_s_wready;
  wire [DW-1:0] sram_s_wdata;
  wire sram_s_wlast;
  wire sram_s_bvalid;
  wire sram_s_bready;
  wire sram_s_arvalid;
  wire sram_s_arready;
  wire [AW-1:0] sram_s_araddr;
  wire sram_s_rvalid;
  wire sram_s_rready;
  wire [DW-1:0] sram_s_rdata;
  wire sram_s_rlast;
  wire ddr_s_awvalid;
  wire ddr_s_awready;
  wire [AW-1:0] ddr_s_awaddr;
  wire ddr_s_wvalid;
  wire ddr_s_wready;
  wire [DW-1:0] ddr_s_wdata;
  wire ddr_s_wlast;
  wire ddr_s_bvalid;
  wire ddr_s_bready;
  wire ddr_s_arvalid;
  wire ddr_s_arready;
  wire [AW-1:0] ddr_s_araddr;
  wire ddr_s_rvalid;
  wire ddr_s_rready;
  wire [DW-1:0] ddr_s_rdata;
  wire ddr_s_rlast;
  wire dma_s_awvalid;
  wire dma_s_awready;
  wire [AW-1:0] dma_s_awaddr;
  wire dma_s_wvalid;
  wire dma_s_wready;
  wire [DW-1:0] dma_s_wdata;
  wire dma_s_wlast;
  wire dma_s_bvalid;
  wire dma_s_bready;
  wire dma_s_arvalid;
  wire dma_s_arready;
  wire [AW-1:0] dma_s_araddr;
  wire dma_s_rvalid;
  wire dma_s_rready;
  wire [DW-1:0] dma_s_rdata;
  wire dma_s_rlast;
  wire npu_s_awvalid;
  wire npu_s_awready;
  wire [AW-1:0] npu_s_awaddr;
  wire npu_s_wvalid;
  wire npu_s_wready;
  wire [DW-1:0] npu_s_wdata;
  wire npu_s_wlast;
  wire npu_s_bvalid;
  wire npu_s_bready;
  wire npu_s_arvalid;
  wire npu_s_arready;
  wire [AW-1:0] npu_s_araddr;
  wire npu_s_rvalid;
  wire npu_s_rready;
  wire [DW-1:0] npu_s_rdata;
  wire npu_s_rlast;
  wire pcie_s_awvalid;
  wire pcie_s_awready;
  wire [AW-1:0] pcie_s_awaddr;
  wire pcie_s_wvalid;
  wire pcie_s_wready;
  wire [DW-1:0] pcie_s_wdata;
  wire pcie_s_wlast;
  wire pcie_s_bvalid;
  wire pcie_s_bready;
  wire pcie_s_arvalid;
  wire pcie_s_arready;
  wire [AW-1:0] pcie_s_araddr;
  wire pcie_s_rvalid;
  wire pcie_s_rready;
  wire [DW-1:0] pcie_s_rdata;
  wire pcie_s_rlast;
  wire eth_tvalid, eth_tready, eth_tlast; wire [63:0] eth_tdata;

  cpu_core #(.DW(128)) u_cpu (
    .clk(clk),
    .rst_n(rst_n),
    .m_axi_awvalid(cpu_awvalid),
    .m_axi_awready(cpu_awready),
    .m_axi_awaddr(cpu_awaddr),
    .m_axi_wvalid(cpu_wvalid),
    .m_axi_wready(cpu_wready),
    .m_axi_wdata(cpu_wdata),
    .m_axi_wlast(cpu_wlast),
    .m_axi_bvalid(cpu_bvalid),
    .m_axi_bready(cpu_bready),
    .m_axi_arvalid(cpu_arvalid),
    .m_axi_arready(cpu_arready),
    .m_axi_araddr(cpu_araddr),
    .m_axi_rvalid(cpu_rvalid),
    .m_axi_rready(cpu_rready),
    .m_axi_rdata(cpu_rdata),
    .m_axi_rlast(cpu_rlast),
    .jtag_tck(jtag_tck),
    .jtag_tdi(jtag_tdi),
    .jtag_tdo(jtag_tdo),
    .gpio(gpio)
  );

  dma_engine u_dma (
    .clk(clk),
    .rst_n(rst_n),
    .m_axi_awvalid(dma_m_awvalid),
    .m_axi_awready(dma_m_awready),
    .m_axi_awaddr(dma_m_awaddr),
    .m_axi_wvalid(dma_m_wvalid),
    .m_axi_wready(dma_m_wready),
    .m_axi_wdata(dma_m_wdata),
    .m_axi_wlast(dma_m_wlast),
    .m_axi_bvalid(dma_m_bvalid),
    .m_axi_bready(dma_m_bready),
    .m_axi_arvalid(dma_m_arvalid),
    .m_axi_arready(dma_m_arready),
    .m_axi_araddr(dma_m_araddr),
    .m_axi_rvalid(dma_m_rvalid),
    .m_axi_rready(dma_m_rready),
    .m_axi_rdata(dma_m_rdata),
    .m_axi_rlast(dma_m_rlast),
    .s_axi_awvalid(dma_s_awvalid),
    .s_axi_awready(dma_s_awready),
    .s_axi_awaddr(dma_s_awaddr),
    .s_axi_wvalid(dma_s_wvalid),
    .s_axi_wready(dma_s_wready),
    .s_axi_wdata(dma_s_wdata),
    .s_axi_wlast(dma_s_wlast),
    .s_axi_bvalid(dma_s_bvalid),
    .s_axi_bready(dma_s_bready),
    .s_axi_arvalid(dma_s_arvalid),
    .s_axi_arready(dma_s_arready),
    .s_axi_araddr(dma_s_araddr),
    .s_axi_rvalid(dma_s_rvalid),
    .s_axi_rready(dma_s_rready),
    .s_axi_rdata(dma_s_rdata),
    .s_axi_rlast(dma_s_rlast),
    .s_axis_tvalid(eth_tvalid),
    .s_axis_tready(eth_tready),
    .s_axis_tdata(eth_tdata),
    .s_axis_tlast(eth_tlast)
  );

  pcie_ctrl u_pcie (
    .clk(clk),
    .rst_n(rst_n),
    .m_axi_awvalid(pcie_m_awvalid),
    .m_axi_awready(pcie_m_awready),
    .m_axi_awaddr(pcie_m_awaddr),
    .m_axi_wvalid(pcie_m_wvalid),
    .m_axi_wready(pcie_m_wready),
    .m_axi_wdata(pcie_m_wdata),
    .m_axi_wlast(pcie_m_wlast),
    .m_axi_bvalid(pcie_m_bvalid),
    .m_axi_bready(pcie_m_bready),
    .m_axi_arvalid(pcie_m_arvalid),
    .m_axi_arready(pcie_m_arready),
    .m_axi_araddr(pcie_m_araddr),
    .m_axi_rvalid(pcie_m_rvalid),
    .m_axi_rready(pcie_m_rready),
    .m_axi_rdata(pcie_m_rdata),
    .m_axi_rlast(pcie_m_rlast),
    .s_axi_awvalid(pcie_s_awvalid),
    .s_axi_awready(pcie_s_awready),
    .s_axi_awaddr(pcie_s_awaddr),
    .s_axi_wvalid(pcie_s_wvalid),
    .s_axi_wready(pcie_s_wready),
    .s_axi_wdata(pcie_s_wdata),
    .s_axi_wlast(pcie_s_wlast),
    .s_axi_bvalid(pcie_s_bvalid),
    .s_axi_bready(pcie_s_bready),
    .s_axi_arvalid(pcie_s_arvalid),
    .s_axi_arready(pcie_s_arready),
    .s_axi_araddr(pcie_s_araddr),
    .s_axi_rvalid(pcie_s_rvalid),
    .s_axi_rready(pcie_s_rready),
    .s_axi_rdata(pcie_s_rdata),
    .s_axi_rlast(pcie_s_rlast),
    .pcie_rx_p(pcie_rx_p),
    .pcie_tx_p(pcie_tx_p)
  );

  npu_top u_npu (
    .clk(clk),
    .rst_n(rst_n),
    .m_axi_awvalid(npu_m_awvalid),
    .m_axi_awready(npu_m_awready),
    .m_axi_awaddr(npu_m_awaddr),
    .m_axi_wvalid(npu_m_wvalid),
    .m_axi_wready(npu_m_wready),
    .m_axi_wdata(npu_m_wdata),
    .m_axi_wlast(npu_m_wlast),
    .m_axi_bvalid(npu_m_bvalid),
    .m_axi_bready(npu_m_bready),
    .m_axi_arvalid(npu_m_arvalid),
    .m_axi_arready(npu_m_arready),
    .m_axi_araddr(npu_m_araddr),
    .m_axi_rvalid(npu_m_rvalid),
    .m_axi_rready(npu_m_rready),
    .m_axi_rdata(npu_m_rdata),
    .m_axi_rlast(npu_m_rlast),
    .s_axi_awvalid(npu_s_awvalid),
    .s_axi_awready(npu_s_awready),
    .s_axi_awaddr(npu_s_awaddr),
    .s_axi_wvalid(npu_s_wvalid),
    .s_axi_wready(npu_s_wready),
    .s_axi_wdata(npu_s_wdata),
    .s_axi_wlast(npu_s_wlast),
    .s_axi_bvalid(npu_s_bvalid),
    .s_axi_bready(npu_s_bready),
    .s_axi_arvalid(npu_s_arvalid),
    .s_axi_arready(npu_s_arready),
    .s_axi_araddr(npu_s_araddr),
    .s_axi_rvalid(npu_s_rvalid),
    .s_axi_rready(npu_s_rready),
    .s_axi_rdata(npu_s_rdata),
    .s_axi_rlast(npu_s_rlast)
  );

  axi_xbar #(.NM(4), .NS(5)) u_xbar (
    .clk(clk),
    .rst_n(rst_n),
    .s0_axi_awvalid(cpu_awvalid),
    .s0_axi_awready(cpu_awready),
    .s0_axi_awaddr(cpu_awaddr),
    .s0_axi_wvalid(cpu_wvalid),
    .s0_axi_wready(cpu_wready),
    .s0_axi_wdata(cpu_wdata),
    .s0_axi_wlast(cpu_wlast),
    .s0_axi_bvalid(cpu_bvalid),
    .s0_axi_bready(cpu_bready),
    .s0_axi_arvalid(cpu_arvalid),
    .s0_axi_arready(cpu_arready),
    .s0_axi_araddr(cpu_araddr),
    .s0_axi_rvalid(cpu_rvalid),
    .s0_axi_rready(cpu_rready),
    .s0_axi_rdata(cpu_rdata),
    .s0_axi_rlast(cpu_rlast),
    .s1_axi_awvalid(dma_m_awvalid),
    .s1_axi_awready(dma_m_awready),
    .s1_axi_awaddr(dma_m_awaddr),
    .s1_axi_wvalid(dma_m_wvalid),
    .s1_axi_wready(dma_m_wready),
    .s1_axi_wdata(dma_m_wdata),
    .s1_axi_wlast(dma_m_wlast),
    .s1_axi_bvalid(dma_m_bvalid),
    .s1_axi_bready(dma_m_bready),
    .s1_axi_arvalid(dma_m_arvalid),
    .s1_axi_arready(dma_m_arready),
    .s1_axi_araddr(dma_m_araddr),
    .s1_axi_rvalid(dma_m_rvalid),
    .s1_axi_rready(dma_m_rready),
    .s1_axi_rdata(dma_m_rdata),
    .s1_axi_rlast(dma_m_rlast),
    .s2_axi_awvalid(pcie_m_awvalid),
    .s2_axi_awready(pcie_m_awready),
    .s2_axi_awaddr(pcie_m_awaddr),
    .s2_axi_wvalid(pcie_m_wvalid),
    .s2_axi_wready(pcie_m_wready),
    .s2_axi_wdata(pcie_m_wdata),
    .s2_axi_wlast(pcie_m_wlast),
    .s2_axi_bvalid(pcie_m_bvalid),
    .s2_axi_bready(pcie_m_bready),
    .s2_axi_arvalid(pcie_m_arvalid),
    .s2_axi_arready(pcie_m_arready),
    .s2_axi_araddr(pcie_m_araddr),
    .s2_axi_rvalid(pcie_m_rvalid),
    .s2_axi_rready(pcie_m_rready),
    .s2_axi_rdata(pcie_m_rdata),
    .s2_axi_rlast(pcie_m_rlast),
    .s3_axi_awvalid(npu_m_awvalid),
    .s3_axi_awready(npu_m_awready),
    .s3_axi_awaddr(npu_m_awaddr),
    .s3_axi_wvalid(npu_m_wvalid),
    .s3_axi_wready(npu_m_wready),
    .s3_axi_wdata(npu_m_wdata),
    .s3_axi_wlast(npu_m_wlast),
    .s3_axi_bvalid(npu_m_bvalid),
    .s3_axi_bready(npu_m_bready),
    .s3_axi_arvalid(npu_m_arvalid),
    .s3_axi_arready(npu_m_arready),
    .s3_axi_araddr(npu_m_araddr),
    .s3_axi_rvalid(npu_m_rvalid),
    .s3_axi_rready(npu_m_rready),
    .s3_axi_rdata(npu_m_rdata),
    .s3_axi_rlast(npu_m_rlast),
    .m0_axi_awvalid(sram_s_awvalid),
    .m0_axi_awready(sram_s_awready),
    .m0_axi_awaddr(sram_s_awaddr),
    .m0_axi_wvalid(sram_s_wvalid),
    .m0_axi_wready(sram_s_wready),
    .m0_axi_wdata(sram_s_wdata),
    .m0_axi_wlast(sram_s_wlast),
    .m0_axi_bvalid(sram_s_bvalid),
    .m0_axi_bready(sram_s_bready),
    .m0_axi_arvalid(sram_s_arvalid),
    .m0_axi_arready(sram_s_arready),
    .m0_axi_araddr(sram_s_araddr),
    .m0_axi_rvalid(sram_s_rvalid),
    .m0_axi_rready(sram_s_rready),
    .m0_axi_rdata(sram_s_rdata),
    .m0_axi_rlast(sram_s_rlast),
    .m1_axi_awvalid(ddr_s_awvalid),
    .m1_axi_awready(ddr_s_awready),
    .m1_axi_awaddr(ddr_s_awaddr),
    .m1_axi_wvalid(ddr_s_wvalid),
    .m1_axi_wready(ddr_s_wready),
    .m1_axi_wdata(ddr_s_wdata),
    .m1_axi_wlast(ddr_s_wlast),
    .m1_axi_bvalid(ddr_s_bvalid),
    .m1_axi_bready(ddr_s_bready),
    .m1_axi_arvalid(ddr_s_arvalid),
    .m1_axi_arready(ddr_s_arready),
    .m1_axi_araddr(ddr_s_araddr),
    .m1_axi_rvalid(ddr_s_rvalid),
    .m1_axi_rready(ddr_s_rready),
    .m1_axi_rdata(ddr_s_rdata),
    .m1_axi_rlast(ddr_s_rlast),
    .m2_axi_awvalid(dma_s_awvalid),
    .m2_axi_awready(dma_s_awready),
    .m2_axi_awaddr(dma_s_awaddr),
    .m2_axi_wvalid(dma_s_wvalid),
    .m2_axi_wready(dma_s_wready),
    .m2_axi_wdata(dma_s_wdata),
    .m2_axi_wlast(dma_s_wlast),
    .m2_axi_bvalid(dma_s_bvalid),
    .m2_axi_bready(dma_s_bready),
    .m2_axi_arvalid(dma_s_arvalid),
    .m2_axi_arready(dma_s_arready),
    .m2_axi_araddr(dma_s_araddr),
    .m2_axi_rvalid(dma_s_rvalid),
    .m2_axi_rready(dma_s_rready),
    .m2_axi_rdata(dma_s_rdata),
    .m2_axi_rlast(dma_s_rlast),
    .m3_axi_awvalid(npu_s_awvalid),
    .m3_axi_awready(npu_s_awready),
    .m3_axi_awaddr(npu_s_awaddr),
    .m3_axi_wvalid(npu_s_wvalid),
    .m3_axi_wready(npu_s_wready),
    .m3_axi_wdata(npu_s_wdata),
    .m3_axi_wlast(npu_s_wlast),
    .m3_axi_bvalid(npu_s_bvalid),
    .m3_axi_bready(npu_s_bready),
    .m3_axi_arvalid(npu_s_arvalid),
    .m3_axi_arready(npu_s_arready),
    .m3_axi_araddr(npu_s_araddr),
    .m3_axi_rvalid(npu_s_rvalid),
    .m3_axi_rready(npu_s_rready),
    .m3_axi_rdata(npu_s_rdata),
    .m3_axi_rlast(npu_s_rlast),
    .m4_axi_awvalid(pcie_s_awvalid),
    .m4_axi_awready(pcie_s_awready),
    .m4_axi_awaddr(pcie_s_awaddr),
    .m4_axi_wvalid(pcie_s_wvalid),
    .m4_axi_wready(pcie_s_wready),
    .m4_axi_wdata(pcie_s_wdata),
    .m4_axi_wlast(pcie_s_wlast),
    .m4_axi_bvalid(pcie_s_bvalid),
    .m4_axi_bready(pcie_s_bready),
    .m4_axi_arvalid(pcie_s_arvalid),
    .m4_axi_arready(pcie_s_arready),
    .m4_axi_araddr(pcie_s_araddr),
    .m4_axi_rvalid(pcie_s_rvalid),
    .m4_axi_rready(pcie_s_rready),
    .m4_axi_rdata(pcie_s_rdata),
    .m4_axi_rlast(pcie_s_rlast)
  );

  sram_wrap u_sram (
    .clk(clk),
    .rst_n(rst_n),
    .s_axi_awvalid(sram_s_awvalid),
    .s_axi_awready(sram_s_awready),
    .s_axi_awaddr(sram_s_awaddr),
    .s_axi_wvalid(sram_s_wvalid),
    .s_axi_wready(sram_s_wready),
    .s_axi_wdata(sram_s_wdata),
    .s_axi_wlast(sram_s_wlast),
    .s_axi_bvalid(sram_s_bvalid),
    .s_axi_bready(sram_s_bready),
    .s_axi_arvalid(sram_s_arvalid),
    .s_axi_arready(sram_s_arready),
    .s_axi_araddr(sram_s_araddr),
    .s_axi_rvalid(sram_s_rvalid),
    .s_axi_rready(sram_s_rready),
    .s_axi_rdata(sram_s_rdata),
    .s_axi_rlast(sram_s_rlast)
  );

  ddr_ctrl u_ddrc (
    .clk(clk),
    .rst_n(rst_n),
    .s_axi_awvalid(ddr_s_awvalid),
    .s_axi_awready(ddr_s_awready),
    .s_axi_awaddr(ddr_s_awaddr),
    .s_axi_wvalid(ddr_s_wvalid),
    .s_axi_wready(ddr_s_wready),
    .s_axi_wdata(ddr_s_wdata),
    .s_axi_wlast(ddr_s_wlast),
    .s_axi_bvalid(ddr_s_bvalid),
    .s_axi_bready(ddr_s_bready),
    .s_axi_arvalid(ddr_s_arvalid),
    .s_axi_arready(ddr_s_arready),
    .s_axi_araddr(ddr_s_araddr),
    .s_axi_rvalid(ddr_s_rvalid),
    .s_axi_rready(ddr_s_rready),
    .s_axi_rdata(ddr_s_rdata),
    .s_axi_rlast(ddr_s_rlast),
    .ddr_dq(ddr_dq),
    .ddr_addr(ddr_addr),
    .ddr_ck(ddr_ck)
  );

  eth_mac u_eth (clk, rst_n, eth_rxd, eth_txd, eth_tvalid, eth_tready, eth_tdata, eth_tlast);  // positional

endmodule
