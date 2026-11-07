"""動作確認用: マクロ・バスが多い SoC の図YAMLを作る(構造は架空)。
  python3 gen_stress.py > stress_soc.yaml
"""
import yaml

clocks = [{"id": "cpu", "label": "cpu", "freq": "2GHz"}, {"id": "noc", "label": "noc", "freq": "1GHz"},
          {"id": "media", "label": "media", "freq": "600MHz"}, {"id": "net", "label": "net", "freq": "500MHz"},
          {"id": "ctl", "label": "ctl", "freq": "400MHz"}, {"id": "ahb", "label": "ahb", "freq": "200MHz"},
          {"id": "apb", "label": "apb", "freq": "100MHz"}, {"id": "ddr", "label": "ddr", "freq": "1.6GHz"}]
blocks, buses, links = [], [], []
base = 0x1000_0000


def alloc(size):
    global base
    a = base
    base += size
    return f"0x{a:08X}/{size // 1024}K" if size < (1 << 20) else f"0x{a:08X}/{size >> 20}M"


# CPU: コア ×8(I$/D$)→ L2$ ×8 → L3$ → NoC
blocks += [
    {"id": "cpu", "label": "CPU Core", "count": 8, "cpu": True, "clock_domain": "cpu",
     "attached": [{"id": "l1i", "label": "I$", "sub": "64KB", "kind": "memory", "ports": [{"id": "m", "role": "M", "width": 128}]},
                  {"id": "l1d", "label": "D$", "sub": "64KB", "kind": "memory", "ports": [{"id": "m", "role": "M", "width": 128}]}]},
    {"id": "l2", "label": "L2$", "sub": "1MB", "count": 8, "kind": "memory", "clock_domain": "cpu",
     "ports": [{"id": "i", "role": "S"}, {"id": "d", "role": "S"}, {"id": "io", "role": "M"}, {"id": "do", "role": "M"}]},
    {"id": "l3", "label": "L3$", "sub": "16MB", "kind": "memory", "clock_domain": "cpu",
     "ports": [{"id": "i", "role": "S"}, {"id": "d", "role": "S"}, {"id": "m", "role": "M", "width": 256}]},
]
links += [{"from": "l1i.m", "to": "l2.i"}, {"from": "l1d.m", "to": "l2.d"},
          {"from": "l2.io", "to": "l3.i"}, {"from": "l2.do", "to": "l3.d"}]
noc = ["l3.m"]
for i, (n, dom) in enumerate([("GPU", "media"), ("NPU", "media"), ("VPU Dec", "media"), ("VPU Enc", "media"),
                               ("ISP", "media"), ("Display", "media"), ("DMA0", "noc"), ("DMA1", "noc")]):
    bid = f"m{i}"
    blocks.append({"id": bid, "label": n, "clock_domain": dom,
                   "ports": [{"id": "m", "role": "M", "width": 128}, {"id": "s", "role": "S", "width": 32}]})
    noc += [f"{bid}.m", {"port": f"{bid}.s", "addr": alloc(1 << 20)}]
blocks += [{"id": "ddrc", "label": "DDR Ctrl", "sub": "4ch", "clock_domain": "ddr", "ports": [{"id": "s", "role": "S", "width": 256}]},
           {"id": "sram", "label": "Sys SRAM", "sub": "4MB", "kind": "memory", "clock_domain": "noc", "ports": [{"id": "s", "role": "S"}]},
           {"id": "rom", "label": "Boot ROM", "sub": "256KB", "kind": "memory", "clock_domain": "noc", "ports": [{"id": "s", "role": "S"}]},
           {"id": "gic", "label": "GIC", "clock_domain": "noc", "ports": [{"id": "s", "role": "S"}]}]
noc += [{"port": "ddrc.s", "addr": "0x8000_0000-0xFFFF_FFFF"}, {"port": "sram.s", "addr": alloc(4 << 20)},
        {"port": "rom.s", "addr": "0x0000_0000/256K"}, {"port": "gic.s", "addr": alloc(1 << 20)}]
buses.append({"id": "noc", "label": "Main NoC", "width": 256, "clock_domain": "noc", "attach": noc})

# サブバス(ネットワーク・制御)と AHB / APB
sub = {}
for bus_id, lab, dom, n in (("net", "Net AXI", "net", 6), ("ctl", "Ctrl AXI", "ctl", 5)):
    att = []
    blocks.append({"id": f"{bus_id}cpu", "label": f"{lab.split()[0]} MCU", "cpu": True, "clock_domain": dom,
                   "ports": [{"id": "m", "role": "M", "width": 64}]})
    att.append(f"{bus_id}cpu.m")
    rng = []
    for k in range(n):
        bid = f"{bus_id}{k}"
        blocks.append({"id": bid, "label": f"{lab.split()[0]}{k}", "clock_domain": dom, "ports": [{"id": "s", "role": "S"}]})
        r = alloc(64 << 10)
        rng.append(r)
        att.append({"port": f"{bid}.s", "addr": r})
    buses.append({"id": bus_id, "label": lab, "width": 64, "clock_domain": dom, "attach": att})
    sub[bus_id] = rng
ahb_r, apb_r = [], []
ahb = ["x2h.m"]
for k in range(6):
    bid = f"ahb{k}"
    blocks.append({"id": bid, "label": ["QSPI", "SDIO", "Crypto", "TRNG", "USB2", "CAN"][k], "clock_domain": "ahb",
                   "ports": [{"id": "s", "role": "S"}]})
    r = alloc(64 << 10)
    ahb_r.append(r)
    ahb.append({"port": f"{bid}.s", "addr": r})
apb = ["h2p.m"]
for k in range(16):
    bid = f"apb{k}"
    blocks.append({"id": bid, "label": ["UART", "I2C", "SPI", "GPIO", "PWM", "WDT", "TMR", "RTC"][k % 8] + str(k // 8),
                   "clock_domain": "apb", "ports": [{"id": "s", "role": "S"}]})
    r = alloc(4 << 10)
    apb_r.append(r)
    apb.append({"port": f"{bid}.s", "addr": r})
ahb.append({"port": "h2p.s", "addr": apb_r})
blocks += [{"id": "x2h", "label": "AXI2AHB", "ports": [{"id": "s", "role": "S", "clock_domain": "noc"}, {"id": "m", "role": "M", "clock_domain": "ahb"}]},
           {"id": "h2p", "label": "AHB2APB", "ports": [{"id": "s", "role": "S", "clock_domain": "ahb"}, {"id": "m", "role": "M", "clock_domain": "apb"}]}]
noc.append({"port": "x2h.s", "addr": ahb_r + apb_r})
buses += [{"id": "ahb", "label": "AHB", "width": 32, "clock_domain": "ahb", "attach": ahb},
          {"id": "apb", "label": "APB", "width": 32, "clock_domain": "apb", "attach": apb}]
spec = {
    "chip": {"label": "大規模 SoC(動作確認用・架空)"},
    "clocks": {"domains": clocks},
    "buses": buses,
    "bus_links": [{"between": ["noc", "net"], "to_b": sub["net"], "to_a": "default"},
                  {"between": ["noc", "ctl"], "to_b": sub["ctl"], "to_a": "default"}],
    "blocks": blocks,
    "links": links,
    "latency": {"accesses": [{"from": "cpu", "to": "apb3", "op": "read", "title": "CPU → GPIO0(AXI→AHB→APB)リード"}]},
}
print("# 自動生成(gen_stress.py): マクロ・バスが多い時の確認用。構造は架空")
print(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=200))
