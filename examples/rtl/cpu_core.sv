// CPU (RISC-V x2) — AXI master
module cpu_core #(parameter AW=40, parameter DW=128) (
    input  wire clk,
    input  wire rst_n,
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
    input  wire jtag_tck, input wire jtag_tdi, output wire jtag_tdo,
    inout  wire [15:0] gpio
);
endmodule
