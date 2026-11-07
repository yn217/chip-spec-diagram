module ddr_ctrl #(parameter AW=40, parameter DW=256) (
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
    inout  wire [31:0] ddr_dq,
    output wire [15:0] ddr_addr,
    output wire        ddr_ck
);
endmodule
