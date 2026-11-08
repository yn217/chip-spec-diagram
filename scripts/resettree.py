#!/usr/bin/env python3
"""resettree — リセット系統図(リセット源 → リセット制御(OR・伸長・PLL ロック待ち) → 同期化 → リセットドメイン)
と解除シーケンスの波形、リセットドメイン間の乗り換え(RDC)の一覧。

  python3 resettree.py diagram.yaml -o out/ [--png]

diagram.yaml の resets: を使う
  resets:
    default: rst_sys                 # reset_domain を書いていないブロック・バスの所属
    sources:
      - {id: por,   label: POR,        kind: por}    # por | bod | pad | wdt | sw | dbg
      - {id: rst_n, label: RST_N,      kind: pad, polarity: low}
      - {id: wdt,   label: WDT タイムアウト, kind: wdt, type: sync, from: wdog}   # type: async|sync、同期なら発行クロック
                                                     # (clock_domain / clock / 発行元ブロック from のクロック)
    domains:
      - id: rst_sys
        label: system
        sources: [por, rst_n, wdt]   # どれかが入るとこのドメインがリセットされる(OR)
        clock_domain: core_clk       # 解除をこのクロックに同期させる(clocks.domains の id)
        sync: 2                      # 同期化の段数(非同期アサート・同期解除)。0 = 同期化なし
        stretch: 16                  # 伸長サイクル(解除を遅らせる)
        wait: [pll_sys]              # 解除前に待つ PLL ロック(clocks.plls の id)
        after: []                    # 先に解除されている必要があるドメイン
        isolation: false             # 単独リセット時にバスを切り離す仕組み(アイソレーション/静止化)がある
        style: async                 # async = 非同期アサート・同期解除(既定)/ sync = 同期リセット
  block/bus に reset_domain: rst_xxx を書く。子ブロックは親を継ぐ。

チェック(lint):
  [リセット未割当] [未定義] [順序](after の循環) [同期](段数 < 2) [PLL](PLL 由来のクロックなのにロック待ちがない)
  [リセット発行](同期リセットなのに発行クロックが未決)
  [自己リセット] 発行元が自分のドメインをリセットするのに伸長がない(直接)/ 同期の源がリセット制御の入っているドメインを
                リセットする(間接: 制御の伸長・シーケンスごと消える)。resets.controller: {always_on: true | domain: | block: }
  [RDC] 片側だけが単独でリセットされうる接続(相手が動いている時に X/グリッチが届く)。isolation: true なら対策ありとして表示のみ
"""
import os, sys, re, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, load, esc, wlen, INK, to_png

PAL = ["#2563eb", "#9333ea", "#ea580c", "#db2777", "#0891b2", "#16a34a", "#ca8a04"]
KIND = {"por": "電源投入", "bod": "電圧低下", "pad": "外部端子", "wdt": "ウォッチドッグ", "sw": "ソフトウェア",
        "dbg": "デバッガ"}
RDC_C, OK_C, GRAY = "#dc2626", "#16a34a", "#64748b"


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def amp(v):
    m = re.fullmatch(r"\s*([\d.]+)\s*(mA|A)\s*", str(v)) if v is not None else None
    return float(m.group(1)) * (1e-3 if m.group(2) == "mA" else 1) if m else None


def volt(v):
    m = re.fullmatch(r"\s*([\d.]+)\s*(mV|V)\s*", str(v)) if v is not None else None
    return float(m.group(1)) * (1e-3 if m.group(2) == "mV" else 1) if m else None


def mv_per_a(v):
    m = re.fullmatch(r"\s*([\d.]+)\s*mV/A\s*", str(v)) if v is not None else None
    return float(m.group(1)) * 1e-3 if m else None


def fa(a):
    return f"{a:.2f}".rstrip("0").rstrip(".") + "A" if a >= 1 else f"{a * 1000:.0f}mA"


class ResetTree:
    def __init__(self, c):
        self.c = c
        r = c.s.get("resets", {}) or {}
        self.srcs = r.get("sources", [])
        self.doms = r.get("domains", [])
        self.default = r.get("default")
        self.ctl = r.get("controller") or {}       # リセット制御の置き場所 {always_on: true} / {domain: rst_x} / {block: id}
        self.warn, self.info = [], []
        self.sid = {s["id"]: s for s in self.srcs}
        self.did = {d["id"]: d for d in self.doms}
        ck = c.s.get("clocks", {}) or {}
        self.ckd = {d["id"]: d for d in ck.get("domains", [])}
        self.plls = {p["id"]: p for p in ck.get("plls", [])}
        for i, d in enumerate(self.doms):
            d.setdefault("_col", d.get("color") or self.ckd.get(d.get("clock_domain"), {}).get("color") or PAL[i % len(PAL)])

    # ---------- 所属 ----------
    def dom_of_block(self, bid):
        b = self.c.blocks.get(bid)
        while b is not None:
            if b.get("reset_domain"):
                return b["reset_domain"]
            nxt = b.get("_att_of") or b.get("_parent")
            b = self.c.blocks.get(nxt) if nxt else None
        return self.default

    def dom_of_bus(self, bus):
        return bus.get("reset_domain") or self.default

    def members(self):
        mem = {d["id"]: [] for d in self.doms}
        for b in self.c.blocks.values():
            if b.get("_ext") or b.get("virtual") or b.get("_hidden"):
                continue
            own = b.get("reset_domain")
            par = b.get("_att_of") or b.get("_parent")
            if par and not own:
                continue                                   # 親と同じ: 親の札にまとめる
            d = self.dom_of_block(b["id"])
            lab = b.get("label", b["id"])
            if par:
                lab = f'{self.c.blocks[par].get("label", par)} / {lab}'
            if d is None:
                self.warn.append(f"[リセット未割当] {lab}: reset_domain がなく resets.default もない")
            elif d not in mem:
                self.warn.append(f"[未定義] {lab}: reset_domain {d} が resets.domains にない")
            else:
                mem[d].append(lab)
        for bus in self.c.buses.values():
            d = self.dom_of_bus(bus)
            lab = bus.get("label", bus["id"])
            if d is None:
                self.warn.append(f"[リセット未割当] バス {lab}: reset_domain がなく resets.default もない")
            elif d not in mem:
                self.warn.append(f"[未定義] バス {lab}: reset_domain {d} が resets.domains にない")
            else:
                mem[d].append("⇄ " + lab)
        return mem

    # ---------- チェック ----------
    def src_info(self, x):
        """源の 非同期/同期、発行クロック(ラベル, 色)、発行元ブロック"""
        k = x.get("kind")
        typ = x.get("type") or ("async" if k in ("por", "bod", "pad") else "sync")
        ck = None
        cd = x.get("clock_domain")
        b = self.c.blocks.get(x.get("from")) if x.get("from") else None
        if not cd and b is not None:                     # 発行元ブロックのクロックを使う
            o = b
            while o is not None and not o.get("clock_domain") and o.get("_parent"):
                o = self.c.blocks.get(o["_parent"])
            cd = o.get("clock_domain") if o else None
        if cd and cd in self.ckd:
            d = self.ckd[cd]
            ck = (f'{d.get("label", cd)} {d.get("freq", "")}'.strip(), d.get("color", GRAY))
        elif x.get("clock"):
            ck = (str(x["clock"]), GRAY)
        elif cd:
            ck = (cd, GRAY)
        return typ, ck, (b.get("label", b["id"]) if b is not None else x.get("from"))

    def check(self):
        for d in self.doms:
            for s in d.get("sources", []):
                if s not in self.sid:
                    self.warn.append(f"[未定義] {d['id']}: リセット源 {s} が resets.sources にない")
            for a in d.get("after", []):
                if a not in self.did:
                    self.warn.append(f"[未定義] {d['id']}: after {a} が resets.domains にない")
            for p in d.get("wait", []):
                if p not in self.plls:
                    self.warn.append(f"[未定義] {d['id']}: wait {p} が clocks.plls にない")
            cd = d.get("clock_domain")
            if cd and self.ckd and cd not in self.ckd:
                self.warn.append(f"[未定義] {d['id']}: clock_domain {cd} が clocks.domains にない")
            if cd and int(d.get("sync", 2)) < 2:
                self.warn.append(f"[同期] {d['id']}: 解除の同期化が {d.get('sync')} 段。メタステーブル対策に 2 段以上")
            if not cd:
                self.info.append(f"[info] {d['id']}: clock_domain がない(解除を同期化するクロックが未決)")
            if cd in self.ckd:
                src = self.ckd[cd].get("from")
                if src in self.plls and src not in d.get("wait", []) and not self._waits_via_after(d, src):
                    self.warn.append(f"[PLL] {d['id']}: クロック {cd} は {self.plls[src].get('label', src)} 由来だが、"
                                     f"ロックを待たずに解除されうる(wait: [{src}] を追加)")
        for x in self.srcs:
            typ, ck, frm = self.src_info(x)
            lab = x.get("label", x["id"])
            if typ not in ("async", "sync"):
                self.warn.append(f"[リセット発行] {lab}: type は async か sync")
            if typ == "sync" and ck is None:
                self.warn.append(f"[リセット発行] {lab}: 同期リセットだが、発行するクロックが未決(clock_domain / clock / from を書く)")
            if x.get("from") and x["from"] not in self.c.blocks:
                self.warn.append(f"[未定義] {lab}: 発行元 {x['from']} がブロックにない")
            if x.get("clock_domain") and self.ckd and x["clock_domain"] not in self.ckd:
                self.warn.append(f"[未定義] {lab}: clock_domain {x['clock_domain']} が clocks.domains にない")
            # 自分が入っているドメインを自分でリセットする: パルスを保持しないと途中で消える
            if x.get("from") in self.c.blocks:
                own = self.dom_of_block(x["from"])
                for d in self.doms:
                    if d["id"] == own and x["id"] in d.get("sources", []) and not d.get("stretch"):
                        self.warn.append(f"[自己リセット] {lab}: 発行元 {frm} は {self.dl(own)} に入っていて、そのドメインを自分でリセットする。"
                                         f"発行元も途中でリセットされるので、リセット制御でパルスを保持する(stretch を書く)")
        # リセット制御そのものがリセットされる(間接の自己リセット): 伸長のカウンタや保持も消える
        cdom = self.ctl.get("domain") or (self.dom_of_block(self.ctl["block"]) if self.ctl.get("block") in self.c.blocks else None)
        if self.ctl.get("block") and self.ctl["block"] not in self.c.blocks:
            self.warn.append(f"[未定義] resets.controller.block {self.ctl['block']} がブロックにない")
        if cdom and cdom not in self.did:
            self.warn.append(f"[未定義] resets.controller.domain {cdom} が resets.domains にない")
        if not self.ctl:
            self.info.append("[info] リセット制御の置き場所(resets.controller)が未決。常時オン(POR だけでリセット)なら always_on: true")
        elif cdom in self.did:
            for x in self.srcs:
                typ, _, frm = self.src_info(x)
                if typ == "sync" and x["id"] in self.did[cdom].get("sources", []):
                    self.warn.append(f"[自己リセット] {x.get('label', x['id'])}: リセット制御が入っている {self.dl(cdom)} をリセットする。"
                                     f"制御自身もリセットされ、伸長やシーケンスが途中で消える。制御を常時オンに置くか、この源から外す")
        for d in self.doms:
            st = d.get("style", "async")
            if st not in ("async", "sync"):
                self.warn.append(f"[同期] {d['id']}: style は async(非同期アサート・同期解除)か sync(同期リセット)")
            if st == "sync" and not d.get("clock_domain"):
                self.warn.append(f"[同期] {d['id']}: 同期リセットのドメインはクロックが要る(clock_domain)")
        used = {s for d in self.doms for s in d.get("sources", [])}
        for s in self.srcs:
            if s["id"] not in used:
                self.info.append(f"[info] リセット源 {s.get('label', s['id'])} はどのドメインにもつながっていない")
        self.step = self.order()
        ranks = sorted(set(self.step.values()))
        self.rank = {k: ranks.index(v) + 1 for k, v in self.step.items()}
        self.power()
        self.rdc = self.find_rdc()

    def _waits_via_after(self, d, pll, seen=None):
        seen = seen or set()
        for a in d.get("after", []):
            if a in seen or a not in self.did:
                continue
            seen.add(a)
            if pll in self.did[a].get("wait", []) or self._waits_via_after(self.did[a], pll, seen):
                return True
        return False

    def order(self):
        """解除の段: PLL ロック待ちがあれば 1 段目以降、after の後ろ"""
        st, busy = {}, set()
        def go(i):
            if i in st:
                return st[i]
            if i in busy:
                self.warn.append(f"[順序] after が循環している: {i}")
                return 0
            busy.add(i)
            d = self.did[i]
            s = 2 if d.get("wait") else 1
            for a in d.get("after", []):
                if a in self.did:
                    s = max(s, go(a) + 1)
            busy.discard(i)
            st[i] = s
            return s
        for d in self.doms:
            go(d["id"])
        return st

    def find_rdc(self):
        """リセットドメインをまたぐ接続。X だけをリセットする源があると、Y は動いたまま X の出力が乱れる"""
        c, pairs = self.c, {}
        def add(x, y, via):
            if not x or not y or x == y or x not in self.did or y not in self.did:
                return
            k = tuple(sorted((x, y)))
            pairs.setdefault(k, set()).add(via)
        for bus in c.buses.values():
            db = self.dom_of_bus(bus)
            for a in bus.get("attach", []):
                if a not in c.anch or c.anch[a][3] is None:
                    continue
                bid = c.anch[a][3]
                if c.blocks.get(bid, {}).get("_ext"):
                    continue
                add(self.dom_of_block(bid), db, f'{c.blocks[bid].get("label", bid)}↔{bus.get("label", bus["id"])}')
        for ln in c.s.get("links", []):
            A, B = c.anch.get(ln["from"]), c.anch.get(ln["to"])
            if not A or not B or A[3] is None or B[3] is None:
                continue
            if c.blocks.get(A[3], {}).get("_ext") or c.blocks.get(B[3], {}).get("_ext"):
                continue
            add(self.dom_of_block(A[3]), self.dom_of_block(B[3]),
                f'{c.blocks[A[3]].get("label", A[3])}→{c.blocks[B[3]].get("label", B[3])}')
        out = []
        for (x, y), via in sorted(pairs.items()):
            sx, sy = set(self.did[x].get("sources", [])), set(self.did[y].get("sources", []))
            alone = []                                     # 単独でリセットされる側と、その源
            for a, sa, sb in ((x, sx, sy), (y, sy, sx)):
                only = sorted(sa - sb)
                if only:
                    alone.append((a, only))
            if not alone:
                continue
            iso = bool(self.did[x].get("isolation") or self.did[y].get("isolation"))   # 境界の切り離しはどちらの側にあってもよい
            ok = iso
            out.append({"a": x, "b": y, "via": sorted(via), "alone": alone, "ok": ok})
            if not ok:
                for a, only in alone:
                    lab = "・".join(self.sid.get(s, {}).get("label", s) for s in only)
                    other = y if a == x else x
                    self.warn.append(f"[RDC] {self.dl(a)} は {lab} で単独リセットされるが、{self.dl(other)} は動いたまま"
                                     f"({', '.join(sorted(via))})。バスの切り離し/静止化を入れるか isolation: true")
        return out

    def power(self):
        """電源レールごとの電流の跳ね上がり(解除・クロック開始・電源スイッチの突入)を段ごとに集計し、許容値・BOD と比べる"""
        pw = self.c.s.get("power", {}) or {}
        self.rails = {r["id"]: r for r in pw.get("rails", [])}
        self.pev = {r: [] for r in self.rails}          # rail -> [(t, dI, domain, 種類)]
        if not self.rails:
            return
        for d in self.doms:
            r = d.get("rail")
            if not r:
                self.info.append(f"[info] {self.dl(d['id'])}: 電源レール(rail)が未決")
                continue
            if r not in self.rails:
                self.warn.append(f"[未定義] {self.dl(d['id'])}: rail {r} が power.rails にない")
                continue
            t = self.step.get(d["id"], 1) + .5
            ps = d.get("power_switch")
            if ps:
                if not d.get("isolation"):
                    self.warn.append(f"[電源順] {self.dl(d['id'])}: 電源スイッチがあるのに切り離し(isolation)がない。"
                                     f"電源 ON → 切り離し解除 → クロック → リセット解除 の順に、電源が切れている間は出力を固定する")
                ir = amp(ps.get("inrush") if isinstance(ps, dict) else None)
                if ir:
                    self.pev[r].append((round(t - .38, 3), ir, d, "突入"))
            ld = amp(d.get("load"))
            if ld is None:
                self.info.append(f"[info] {self.dl(d['id'])}: 解除時の電流(load)が未決")
                continue
            n = int(d.get("stagger", 1) or 1)
            for k in range(n):
                self.pev[r].append((round(t + k * .5 / n, 3), ld / n, d, "解除"))
        self.psum = {}
        for r, ev in self.pev.items():
            rail = self.rails[r]
            tot = {}
            for t, a, d, k in ev:
                tot.setdefault(t, []).append((a, d, k))
            self.psum[r] = tot
            mx = amp(rail.get("max_step"))
            dpa = mv_per_a(rail.get("droop"))
            worst = max((sum(a for a, _, _ in v) for v in tot.values()), default=0)
            for t, v in sorted(tot.items()):
                a = sum(x for x, _, _ in v)
                if mx and a > mx:
                    who = "・".join(dict.fromkeys(f'{self.dl(d["id"])}({k})' for _, d, k in v))
                    self.warn.append(f"[ドロップ] {rail.get('label', r)}: {who} が同時に動き ΔI≈{fa(a)} > 許容 {fa(mx)}。"
                                     f"stagger(段階解除)か after で解除をずらす")
            if not mx:
                self.info.append(f"[info] {rail.get('label', r)}: 許容できる電流の跳ね上がり(max_step)が未決")
            v0 = volt(rail.get("volt"))
            for x in self.srcs:                           # 電圧低下リセットとのループ
                if x.get("kind") != "bod" or x.get("rail") != r:
                    continue
                th = volt(x.get("threshold"))
                if not (v0 and th and dpa):
                    self.info.append(f"[info] {x.get('label', x['id'])}: しきい値・レール電圧・droop(mV/A)がそろわず、ループを確認できない")
                    continue
                low = v0 - worst * dpa
                margin = low - th - (volt(x.get("hyst")) or 0)
                rail["_bod"] = (x, th, low, margin)
                if margin < 0:
                    self.warn.append(f"[ドロップ] {rail.get('label', r)}: 解除時の電圧 ≈{low * 1000:.0f}mV が {x.get('label', x['id'])} の"
                                     f"しきい値 {th * 1000:.0f}mV を下回る。解除 → 電圧低下 → リセット → 解除 のループになる")

    def ctl_label(self):
        if self.ctl.get("always_on"):
            return "(常時オン)"
        if self.ctl.get("domain"):
            return f'({self.dl(self.ctl["domain"]) if self.ctl["domain"] in self.did else self.ctl["domain"]})'
        if self.ctl.get("block"):
            b = self.c.blocks.get(self.ctl["block"], {})
            return f'({b.get("label", self.ctl["block"])})'
        return "(置き場所 未決)"

    def rdc_text(self):
        """RDC の説明と一覧(図の右下の枠と reset.md で共通)"""
        head = ["RDC(Reset Domain Crossing)= リセットドメインをまたぐ信号",
                "送り側だけがリセットされると、出力が途中で変わる。動いている受け側に X・グリッチ・途中で切れた転送が届き、"
                "誤動作やバスの停止につながる(クロックが同じでも起きる。CDC とは別の問題)。",
                "対策: 送り側をリセットする前にバスを静止化する(新しい転送を止め、残りの完了を待つ)/ 境界で切り離す"
                "(アイソレーション: 出力を既定値に固定)/ 受け側で取り込みを止める。",
                "図: 赤破線 = 対策なし(要対策)、緑破線 = 切り離しあり(isolation: true)。数字は経路の数。"]
        rows = []
        for r in self.rdc:
            al = "、".join(f'{self.dl(a)} が {"・".join(self.sid.get(x, {}).get("label", x) for x in only)} で単独リセット'
                          for a, only in r["alone"])
            rows.append((r["ok"], f'{self.dl(r["a"])} ↔ {self.dl(r["b"])}: {al}({", ".join(r["via"])})'))
        return head, rows

    def dl(self, i):
        return self.did[i].get("label", i)

    # ---------- 描画 ----------
    def svg(self, title="リセット系統図"):
        c, doms, srcs = self.c, self.doms, self.srcs
        mem = self.members()
        o, ow = [], []                                   # ow: 線(下層)
        # 列: リセット源ごとに 1 列。源の札を列の上に置き、縦線から各ドメインの OR へ右に枝を出す
        use = {x["id"]: sum(x["id"] in d.get("sources", []) for d in doms) for x in srcs}
        srcs = sorted(srcs, key=lambda x: (-use[x["id"]], self.srcs.index(x)))   # 多く使う源ほど左(短い列が右に来て交差が減る)
        ns = len(srcs)
        def wrap(t, wmax):
            import re as _re
            toks = [x for x in _re.split(r"(?<=\s)|(?=[(（])", t) if x]   # 空白の後・括弧の前で切る
            out, cur = [], ""
            for tk in toks:
                if cur and wlen(cur + tk) * 6.4 > wmax:
                    out.append(cur.strip()); cur = ""
                while wlen(tk) * 6.4 > wmax:              # 1 語が長すぎる時だけ文字で切る
                    k = len(tk)
                    while k > 1 and wlen(tk[:k]) * 6.4 > wmax:
                        k -= 1
                    out.append(tk[:k]); tk = tk[k:]
                cur += tk
            if cur.strip():
                out.append(cur.strip())
            return out[:3]
        CS = 116
        BW = CS - 12
        labs = {x["id"]: wrap(x.get("label", x["id"]), BW - 10) for x in srcs}
        sinfo = {x["id"]: self.src_info(x) for x in srcs}
        BH = 50 + max(len(v) for v in labs.values()) * 14 + (12 if any(v[2] for v in sinfo.values()) else 0) if srcs else 40
        chip_x = 24
        X_C0 = chip_x + 20
        colx = {x["id"]: X_C0 + i * CS + CS / 2 for i, x in enumerate(srcs)}
        has_ext = any(x.get("kind") == "pad" for x in srcs)
        EXT_Y = 44
        CT = EXT_Y + BH + 22 if has_ext else 50          # チップ枠の上辺
        INT_Y = CT + 14
        ROW0 = INT_Y + BH + 34
        X_CTL = X_C0 + ns * CS + 8                         # リセット制御の枠
        GX = X_CTL + 22                                   # OR(IEEE Std 91 の形: 奥行き = √3/2 × 高さ)
        GW = 0.866 * max([max(len([x for x in d.get("sources", []) if x in self.sid]) * 14 + 8, 30) for d in doms] + [30])
        X_ST = GX + GW + 14                               # 伸長・PLL 待ちの文字
        X_SY = X_CTL + 236                                # 同期化
        W_SY = 150
        X_DOM = X_SY + W_SY + 64
        W_DOM = 360
        SPAL = ["#334155", "#b91c1c", "#0369a1", "#a16207", "#7e22ce", "#15803d", "#c2410c", "#be185d", "#0f766e"]
        scol = {x["id"]: x.get("color") or SPAL[i % len(SPAL)] for i, x in enumerate(srcs)}

        def chips(names, width):
            rows, x, cur = [], 0, []
            for m in names:
                w = min(wlen(m) * 6.6 + 20, width)
                if cur and x + w > width:
                    rows.append(cur); cur, x = [], 0
                cur.append((m, x, w)); x += w + 6
            if cur:
                rows.append(cur)
            return rows
        y = ROW0
        pos, crow = {}, {}
        for d in doms:
            crow[d["id"]] = chips(mem[d["id"]], W_DOM - 24)
            k = len([x for x in d.get("sources", []) if x in self.sid])
            h = max(40 + max(1, len(crow[d["id"]])) * 24 + (16 if d.get("rail") or d.get("load") else 0), k * 14 + 30, 78)
            pos[d["id"]] = (y, h)
            y += h + 14
        ctl_bot = y
        top_h = ctl_bot + 10
        # RDC 列
        rdc = sorted(self.rdc, key=lambda r: abs(pos[r["a"]][0] - pos[r["b"]][0]))
        xr = X_DOM + W_DOM
        lanes = len(rdc)
        W = xr + 30 + lanes * 40 + 30
        # 波形
        WV_Y = top_h + 40
        nst = max(self.step.values() or [1])
        wav_rows = [("src", x) for x in srcs if x.get("kind") in ("por", "pad")] + \
                   [("pll", p) for p in sorted({p for d in doms for p in d.get("wait", []) if p in self.plls})] + \
                   [("dom", d) for d in doms]
        SW_ = 140
        X0w = 230
        PX = X0w + (nst + 1) * SW_ + 50                  # RDC の説明の枠
        PW = 600
        def wrapw(t, wmax, k=5.7):
            out, cur = [], ""
            for ch in t:
                if cur and wlen(cur + ch) * k > wmax:
                    out.append(cur); cur = ""
                cur += ch
            out.append(cur)
            return out
        head, rrows = self.rdc_text()
        plines = [("h", head[0])] + [("p", t) for x in head[1:] for t in wrapw(x, PW - 28)]
        rl = []
        for ok, x in rrows:
            for j, t in enumerate(wrapw(x, PW - 44)):
                rl.append((("ok" if ok else "ng") if j == 0 else "c", t))
        plines += [("s", "この図の RDC")] + (rl or [("p", "なし")])
        PH = 24 + len(plines) * 17 + 10
        rails = [r for r in getattr(self, "rails", {}) if self.pev.get(r)]
        RH = 96
        CH_Y = WV_Y + 40 + len(wav_rows) * 30 + 40          # 電流の段階図
        H = max(WV_Y + 40 + max(len(wav_rows) * 30, PH) + 60, CH_Y + (len(rails) * RH + 50 if rails else 0) + 40)
        W = max(W, 980, PX + PW + 20)

        o.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(W)} {f(H)}" font-family="\'Noto Sans JP\',sans-serif">'
                 f'<rect width="100%" height="100%" fill="#fff"/>'
                 f'<defs><marker id="rsa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                 f'markerUnits="userSpaceOnUse" orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="{INK}"/></marker></defs>'
                 f'<text x="12" y="28" font-size="17" font-weight="800" fill="{INK}">{esc(title)}</text>')
        chip_r = xr + 30 + lanes * 40
        o.append(f'<rect x="{f(chip_x)}" y="{f(CT)}" width="{f(chip_r - chip_x)}" height="{f(top_h - CT)}" rx="6" fill="#fcfcfd" '
                 f'stroke="{INK}" stroke-width="2.2"/><text x="{f(X_CTL)}" y="{f(CT + 20)}" font-size="13" font-weight="800" '
                 f'fill="{INK}">{esc(c.s.get("chip", {}).get("label", "chip"))}</text>')
        # リセット制御の枠
        o.append(f'<rect x="{f(X_CTL)}" y="{f(ROW0 - 22)}" width="{f(X_SY - 18 - X_CTL)}" height="{f(ctl_bot - ROW0 + 18)}" rx="5" '
                 f'fill="#f8fafc" stroke="#475569" stroke-width="1.5" stroke-dasharray="6 3"/><text x="{f(X_CTL + 8)}" '
                 f'y="{f(ROW0 - 8)}" font-size="11.5" font-weight="800" fill="#475569">リセット制御{esc(self.ctl_label())}</text>')
        o.append("<!--LINES-->")
        # 枝(列 → OR)
        sidx = {x["id"]: i for i, x in enumerate(srcs)}
        joins = {x["id"]: [] for x in srcs}
        branches = []
        for d in doms:
            y0, h = pos[d["id"]]
            ins = [x for x in d.get("sources", []) if x in self.sid]
            ins.sort(key=lambda x: sidx[x])
            k = len(ins)
            gh = max(k * 14 + 8, 30)
            gy = y0 + h / 2 - gh / 2
            d["_g"] = (GX, gy, 0.866 * gh, gh)
            for j, x in enumerate(ins):
                iy = gy + gh / 2 + (j - (k - 1) / 2) * 14
                joins[x].append(iy)
                if k > 1:
                    xb = GX - .866 * gh + (gh * gh - (iy - gy - gh / 2) ** 2) ** .5    # 後ろの円弧(半径 = 高さ)
                else:
                    xb = X_SY
                branches.append((colx[x], iy, xb, scol[x]))
        # 源の札と縦線(列)
        for x in srcs:
            cx_ = colx[x["id"]]
            pad = x.get("kind") == "pad"
            by = EXT_Y if pad else INT_Y
            lab = labs[x["id"]]
            sub = KIND.get(x.get("kind"), x.get("kind", ""))
            if x.get("polarity") == "low":
                sub += "・負論理"
            typ, ck, frm = sinfo[x["id"]]
            col = scol[x["id"]]
            dash = ' stroke-dasharray="5 4"' if pad else ""
            o.append(f'<rect x="{f(cx_ - BW / 2)}" y="{f(by)}" width="{f(BW)}" height="{f(BH)}" rx="4" fill="#fff" stroke="{col}" '
                     f'stroke-width="1.6"{dash}/>')
            for j, t in enumerate(lab):
                o.append(f'<text x="{f(cx_)}" y="{f(by + 16 + j * 14)}" font-size="11.5" font-weight="800" fill="{col}" '
                         f'text-anchor="middle">{esc(t)}</text>')
            ty = by + 16 + len(lab) * 14
            o.append(f'<text x="{f(cx_)}" y="{f(ty - 2)}" font-size="9.5" fill="{GRAY}" text-anchor="middle">{esc(sub)}</text>')
            # 非同期 / 同期(発行クロック)
            if typ == "async":
                o.append(f'<rect x="{f(cx_ - 24)}" y="{f(ty + 3)}" width="48" height="15" rx="7.5" fill="#fff" stroke="#475569" '
                         f'stroke-width="1"/><text x="{f(cx_)}" y="{f(ty + 14)}" font-size="9.5" font-weight="800" fill="#475569" '
                         f'text-anchor="middle">非同期</text>')
            else:
                ckl, ckc = ck if ck else ("クロック未決", "#b45309")
                tw = min(BW - 6, wlen("同期 " + ckl) * 5.6 + 12)
                o.append(f'<rect x="{f(cx_ - tw / 2)}" y="{f(ty + 3)}" width="{f(tw)}" height="15" rx="7.5" fill="{ckc}" fill-opacity=".12" '
                         f'stroke="{ckc}" stroke-width="1"/><text x="{f(cx_)}" y="{f(ty + 14)}" font-size="9" font-weight="800" '
                         f'fill="{ckc if ck else "#b45309"}" text-anchor="middle">同期 {esc(ckl)}</text>')
            if frm:
                o.append(f'<text x="{f(cx_)}" y="{f(ty + 31)}" font-size="9.5" fill="{INK}" text-anchor="middle">発行: {esc(frm)}</text>')
            if pad:
                o.append(f'<rect x="{f(cx_ - 8)}" y="{f(CT - 8)}" width="16" height="16" fill="#cbd5e1" stroke="{INK}" stroke-width="1.5"/>')
            if joins[x["id"]]:
                ow.append(f'<path d="M{f(cx_)},{f(by + BH)} L{f(cx_)},{f(max(joins[x["id"]]))}" stroke="{col}" stroke-width="2" fill="none"/>')
        # 枝は白い縁取りの上に描く(交差する列が途切れて見え、つながっていないことがわかる)
        for tx, iy, xb, col in branches:
            ow.append(f'<path d="M{f(tx + 4)},{f(iy)} L{f(xb)},{f(iy)}" stroke="#fcfcfd" stroke-width="7" fill="none"/>'
                      f'<path d="M{f(tx)},{f(iy)} L{f(xb)},{f(iy)}" stroke="{col}" stroke-width="1.6" fill="none"/>'
                      f'<circle cx="{f(tx)}" cy="{f(iy)}" r="3.4" fill="{col}"/>')
        # ドメインの行: OR → 伸長 → 同期化 → ドメイン
        for d in doms:
            y0, h = pos[d["id"]]
            col = d["_col"]
            cyd = y0 + h / 2
            gx, gy, gw, gh = d["_g"]
            k = len([s for s in d.get("sources", []) if s in self.sid])
            if k > 1:
                o.append(f'<path d="M{f(gx)},{f(gy)} A{f(gh)},{f(gh)} 0 0 1 {f(gx + gw)},{f(gy + gh / 2)} A{f(gh)},{f(gh)} 0 0 1 {f(gx)},{f(gy + gh)} '
                         f'A{f(gh)},{f(gh)} 0 0 0 {f(gx)},{f(gy)} Z" fill="#fff" stroke="{INK}" stroke-width="1.6"/>')
                ow.append(f'<path d="M{f(gx + gw)},{f(cyd)} L{f(X_SY)},{f(cyd)}" stroke="{INK}" stroke-width="1.6" fill="none" marker-end="url(#rsa)"/>')
            else:
                ow.append(f'<path d="M{f(X_SY - 12)},{f(cyd)} L{f(X_SY)},{f(cyd)}" stroke="{INK}" stroke-width="1.6" fill="none" marker-end="url(#rsa)"/>')
            notes = []
            if d.get("wait"):
                notes.append("ロック待ち " + "・".join(self.plls.get(p, {}).get("label", p) for p in d["wait"]))
            if d.get("stretch"):
                notes.append(f'伸長 {d["stretch"]}cyc')
            for j, t in enumerate(notes):
                o.append(f'<text x="{f(X_ST)}" y="{f(cyd - 8 - (len(notes) - 1 - j) * 13)}" font-size="10.5" font-weight="700" '
                         f'fill="#475569">{esc(t)}</text>')
            # 同期化
            cd = d.get("clock_domain")
            ckd = self.ckd.get(cd, {})
            ccol = ckd.get("color", GRAY)
            sync = int(d.get("sync", 2)) if cd else 0
            st = d.get("style", "async")
            if cd:
                head = f"{sync}段 同期化" if st == "async" else f"{sync}段 同期化・同期リセット"
                foot = "非同期アサート・同期解除" if st == "async" else "アサートもクロックで"
                o.append(f'<rect x="{f(X_SY)}" y="{f(cyd - 30)}" width="{W_SY}" height="60" rx="4" fill="{ccol}" fill-opacity=".08" '
                         f'stroke="{ccol}" stroke-width="1.6"/><text x="{f(X_SY + W_SY / 2)}" y="{f(cyd - 13)}" font-size="11" '
                         f'font-weight="800" fill="{INK}" text-anchor="middle">{esc(head)}</text>'
                         f'<text x="{f(X_SY + W_SY / 2)}" y="{f(cyd + 3)}" font-size="10.5" font-weight="700" fill="#0369a1" '
                         f'text-anchor="middle">{esc(ckd.get("label", cd))} {esc(ckd.get("freq", ""))}</text>'
                         f'<text x="{f(X_SY + W_SY / 2)}" y="{f(cyd + 20)}" font-size="9.5" fill="{GRAY if st == "async" else "#b45309"}" '
                         f'text-anchor="middle">{foot}</text>')
            else:
                o.append(f'<rect x="{f(X_SY)}" y="{f(cyd - 24)}" width="{W_SY}" height="48" rx="4" fill="#fff" stroke="#f59e0b" '
                         f'stroke-width="1.6" stroke-dasharray="4 3"/><text x="{f(X_SY + W_SY / 2)}" y="{f(cyd + 4)}" font-size="11" '
                         f'font-weight="700" fill="#b45309" text-anchor="middle">同期クロック未決</text>')
            sig = d.get("signal") or f'{d["id"]}_n'
            ow.append(f'<path d="M{f(X_SY + W_SY)},{f(cyd)} L{f(X_DOM)},{f(cyd)}" stroke="{col}" stroke-width="2" fill="none" marker-end="url(#rsa)"/>')
            o.append(f'<text x="{f(X_SY + W_SY + 6)}" y="{f(cyd - 6)}" font-size="10.5" font-weight="700" fill="{col}">{esc(sig)}</text>')
            # ドメイン
            o.append(f'<rect x="{f(X_DOM)}" y="{f(y0)}" width="{W_DOM}" height="{f(h)}" rx="6" fill="{col}" fill-opacity=".09" '
                     f'stroke="{col}" stroke-width="2"/><text x="{f(X_DOM + 12)}" y="{f(y0 + 20)}" font-size="13" font-weight="800" '
                     f'fill="{col}">{esc(d.get("label", d["id"]))}{"(仮)" if d.get("assumed") else ""}</text>'
                     f'<text x="{f(X_DOM + W_DOM - 10)}" y="{f(y0 + 20)}" font-size="11" font-weight="800" fill="{INK}" '
                     f'text-anchor="end">解除 {self.rank.get(d["id"], 1)}番目{"・切り離しあり" if d.get("isolation") else ""}</text>')
            for r, row in enumerate(crow[d["id"]]):
                for m, ox, w in row:
                    cx_, cy_ = X_DOM + 12 + ox, y0 + 30 + r * 24
                    o.append(f'<rect x="{f(cx_)}" y="{f(cy_)}" width="{f(w)}" height="19" rx="9.5" fill="#fff" stroke="{col}" stroke-width="1"/>'
                             f'<text x="{f(cx_ + w / 2)}" y="{f(cy_ + 13.5)}" font-size="10.5" fill="{INK}" text-anchor="middle">{esc(m)}</text>')
            if not mem[d["id"]]:
                o.append(f'<text x="{f(X_DOM + 12)}" y="{f(y0 + 44)}" font-size="11" fill="#94a3b8">所属ブロックなし</text>')
            if d.get("rail") or d.get("load"):
                rl = getattr(self, "rails", {}).get(d.get("rail"), {})
                bits = [f'電源 {rl.get("label", d.get("rail", "未決"))}']
                if d.get("load"):
                    bits.append(f'ΔI≈{d["load"]}')
                if int(d.get("stagger", 1) or 1) > 1:
                    bits.append(f'段階解除×{d["stagger"]}')
                if d.get("power_switch"):
                    bits.append("電源スイッチ")
                o.append(f'<text x="{f(X_DOM + 12)}" y="{f(y0 + h - 8)}" font-size="10.5" font-weight="700" fill="#a16207">'
                         f'⚡ {esc("・".join(bits))}</text>')
        # RDC
        for k, r in enumerate(rdc):
            lx = xr + 26 + k * 40
            ya, yb = pos[r["a"]][0] + pos[r["a"]][1] / 2 + 10, pos[r["b"]][0] + pos[r["b"]][1] / 2 + 10
            colr = OK_C if r["ok"] else RDC_C
            ow.append(f'<path d="M{f(xr)},{f(ya)} L{f(lx)},{f(ya)} L{f(lx)},{f(yb)} L{f(xr)},{f(yb)}" fill="none" stroke="{colr}" '
                      f'stroke-width="1.6" stroke-dasharray="5 3"/>')
            tag = f'RDC×{len(r["via"])}'
            tw = wlen(tag) * 6.4 + 10
            my = (ya + yb) / 2
            o.append(f'<rect x="{f(lx - tw / 2)}" y="{f(my - 9)}" width="{f(tw)}" height="18" rx="2" fill="#fff" stroke="{colr}" stroke-width="1.4"/>'
                     f'<text x="{f(lx)}" y="{f(my + 4)}" font-size="10" font-weight="800" fill="{colr}" text-anchor="middle">{tag}</text>')
        # 解除シーケンス(負論理: L = リセット中、H = 解除)
        o.append(f'<text x="12" y="{f(WV_Y)}" font-size="14" font-weight="800" fill="{INK}">解除シーケンス</text>')
        LX, X0 = 24, X0w
        py = WV_Y - 4
        o.append(f'<rect x="{f(PX)}" y="{f(py)}" width="{PW}" height="{f(PH)}" rx="6" fill="#f8fafc" stroke="#cbd5e1"/>')
        yy = py + 20
        for kd, t in plines:
            if kd == "h":
                o.append(f'<text x="{f(PX + 14)}" y="{f(yy)}" font-size="12.5" font-weight="800" fill="{INK}">{esc(t)}</text>')
            elif kd == "s":
                yy += 4
                o.append(f'<text x="{f(PX + 14)}" y="{f(yy)}" font-size="11.5" font-weight="800" fill="{INK}">{esc(t)}</text>')
            elif kd == "c":
                o.append(f'<text x="{f(PX + 30)}" y="{f(yy)}" font-size="10.5" fill="{INK}">{esc(t)}</text>')
            elif kd in ("ok", "ng"):
                colr = OK_C if kd == "ok" else RDC_C
                o.append(f'<text x="{f(PX + 30)}" y="{f(yy)}" font-size="10.5" fill="{INK}">{esc(t)}</text>')
                o.append(f'<line x1="{f(PX + 14)}" y1="{f(yy - 4)}" x2="{f(PX + 24)}" y2="{f(yy - 4)}" stroke="{colr}" stroke-width="2.4"/>')
            else:
                o.append(f'<text x="{f(PX + 14)}" y="{f(yy)}" font-size="10.5" fill="#334155">{esc(t)}</text>')
            yy += 17
        tops = WV_Y + 26
        for j in range(nst + 2):
            xx = X0 + j * SW_
            o.append(f'<line x1="{f(xx)}" y1="{f(tops)}" x2="{f(xx)}" y2="{f(tops + len(wav_rows) * 30)}" stroke="#e2e8f0" stroke-dasharray="3 3"/>')
        for i, (kd, it) in enumerate(wav_rows):
            yy = tops + i * 30 + 6
            hi, lo = yy, yy + 16
            if kd == "src":
                name, t, col = it.get("label", it["id"]), 0.5, scol.get(it["id"], "#475569")   # 列と同じ色
            elif kd == "pll":
                name, t, col = f'{self.plls[it].get("label", it)} ロック', 1.5, "#475569"
            else:
                name, t, col = it.get("label", it["id"]) + " リセット", self.step.get(it["id"], 1) + .5, it["_col"]
                if kd == "dom" and it.get("stretch"):
                    o.append(f'<text x="{f(X0 + t * SW_ + 6)}" y="{f(hi - 2)}" font-size="9.5" fill="{GRAY}">+{it["stretch"]}cyc</text>')
            o.append(f'<text x="{LX}" y="{f(lo - 2)}" font-size="11" font-weight="700" fill="{INK}">{esc(name)}</text>')
            xt = X0 + t * SW_
            p = f"M{f(X0)},{f(lo)} L{f(xt)},{f(lo)} L{f(xt)},{f(hi)} L{f(X0 + (nst + 1) * SW_)},{f(hi)}"
            o.append(f'<path d="{p}" fill="none" stroke="{col}" stroke-width="2"/>')
            if kd == "dom":
                n = int(it.get("stagger", 1) or 1)
                for k in range(1, n):                  # 段階解除: 後続の小さな立ち上がり
                    xk = xt + k * .5 / n * SW_
                    o.append(f'<line x1="{f(xk)}" y1="{f(hi)}" x2="{f(xk)}" y2="{f(hi + 8)}" stroke="{col}" stroke-width="1.6"/>')
                if n > 1:
                    o.append(f'<text x="{f(xt + .5 * SW_ + 4)}" y="{f(lo)}" font-size="9.5" fill="{col}">段階×{n}</text>')
                if it.get("power_switch"):
                    o.append(f'<text x="{f(xt - 6)}" y="{f(lo - 2)}" font-size="9" fill="#a16207" text-anchor="end">電源ON→切離し解除→クロック→</text>')
        if rails:
            o.append(f'<text x="12" y="{f(CH_Y)}" font-size="14" font-weight="800" fill="{INK}">電源レールごとの電流の跳ね上がり(解除・クロック開始・突入)</text>')
            for i, r in enumerate(rails):
                rail = self.rails[r]
                base = CH_Y + 22 + i * RH + RH - 18
                mx = amp(rail.get("max_step"))
                tot = self.psum[r]
                top = max([mx or 0] + [sum(a for a, _, _ in v) for v in tot.values()]) * 1.18 or 1
                sc = (RH - 34) / top
                o.append(f'<text x="{LX}" y="{f(base - 34)}" font-size="11.5" font-weight="800" fill="{INK}">{esc(rail.get("label", r))}</text>'
                         f'<text x="{LX}" y="{f(base - 19)}" font-size="10" fill="{GRAY}">{esc(str(rail.get("volt", "")))}'
                         f'{" ・ " + esc(str(rail.get("droop"))) if rail.get("droop") else ""}</text>')
                o.append(f'<line x1="{f(X0)}" y1="{f(base)}" x2="{f(X0 + (nst + 1) * SW_)}" y2="{f(base)}" stroke="#94a3b8"/>')
                if mx:
                    ym = base - mx * sc
                    o.append(f'<line x1="{f(X0)}" y1="{f(ym)}" x2="{f(X0 + (nst + 1) * SW_)}" y2="{f(ym)}" stroke="{RDC_C}" '
                             f'stroke-dasharray="5 3" stroke-width="1.2"/><text x="{f(X0 + (nst + 1) * SW_ + 6)}" y="{f(ym + 4)}" '
                             f'font-size="10" fill="{RDC_C}">許容 {fa(mx)}</text>')
                lastx = -1e9
                for t, v in sorted(tot.items()):
                    x = X0 + t * SW_
                    yb = base
                    a_sum = sum(a for a, _, _ in v)
                    over = mx and a_sum > mx
                    for a, d, k in v:
                        hgt = a * sc
                        o.append(f'<rect x="{f(x - 5)}" y="{f(yb - hgt)}" width="10" height="{f(hgt)}" fill="{d["_col"]}" '
                                 f'fill-opacity="{".45" if k == "突入" else ".85"}" stroke="{RDC_C if over else "#fff"}" stroke-width="{1.4 if over else .6}"/>')
                        yb -= hgt
                    if over or x - lastx > 44:              # 段階解除で近い棒は数字を間引く(合計は表に出る)
                        o.append(f'<text x="{f(x)}" y="{f(yb - 3)}" font-size="9.5" font-weight="{800 if over else 400}" '
                                 f'fill="{RDC_C if over else INK}" text-anchor="middle">{fa(a_sum)}</text>')
                        lastx = x
                dpa = mv_per_a(rail.get("droop"))
                worst = max(sum(a for a, _, _ in v) for v in tot.values())
                if dpa:
                    o.append(f'<text x="{f(X0 + (nst + 1) * SW_ + 6)}" y="{f(base - 18)}" font-size="10" fill="{INK}">'
                             f'最大 ΔI {fa(worst)} → 電圧降下 ≈{worst * dpa * 1000:.0f}mV</text>')
                bod = rail.get("_bod")
                if bod:
                    x_, th, low, margin = bod
                    colb = RDC_C if margin < 0 else OK_C
                    o.append(f'<text x="{f(X0 + (nst + 1) * SW_ + 6)}" y="{f(base - 4)}" font-size="10" fill="{colb}">'
                             f'最悪 ≈{low * 1000:.0f}mV / {esc(x_.get("label", x_["id"]))} {th * 1000:.0f}mV(余裕 {margin * 1000:.0f}mV)</text>')
            o.append(f'<text x="{LX}" y="{f(CH_Y + 22 + len(rails) * RH + 14)}" font-size="10.5" fill="{GRAY}">棒 = その段で増える電流(ドメインの色、薄い = 電源スイッチの突入)。'
                     f'同じ段の合計が赤破線(許容)を超えると電圧が落ちる。stagger で段階解除、after で段をずらす</text>')
        o.append(f'<text x="12" y="{f(H - 14)}" font-size="11" fill="{GRAY}">OR = どれかのリセット源が入るとリセット / 非同期でリセットに入り、'
                 f'解除はドメインのクロックに同期 / 赤破線 = 片側だけ単独リセットされる乗り換え(RDC)・緑 = 切り離しあり / 破線の源 = チップ外 / '
                 f'波形は L = リセット中</text><text x="12" y="{f(H - 30)}" font-size="11" fill="{GRAY}">源の札: 非同期 = クロックと無関係に入る / '
                 f'同期 &lt;クロック&gt; = そのクロックの FF から出る(発行 = 出すブロック) / 同期化の箱: 非同期アサート・同期解除 か 同期リセット(アサートにもクロックが要る)</text></svg>')
        return "".join(o).replace("<!--LINES-->", "".join(ow), 1)

    def md(self):
        L = ["# リセット系統", "", "## リセット源 × ドメイン", ""]
        L.append("| ドメイン | " + " | ".join(s.get("label", s["id"]) for s in self.srcs) + " | 同期 | 方式 | 解除 |")
        L.append("|---|" + "---|" * len(self.srcs) + "---|---|---|")
        for d in self.doms:
            row = ["●" if s["id"] in d.get("sources", []) else "" for s in self.srcs]
            ck = self.ckd.get(d.get("clock_domain"), {})
            sy = f'{d.get("sync", 2)}段 @{ck.get("label", d.get("clock_domain", "未決"))}' if d.get("clock_domain") else "未決"
            L.append(f'| {d.get("label", d["id"])} | ' + " | ".join(row) + f' | {sy} | {"同期リセット" if d.get("style") == "sync" else "非同期アサート・同期解除"} | {self.rank.get(d["id"], 1)}番目 |')
        L += ["", "## リセット源", "", "| 源 | 種類 | 非同期/同期 | 発行クロック | 発行元 |", "|---|---|---|---|---|"]
        for x in self.srcs:
            typ, ck, frm = self.src_info(x)
            L.append(f'| {x.get("label", x["id"])} | {KIND.get(x.get("kind"), x.get("kind", ""))} | '
                     f'{"非同期" if typ == "async" else "同期"} | {ck[0] if ck else ("—" if typ == "async" else "未決")} | {frm or ""} |')
        if getattr(self, "rails", None):
            L += ["", "## 電源レールと解除時の電流", "", "| レール | 段 | ΔI 合計 | 内訳 | 許容 |", "|---|---|---|---|---|"]
            for r, tot in self.psum.items():
                rail = self.rails[r]
                for t, v in sorted(tot.items()):
                    a = sum(x for x, _, _ in v)
                    mx = amp(rail.get("max_step"))
                    d0 = v[0][1]
                    st = self.step.get(d0["id"], 1)
                    lab = f'{self.rank.get(d0["id"], 1)}番目' + ("の直前" if t < st + .5 - 1e-6 else ("" if abs(t - st - .5) < 1e-6 else "の後(段階)"))
                    L.append(f'| {rail.get("label", r)} | {lab} | {fa(a)} | '
                             + ", ".join(f'{self.dl(d["id"])}({k} {fa(x)})' for x, d, k in v)
                             + f' | {fa(mx) if mx else "未決"}{" **超過**" if mx and a > mx else ""} |')
        L += ["", "## 解除の順番", ""]
        for d in sorted(self.doms, key=lambda d: self.step.get(d["id"], 1)):
            pre = [self.dl(a) for a in d.get("after", [])] + [self.plls.get(p, {}).get("label", p) + " ロック" for p in d.get("wait", [])]
            L.append(f'{self.rank.get(d["id"], 1)}. {d.get("label", d["id"])}' + (f'(待つ: {", ".join(pre)})' if pre else "")
                     + (f' 伸長 {d["stretch"]}cyc' if d.get("stretch") else ""))
        head, _ = self.rdc_text()
        L += ["", "## リセットドメインの乗り換え(RDC)", "", f"**{head[0]}**", ""] + [x for x in head[1:]] + [""]
        if not self.rdc:
            L.append("なし")
        for r in self.rdc:
            al = "; ".join(f'{self.dl(a)} が {"・".join(self.sid.get(s, {}).get("label", s) for s in only)} で単独リセット'
                           for a, only in r["alone"])
            L.append(f'- {self.dl(r["a"])} ↔ {self.dl(r["b"])}: {al} — {"対策あり(切り離し)" if r["ok"] else "**要対策**"}'
                     f'({", ".join(r["via"])})')
        return "\n".join(L) + "\n"


def build(c, title="リセット系統図"):
    if not (c.s.get("resets", {}) or {}).get("domains"):
        return None, ["resets.domains がない(リセット系統図は描けない)"], None
    rt = ResetTree(c)
    rt.check()
    svg = rt.svg(title)
    return svg, rt, rt.md()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    c = Chip(load(a.spec))
    svg, rt, md = build(c)
    if svg is None:
        sys.exit(rt[0])
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "reset_tree.svg")
    open(p, "w", encoding="utf-8").write(svg)
    open(os.path.join(a.out, "reset.md"), "w", encoding="utf-8").write(md)
    if a.png:
        to_png(p)
    print(f"生成: {p} / reset.md")
    for w in rt.info:
        print("  " + w)
    print("lint:", "OK" if not rt.warn else "")
    for w in rt.warn:
        print("  " + w)


if __name__ == "__main__":
    main()
