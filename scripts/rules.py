#!/usr/bin/env python3
"""rules — 作図ルール台帳の操作(指摘 → ルール化 → 検査)

  list                                   ルール一覧(守らせ方つき)
  checklist [--type architecture]        図を出す前の目視チェックリスト(review を含むルール)
  add --title .. --rule .. --enforce review[,lint,...] --applies architecture[,..] --origin "指摘の要約" [--why ..]
  coverage                               自動(render/lint)で守れていないルール = 目視頼み の一覧
台帳: $SK/rules/style_rules.yaml(-f で変更可)。チームで共有するなら git 管理する。
"""
import os, sys, argparse, yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEF = os.path.join(HERE, "..", "rules", "style_rules.yaml")
EN = {"render": "描画で自動", "lint": "lintで検査", "review": "目視", "question": "質問"}


def load(path):
    return yaml.safe_load(open(path, encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default=DEF)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    p = sp.add_parser("checklist"); p.add_argument("--type", default=None)
    p = sp.add_parser("add")
    for k in ("title", "rule", "origin"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--why"); p.add_argument("--enforce", default="review"); p.add_argument("--applies", default="all")
    p.add_argument("--lint")
    sp.add_parser("coverage")
    a = ap.parse_args()
    d = load(a.file)
    rules = d["rules"]
    if a.cmd == "list":
        for r in rules:
            print(f"{r['id']} {r['title']}  [{', '.join(EN.get(e, e) for e in r['enforce'])}]")
            print(f"    {r['rule']}")
    elif a.cmd == "checklist":
        print(f"# 出す前のチェック({a.type or '全種類'})  ※ lint の警告([重なり][perf][cdc] 等)が 0 件であることが前提")
        for r in rules:
            ap_ = r.get("applies", ["all"])
            if a.type and a.type not in ap_ and "all" not in ap_:
                continue
            mark = "□" if "review" in r["enforce"] else "✓(自動)"
            print(f"{mark} {r['id']} {r['title']}: {r['rule']}")
    elif a.cmd == "coverage":
        man = [r for r in rules if not ({"render", "lint"} & set(r["enforce"]))]
        print(f"ルール {len(rules)} 件 / 自動で守れるもの {len(rules) - len(man)} 件")
        for r in man:
            print(f"  目視のみ: {r['id']} {r['title']}  → lint 化/描画化を検討")
    elif a.cmd == "add":
        nid = f"R{max(int(r['id'][1:]) for r in rules) + 1:02d}"
        r = {"id": nid, "title": a.title, "rule": a.rule}
        if a.why:
            r["why"] = a.why
        r["enforce"] = [x.strip() for x in a.enforce.split(",")]
        if a.lint:
            r["lint"] = a.lint
        r["applies"] = [x.strip() for x in a.applies.split(",")]
        r["origin"] = a.origin
        rules.append(r)
        text = open(a.file, encoding="utf-8").read()
        head = "".join(l for l in text.splitlines(True) if l.startswith("#"))
        with open(a.file, "w", encoding="utf-8") as fo:
            fo.write(head)
            yaml.safe_dump(d, fo, allow_unicode=True, sort_keys=False, width=110)
        print(f"追加: {nid} {a.title}")


if __name__ == "__main__":
    main()
