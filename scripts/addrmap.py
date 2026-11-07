"""アドレス範囲の解釈と書式(図とメモリマップで共通)"""
import re


def parse_addr(v):
    """'0x3000_0000-0x303F_FFFF' / '0x3000_0000/4M' / {base, size} → (base, end) 端を含む"""
    if v is None:
        return None
    if isinstance(v, dict):
        b = int(str(v["base"]).replace("_", ""), 0)
        return (b, b + size_bytes(v["size"]) - 1)
    s = str(v).replace("_", "").replace(" ", "")
    m = re.match(r"^(0x[0-9a-fA-F]+|\d+)(?:-|–|～|~)(0x[0-9a-fA-F]+|\d+)$", s)
    if m:
        return (int(m.group(1), 0), int(m.group(2), 0))
    m = re.match(r"^(0x[0-9a-fA-F]+|\d+)/(.+)$", s)
    if m:
        b = int(m.group(1), 0)
        return (b, b + size_bytes(m.group(2)) - 1)
    raise ValueError(f"アドレス範囲の書式が不明: {v}")


def size_bytes(v):
    if isinstance(v, int):
        return v
    s = str(v).strip().upper().replace("B", "")
    m = re.match(r"^(0X[0-9A-F]+|[\d.]+)\s*([KMGT]?)$", s)
    if not m:
        raise ValueError(f"サイズの書式が不明: {v}")
    n = int(m.group(1), 0) if m.group(1).startswith("0X") else float(m.group(1))
    return int(n * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2)])


def hx(v, digits=None):
    """0x3000_0000 形式(4桁区切り)"""
    d = digits or (8 if v < 1 << 32 else 10)
    s = f"{v:0{d}X}"
    parts = []
    while s:
        parts.insert(0, s[-4:])
        s = s[:-4]
    return "_".join(parts)


def fmt_size(n):
    for u, k in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= k:
            return f"{n // k}{u}B" if n % k == 0 else f"{n / k:.1f}{u}B"
    return f"{n}B"


def range_text(r):
    return f"{hx(r[0])}–{hx(r[1])}"


SPACE = (0, (1 << 40) - 1)   # 検査に使うアドレス空間(40bit)


def parse_ranges(v):
    """addr の値 → [(lo,hi), ...] / ["default"](そのバスで他にデコードされない全て)"""
    if v is None:
        return []
    if isinstance(v, str) and v.strip().lower() in ("default", "その他", "others"):
        return ["default"]
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out += parse_ranges(x)
        return out
    return [parse_addr(v)]


def norm(rs):
    rs = sorted(r for r in rs if r != "default")
    out = []
    for lo, hi in rs:
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def inter(a, b):
    out = []
    for lo, hi in norm(a):
        for lo2, hi2 in norm(b):
            l, h = max(lo, lo2), min(hi, hi2)
            if l <= h:
                out.append((l, h))
    return norm(out)


def subtract(a, b):
    out = norm(a)
    for lo2, hi2 in norm(b):
        nxt = []
        for lo, hi in out:
            if hi < lo2 or lo > hi2:
                nxt.append((lo, hi))
            else:
                if lo < lo2:
                    nxt.append((lo, lo2 - 1))
                if hi > hi2:
                    nxt.append((hi2 + 1, hi))
        out = nxt
    return out


def total(rs):
    return sum(h - l + 1 for l, h in norm(rs))
