#!/usr/bin/env python3
"""viewer — レビュー用のHTMLビューア(1ファイル)を作る。

  python3 viewer.py spec.yaml -o viewer.html [--decisions diagram_decisions.yaml] [--floorplan fp.yaml]

  - 構造図に ステップ/グループ/階層 を切り替えて重ねる(座標は固定)
  - ストーリーボード(ステップを時系列に並べる)
  - 詳細図(階層ブロックごと)
  - 決定事項表(決定/仮決め/未決) と フロアプラン(あれば)
"""
import os, sys, html, argparse, subprocess, yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chipdiag import Chip, esc, load

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:'Noto Sans JP','Hiragino Kaku Gothic ProN','Yu Gothic',Meiryo,sans-serif;color:#1e293b;line-height:1.7}
main{max-width:1180px;margin:0 auto;padding:24px 16px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px;padding-top:10px;border-top:1px solid #e2e8f0}
.fig{width:100%;height:auto;display:block}
#main{overflow:auto;max-height:85vh}
.nat{max-width:100%;height:auto;display:block}.frame{border:1px solid #e2e8f0;border-radius:10px;padding:8px}
.ctl{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0}.ctl .lab{font-size:13px;color:#64748b;min-width:5em}
.ctl button{font:inherit;font-size:13px;border:1px solid #cbd5e1;background:#fff;border-radius:999px;padding:3px 12px;cursor:pointer}
.ctl button.on{background:#1e293b;color:#fff;border-color:#1e293b}.ctl label{font-size:13px;display:flex;gap:4px;align-items:center}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{border-bottom:1px solid #e2e8f0;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f8fafc}tr.cur{background:#fef9c3}.tw{overflow-x:auto}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:10px}.sb{margin:0;border:1px solid #e2e8f0;border-radius:8px;padding:4px}
.st-decided{color:#15803d;font-weight:700}.st-assumed{color:#d97706;font-weight:700}.st-open{color:#b91c1c;font-weight:700}
.warn{font-family:monospace;font-size:12.5px;white-space:pre-wrap;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
"""
JS = """
const svg=document.querySelector('#main svg');const st={s:0,g:'',h:1,cum:true,p:1};const NC=JSON.parse(document.getElementById('nc')?.textContent||'[]');
function apply(){
 svg.querySelectorAll('.ov.cur').forEach(e=>e.style.display=(+e.dataset.step===st.s&&+e.dataset.h===st.h)?'':'none');
 svg.querySelectorAll('.ov.past').forEach(e=>e.style.display=(st.cum&&!NC.includes(st.s)&&!NC.includes(+e.dataset.step)&&+e.dataset.step<st.s&&+e.dataset.h===st.h)?'':'none');
 svg.querySelectorAll('.grp').forEach(e=>e.style.display=(e.dataset.grp===st.g)?'':'none');
 svg.querySelectorAll('.perf').forEach(e=>e.style.display=st.p?'':'none');
 document.querySelectorAll('button[data-p]').forEach(b=>b.classList.toggle('on',+b.dataset.p===st.p));
 svg.querySelectorAll('.h-open').forEach(e=>e.style.display=st.h?'':'none');
 svg.querySelectorAll('.h-closed').forEach(e=>e.style.display=st.h?'none':'');
 document.querySelectorAll('#steps tr[data-step]').forEach(r=>r.classList.toggle('cur',+r.dataset.step===st.s));
 document.querySelectorAll('button[data-s]').forEach(b=>b.classList.toggle('on',+b.dataset.s===st.s));
 document.querySelectorAll('button[data-g]').forEach(b=>b.classList.toggle('on',b.dataset.g===st.g));
 document.querySelectorAll('button[data-h]').forEach(b=>b.classList.toggle('on',+b.dataset.h===st.h));}
document.querySelectorAll('button[data-s]').forEach(b=>b.onclick=()=>{st.s=+b.dataset.s;apply()});
document.querySelectorAll('button[data-g]').forEach(b=>b.onclick=()=>{st.g=b.dataset.g;apply()});
document.querySelectorAll('button[data-h]').forEach(b=>b.onclick=()=>{st.h=+b.dataset.h;apply()});
document.querySelectorAll('button[data-p]').forEach(b=>b.onclick=()=>{st.p=+b.dataset.p;apply()});
const vbw=+(svg.getAttribute('viewBox')||'0 0 1000 1').split(' ')[2];
document.querySelectorAll('button[data-z]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('button[data-z]').forEach(x=>x.classList.toggle('on',x===b));
 svg.style.width=b.dataset.z==='fit'?'100%':(vbw*+b.dataset.z)+'px';});
const cum=document.getElementById('cum');if(cum)cum.onchange=e=>{st.cum=e.target.checked;apply()};
document.querySelectorAll('#steps tr[data-step]').forEach(r=>r.onclick=()=>{st.s=+r.dataset.step;apply()});
apply();
"""


def md_html(md):
    """latency 表など簡単な markdown(### 見出し・表・段落・**太字**)を HTML に"""
    import re as _re
    out, tbl = [], []
    def flush():
        if tbl:
            rows = [r for r in tbl if not _re.match(r"^\|[-|]+\|$", r)]
            h = "".join(f"<th>{esc(x.strip())}</th>" for x in rows[0].strip("|").split("|"))
            b = "".join("<tr>" + "".join(f"<td>{esc(x.strip())}</td>" for x in r.strip("|").split("|")) + "</tr>" for r in rows[1:])
            out.append(f'<div class="tw"><table><tr>{h}</tr>{b}</table></div>')
            tbl.clear()
    for ln in md.splitlines():
        if ln.startswith("|"):
            tbl.append(ln)
            continue
        flush()
        if ln.startswith("### "):
            out.append(f"<h3>{esc(ln[4:])}</h3>")
        elif ln.strip():
            out.append("<p>" + _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(ln)) + "</p>")
    flush()
    return "".join(out)


def decisions_table(path):
    d = yaml.safe_load(open(path, encoding="utf-8"))
    lab = {"decided": "決定", "assumed": "仮決め", "open": "未決"}
    rows = []
    for it in sorted(d["items"], key=lambda x: ({"decided": 0, "assumed": 1, "open": 2}.get(x["status"], 3), x.get("priority", 50))):
        if it["status"] not in lab:
            continue
        v = str(it.get("value") or "—")
        rows.append(f'<tr><td>{esc(it["category"])}</td><td>{esc(it["q"].splitlines()[0])}</td><td>{esc(v)}</td>'
                    f'<td class="st-{it["status"]}">{lab[it["status"]]}</td></tr>')
    return ('<div class="tw"><table><tr><th>区分</th><th>項目</th><th>内容</th><th>状態</th></tr>'
            + "".join(rows) + "</table></div>")


def build(spec_path, out, decisions=None, floorplan=None, board=None):
    spec = load(spec_path)
    c = Chip(spec)
    title = spec.get("chip", {}).get("label", "diagram")
    steps = c.steps
    has_kids = any(b.get("children") for b in spec.get("blocks", []))
    main = c.svg(interactive=True, css_class="fig")
    sbtn = '<button data-s="0">構造のみ</button>' + "".join(f'<button data-s="{i}">{i}. {esc(s["title"])}</button>' for i, s in enumerate(steps, 1))
    gbtn = '<button data-g="">なし</button>' + "".join(f'<button data-g="{g["id"]}">{esc(g["label"])}</button>' for g in spec.get("groups", []))
    ctl = ""
    if steps:
        ctl += f'<div class="ctl"><span class="lab">時系列</span>{sbtn}<label><input type="checkbox" id="cum" checked>過去ステップを残す</label></div>'
    if spec.get("groups"):
        ctl += f'<div class="ctl"><span class="lab">グループ</span>{gbtn}</div>'
    ctl += ('<div class="ctl"><span class="lab">表示倍率</span><button data-z="fit">全体</button>'
            '<button data-z="1">等倍</button><button data-z="1.5">1.5倍</button></div>')
    ctl += '<div class="ctl"><span class="lab">注記</span><button data-p="1">クロック/帯域</button><button data-p="0">非表示</button></div>'
    if has_kids:
        ctl += '<div class="ctl"><span class="lab">階層</span><button data-h="1">中身を表示</button><button data-h="0">折りたたむ</button></div>'
    rows = "".join(f'<tr data-step="{i}"><td><b>{i}</b></td><td><b>{esc(s["title"])}</b></td><td>{esc(s.get("master", ""))}</td>'
                   f'<td>{esc(s.get("note", ""))}</td></tr>' for i, s in enumerate(steps, 1))
    meta = spec.get("meta", {})
    note = f'<p style="font-size:13.5px;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px 10px">{esc(meta["note"])}</p>' if meta.get("note") else ""
    srcs = "".join(f'<li><a href="{esc(x["url"])}">{esc(x["title"])}</a></li>' for x in meta.get("sources", []))
    srcs = f'<details style="font-size:13px"><summary>出典</summary><ul>{srcs}</ul></details>' if srcs else ""
    parts = [f'<h1>{esc(title)}</h1><p style="color:#64748b">生成元: {esc(os.path.basename(spec_path))}</p>{note}{srcs}',
             f'<div class="frame">{ctl}<div id="main">{main}</div>']
    if steps:
        parts.append(f'<div class="tw"><table id="steps"><tr><th>#</th><th>動作</th><th>マスタ</th><th>説明</th></tr>{rows}</table></div>')
    nc = [i for i, s_ in enumerate(steps, 1) if not s_.get("cumulative", True)]
    parts.append(f'<script type="application/json" id="nc">{nc}</script>')
    parts.append("</div>")
    if c.lat_results:
        from latency import table_md as lat_md
        parts.append("<h2>レイテンシ目安</h2>" + md_html(lat_md(c.lat_results)))
    if steps:
        parts.append("<h2>ストーリーボード</h2><div class='grid2'>" + "".join(
            f'<figure class="sb">{c.svg(title=f"{i}. {s["title"]}", cur=i, cumulative=s.get("cumulative", True), css_class="fig")}</figure>'
            for i, s in enumerate(steps, 1)) + "</div>")
    import re as _re

    def natural(svg_):   # 自然な大きさ(viewBox の幅)を上限に表示
        m = _re.search(r'viewBox="[-\d.]+ [-\d.]+ ([\d.]+) ', svg_)
        w_ = float(m.group(1)) if m else 1000
        return svg_.replace('<svg ', f'<svg class="nat" width="{w_:.0f}" ', 1)
    for name, title_, svg_ in c.detail_pages():   # 自動配置: 階層ごとの独立ページ
        parts.append(f"<h2>詳細: {esc(title_)}</h2><div class='frame' style='overflow:auto'>{natural(svg_)}</div>")
    for b in ([] if c.auto else spec.get("blocks", [])):
        if b.get("children"):
            v = c.detail_view(b["id"])
            figs = [c.svg(view=v, title=f'{b["label"]} 内部', detail=True, css_class="fig")]
            for i, s in enumerate(steps, 1):
                if any(c.depth_of(a) > 0 and c.root_of(a) == b["id"] for fl in s["flows"] for a in fl["path"]):
                    figs.append(c.svg(view=v, title=f'{b["label"]} × {i}. {s["title"]}', detail=True, cur=i, cumulative=False, css_class="fig"))
            parts.append(f"<h2>詳細図: {esc(b['label'])}</h2><div class='grid2'>" + "".join(f'<figure class="sb">{x}</figure>' for x in figs) + "</div>")
    try:
        from memmap import pages as mm_pages, check as mm_check
        mps, rs = mm_pages(c)
        if rs:
            parts.append("<h2>メモリマップ</h2><div class='grid2'>" + "".join(
                f"<figure class='sb'>{natural(s_)}</figure>" for _, _, s_ in mps) + "</div>")
    except Exception:
        pass
    if spec.get("clocks", {}).get("domains"):
        import clocktree
        res = clocktree.build(c)
        if res[0]:
            svg_ck, wk, table = res
            tr = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(', '.join(v))}</td></tr>" for a, b, v in table)
            parts.append(f"<h2>クロック系統図</h2><div class='frame'>{svg_ck}</div>"
                         f"<div class='tw'><table><tr><th>ドメイン</th><th>ドメイン</th><th>非同期の乗り換え(CDC)</th></tr>{tr}</table></div>")
    if floorplan:
        from floorplan import FP
        fspec = yaml.safe_load(open(floorplan, encoding="utf-8"))
        fp = FP(fspec, os.path.dirname(os.path.abspath(floorplan)))
        parts.append(f"<h2>フロアプラン</h2><div class='frame'>{fp.svg(fspec['floorplan'].get('title'))}</div>")
        if fp.warn or fp.ask:
            parts.append(f"<div class='warn'>{esc(chr(10).join(['警告: ' + w for w in fp.warn] + ['要確認: ' + a for a in fp.ask]))}</div>")
    if board:
        from board import Board, check
        bspec = yaml.safe_load(open(board, encoding="utf-8"))
        bd = Board(bspec)
        fpd = yaml.safe_load(open(floorplan, encoding="utf-8")) if floorplan else None
        rows, bad = check(bd, spec, fpd)
        tr = "".join(f'<tr><td>{esc(r["pad"])}</td><td>{esc(r["to"])}</td><td><b>{r["board"]}</b></td><td>{r["diagram"] or "—"}</td>'
                     f'<td>{r["floorplan"] or "—"}</td><td class="{"st-decided" if r["ok"] else "st-open"}">{"一致" if r["ok"] else "食い違い"}'
                     f'{"（仮置き由来）" if r["assumed"] else ""}</td></tr>' for r in rows)
        parts.append(f"<h2>ボード配置とIOの辺</h2><div class='frame'>{bd.svg(bspec['board'].get('title'))}</div>"
                     f"<div class='tw'><table><tr><th>IF</th><th>相手部品</th><th>ボード推奨</th><th>論理図</th><th>フロアプラン</th><th>判定</th></tr>{tr}</table></div>")
        if bd.ask:
            parts.append(f"<div class='warn'>{esc(chr(10).join('仮置き: ' + a for a in bd.ask))}</div>")
    if decisions:
        parts.append("<h2>決定事項</h2>" + decisions_table(decisions))
    if c.warn:
        parts.append(f"<h2>lint</h2><div class='warn'>{esc(chr(10).join(c.warn))}</div>")
    page = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} 図ビューア</title><style>{CSS}</style></head><body><main>{"".join(parts)}</main><script>{JS}</script></body></html>')
    open(out, "w", encoding="utf-8").write(page)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", default="viewer.html")
    ap.add_argument("--decisions")
    ap.add_argument("--floorplan")
    ap.add_argument("--board")
    a = ap.parse_args()
    print("ビューア:", build(a.spec, a.out, a.decisions, a.floorplan, a.board))
