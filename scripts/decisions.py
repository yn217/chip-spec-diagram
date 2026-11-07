#!/usr/bin/env python3
"""decisions — 図を作るための決定事項ログ(質問キュー)

  init        --mode concept|rtl|mixed [--project 名前]   質問バンクから決定事項ファイルを作る
  next        [-n 4]          次に聞く質問を AskUserQuestion 形式(JSON)で出す
  set ID 値   [--source user|rtl|assumed] [--note ..]     回答を記録
  assume ID   [値]            「任せる」→ 仮決め(図では「仮」表示)
  skip ID / reopen ID
  add         --id --q --category [--options a,b] [--depends x] [--priority n] [--confirm]
  import-rtl  inference.yaml  RTL推定を「決定(rtl)」と「要確認」に振り分けて取り込む
  status      [--md]          進捗。--md で仕様書に貼れる決定事項表
ファイル: -f diagram_decisions.yaml (既定)
"""
import sys, os, json, argparse, datetime, yaml

HERE = os.path.dirname(os.path.abspath(__file__))
BANK = os.path.join(HERE, "..", "templates", "question_bank.yaml")
NEG = ("なし", "不要", "未定", "none", "no", "")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def load(path):
    if not os.path.exists(path):
        sys.exit(f"{path} がない: まず init を実行")
    return yaml.safe_load(open(path, encoding="utf-8"))


def save(d, path):
    yaml.safe_dump(d, open(path, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False, width=110)


def find(d, iid):
    for it in d["items"]:
        if it["id"] == iid:
            return it
    sys.exit(f"ID {iid} がない")


def dep_ok(d, it):
    for dep in it.get("depends_on", []):
        neg = "!=" in dep
        key, _, want = dep.partition("!=") if neg else dep.partition("=")
        src = next((x for x in d["items"] if x["id"] == key), None)
        if not src or src["status"] not in ("decided", "assumed"):
            return False
        val = str(src.get("value", ""))
        if neg:                      # key!=値: その値の時は聞かない
            if want in val:
                return False
        elif want:
            if want not in val:
                return False
        elif val.strip().lower() in NEG or val.startswith("未定"):
            return False
    return True


def cmd_init(a):
    bank = yaml.safe_load(open(BANK, encoding="utf-8"))["items"]
    if os.path.exists(a.file) and not a.force:
        sys.exit(f"{a.file} は既にある(--force で作り直し)")
    items = []
    for q in bank:
        if a.mode not in q.get("modes", []):
            continue
        it = {k: v for k, v in q.items() if k != "modes"}
        it.update(status="open", value=None, source=None, confirm=False, note=None, updated=None)
        items.append(it)
    d = {"project": a.project, "mode": a.mode, "created": now(), "items": items}
    save(d, a.file)
    print(f"{a.file}: {len(items)} 項目 (mode={a.mode})")


def cmd_next(a):
    d = load(a.file)
    cand = [it for it in d["items"] if it["status"] == "open" and dep_ok(d, it)]
    cand.sort(key=lambda it: (0 if it.get("confirm") else 1, it.get("priority", 50)))
    out = []
    for it in cand[:a.n]:
        opts = [{"label": o["label"], "description": o.get("description", "")} for o in it.get("options", [])][:4]
        if len(opts) == 1:
            opts.append({"label": "任せる(仮決め)", "description": f"仮に「{it.get('assume', '?')}」で進める"})
        out.append({"id": it["id"], "header": it["category"][:12], "question": it["q"],
                    "options": opts, "multiSelect": bool(it.get("multi")), "free_text": bool(it.get("free")) or not opts,
                    "why": it.get("effect"), "assume": it.get("assume"), "confirm": bool(it.get("confirm"))})
    rest = sum(1 for it in d["items"] if it["status"] == "open") - len(out)
    print(json.dumps({"questions": out, "remaining_open": rest}, ensure_ascii=False, indent=1))


def cmd_set(a):
    d = load(a.file)
    it = find(d, a.id)
    it["value"] = a.value
    it["source"] = a.source
    it["status"] = "assumed" if a.source == "assumed" else "decided"
    it["confirm"] = False
    if a.note:
        it["note"] = a.note
    it["updated"] = now()
    save(d, a.file)
    print(f"{a.id} = {a.value} ({it['status']}/{a.source})")


def cmd_assume(a):
    d = load(a.file)
    it = find(d, a.id)
    it["value"] = a.value or it.get("assume") or "TBD"
    it.update(status="assumed", source="assumed", updated=now())
    save(d, a.file)
    print(f"{a.id} = {it['value']} (仮決め)")


def cmd_skip(a, status):
    d = load(a.file)
    it = find(d, a.id)
    it["status"] = status
    if status == "open":
        it["value"] = None
    it["updated"] = now()
    save(d, a.file)
    print(f"{a.id}: {status}")


def cmd_add(a):
    d = load(a.file)
    if any(x["id"] == a.id for x in d["items"]):
        sys.exit(f"ID {a.id} は既にある")
    it = {"id": a.id, "category": a.category, "q": a.q, "priority": a.priority,
          "options": [{"label": o} for o in a.options.split(",")] if a.options else [],
          "free": not a.options, "depends_on": [x for x in (a.depends or "").split(",") if x],
          "status": "open", "value": None, "source": None, "confirm": a.confirm, "note": a.note, "updated": now()}
    if a.effect:
        it["effect"] = a.effect
    d["items"].append(it)
    save(d, a.file)
    print(f"追加: {a.id}")


KIND_Q = {
    "pad_side": ("IOの辺", "IOの辺をRTLのポート名から推定しました。この配置でよいですか？"),
    "external": ("チップ外", "チップ外の接続先をポート名から推定しました。この接続先でよいですか？"),
    "bus": ("バス", "次のインスタンスをバスとして描きます。よいですか？"),
    "memory": ("メモリ", "次のインスタンスをメモリとして描きます。よいですか？"),
    "interface": ("IF推定", "信号のまとまり(IF)と方向をこう推定しました。よいですか？"),
    "blackbox": ("定義なし", "定義が見つからないモジュールがあります(方向・M/Sが不明)。RTLを追加しますか、仮のまま描きますか？"),
    "conflict": ("M/S矛盾", "Master/Slaveの推定に矛盾があります。正しい方向を教えてください"),
    "unconnected": ("未接続", "接続が見つからないブロックがあります。図に残しますか？"),
    "clock": ("クロック", "SDCとクロック接続から各ブロックの周波数をこう推定しました。よいですか？"),
    "clock_tree": ("クロック系統", "クロック系統はSDCのクロック定義だけから作りました。PLL・分周・ゲートの構成を教えてください"),
    "warning": ("注意", "RTL解析で注意点があります"),
}


def cmd_import(a):
    d = load(a.file)
    rep = yaml.safe_load(open(a.inference, encoding="utf-8"))
    if rep.get("top"):
        it = next((x for x in d["items"] if x["id"] == "top_module"), None)
        if it and it["status"] == "open":
            it.update(value=rep["top"], source="rtl", status="decided", confirm=True, updated=now(),
                      note="rtl_scan のトップ候補")
            it["status"] = "open"   # トップは必ず確認
            it["q"] = f"トップモジュールは {rep['top']} でよいですか？"
            it["options"] = [{"label": rep["top"], "description": "RTL解析で最上位と判定"},
                             {"label": "別のモジュール", "description": "名前を指定"}]
    groups = {}
    decided = 0
    for x in rep["items"]:
        if x["kind"] in ("link", "exclude", "hierarchy"):
            continue
        if x["confidence"] == "high" and x["kind"] not in ("warning", "blackbox", "conflict"):
            decided += 1
            continue
        groups.setdefault(x["kind"], []).append(x)
    for kind, xs in groups.items():
        hdr, q = KIND_Q.get(kind, (kind, f"{kind} の推定を確認してください"))
        iid = f"rtl_{kind}"
        lines = [f"{x['target']} = {x['value']}  [{x['confidence']}] {x['evidence']}" for x in xs]
        it = next((y for y in d["items"] if y["id"] == iid), None)
        new = {"id": iid, "category": hdr, "q": q + "\n" + "\n".join("・" + l for l in lines[:12]),
               "options": [{"label": "推定どおりでよい", "description": f"{len(xs)}件をそのまま採用"},
                           {"label": "一部修正する", "description": "修正点を自由記述で"}],
               "free": True, "priority": 19, "status": "open", "value": None, "source": "rtl",
               "confirm": True, "evidence": lines, "note": None, "updated": now(),
               "effect": "RTLから推定した図の要素"}
        if kind == "pad_side":
            new["options"].insert(1, {"label": "フロアプランに合わせる", "description": "辺を個別に指定"})
            for y in d["items"]:
                if y["id"] == "io_sides" and y["status"] == "open":
                    y["status"] = "skipped"; y["note"] = "rtl_pad_side で確認"
        if it:
            it.update(new)
        else:
            d["items"].append(new)
    save(d, a.file)
    print(f"確度highの推定 {decided} 件は確認不要として採用 / 要確認 {sum(len(v) for v in groups.values())} 件を {len(groups)} 個の質問にまとめた")


def cmd_status(a):
    d = load(a.file)
    items = d["items"]
    cnt = {s: sum(1 for it in items if it["status"] == s) for s in ("decided", "assumed", "open", "skipped")}
    total = len(items) - cnt["skipped"]
    pct = 100 * (cnt["decided"] + cnt["assumed"]) // max(total, 1)
    if a.md:
        print(f"## 図の決定事項（{d.get('project') or ''} / 更新 {now()}）\n")
        print(f"決定 {cnt['decided']} / 仮決め {cnt['assumed']} / 未決 {cnt['open']}\n")
        print("| 区分 | 項目 | 内容 | 状態 | 根拠 |\n|---|---|---|---|---|")
        lab = {"decided": "決定", "assumed": "仮決め", "open": "未決", "skipped": "対象外"}
        for it in sorted(items, key=lambda x: ({"decided": 0, "assumed": 1, "open": 2, "skipped": 3}[x["status"]], x.get("priority", 50))):
            if it["status"] == "skipped":
                continue
            q = it["q"].split("\n")[0]
            v = str(it.get("value") or "—").replace("\n", " ").replace("|", "/")
            src = {"user": "回答", "rtl": "RTL", "assumed": "仮"}.get(it.get("source"), "")
            print(f"| {it['category']} | {q} | {v} | {lab[it['status']]} | {src}{(' ' + it['note']) if it.get('note') else ''} |")
        return
    print(f"[{d.get('mode')}] 進捗 {pct}%  決定 {cnt['decided']} / 仮決め {cnt['assumed']} / 未決 {cnt['open']} / 対象外 {cnt['skipped']}")
    for it in sorted(items, key=lambda x: x.get("priority", 50)):
        if it["status"] == "skipped":
            continue
        mark = {"decided": "✔", "assumed": "△", "open": "・"}[it["status"]]
        ready = "" if it["status"] != "open" else (" ←質問可" if dep_ok(d, it) else " (前提未決)")
        v = str(it.get("value") or "").split("\n")[0][:40]
        print(f" {mark} {it['id']:16s} {it['category']:8s} {v}{ready}")


def main():
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default="diagram_decisions.yaml")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init"); p.add_argument("--mode", required=True, choices=["concept", "rtl", "mixed"])
    p.add_argument("--project", default=""); p.add_argument("--force", action="store_true")
    p = sp.add_parser("next"); p.add_argument("-n", type=int, default=4)
    p = sp.add_parser("set"); p.add_argument("id"); p.add_argument("value")
    p.add_argument("--source", default="user", choices=["user", "rtl", "assumed"]); p.add_argument("--note")
    p = sp.add_parser("assume"); p.add_argument("id"); p.add_argument("value", nargs="?")
    p = sp.add_parser("skip"); p.add_argument("id")
    p = sp.add_parser("reopen"); p.add_argument("id")
    p = sp.add_parser("add"); p.add_argument("--id", required=True); p.add_argument("--q", required=True)
    p.add_argument("--category", required=True); p.add_argument("--options"); p.add_argument("--depends")
    p.add_argument("--priority", type=int, default=50); p.add_argument("--confirm", action="store_true")
    p.add_argument("--effect"); p.add_argument("--note")
    p = sp.add_parser("import-rtl"); p.add_argument("inference")
    p = sp.add_parser("status"); p.add_argument("--md", action="store_true")
    a = ap.parse_args()
    {"init": cmd_init, "next": cmd_next, "set": cmd_set, "assume": cmd_assume, "add": cmd_add,
     "import-rtl": cmd_import, "status": cmd_status,
     "skip": lambda a: cmd_skip(a, "skipped"), "reopen": lambda a: cmd_skip(a, "open")}[a.cmd](a)


if __name__ == "__main__":
    main()
