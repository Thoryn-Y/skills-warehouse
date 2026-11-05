#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纪律探针：只用 ast + tokenize 读文件，不 import、不执行被测代码。

用途：检测「迭代式 agent 生成」残留的**纪律痕迹**（缺失与分布），
与主技能的文体/结构信号互补。文体层被多轮改写压平后，这一层仍然留痕。

用法：
    python trace_probe.py <目录或清单文件> [--baseline <目录>] [--exclude <目录名>]... [--json] [--top N]

    --baseline  在对照组（公认人工语料）上跑同一探针，输出基线命中率与倍数。
                没有对照组的纪律分没有解释力，不要跳过。
    --exclude   额外排除的目录名，可重复（如 --exclude 备份 --exclude 软著相关）。
                匹配的是目标根之下的路径分量，不会误伤路径里的祖先目录。

输出：逐文件指标 + 纪律分（0-5）+ 项目级命中率；给了 baseline 则并列对比。
"""
import argparse
import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

# ---- 默认排除（与 SKILL.md 第 1 步一致） -------------------------------------
EXCLUDE_DIRS = {
    "node_modules", "vendor", "dist", "build", "out", "target", "coverage",
    ".next", ".venv", "venv", "env", "__pycache__", ".git", ".mypy_cache",
    ".pytest_cache", "site-packages", "migrations", "testdata",
}

# ---- 判据阈值（集中在此，便于按语言/项目调整；改动需同步 SKILL.md） ----
TH = {
    "doc_cov_high": 0.70,      # B1 文档覆盖偏高
    "cmt_ratio_low": 0.20,     # B1 注释率上限（两通道脱钩）
    "funcs_min": 5,            #    函数数下限（少于此不判）
    "vis_floor": 0.60,         # B2 私有/公共各自的下限
    "vis_ratio": 0.85,         #   私有 ÷ 公共 的比值下限
    "sect_per_kloc": 3.0,      # S 分区注释密度
    "trail_share_max": 0.30,   # T 行尾注释占比上限
    "trail_min_comments": 20,  # T 的样本下限（注释太少不判）
    "scorable_sloc": 100,      # 计分文件的最小非空行数
    "hit_score": 2,            # 单文件“命中”的纪律分门槛（三条中命中两条）
    "axis_min_rate": 0.30,     # 项目级：单条判据的命中占比要达到这条线才算“该轴偏离”
    "axis_min_hits": 4,        # 项目级：命中文件数下限（防止小样本放大倍数）
}

DEFAULT_TARGET = ["*.py"]


def iter_files(target, extra_exclude=()):
    """返回 .py 绝对路径列表；target 可以是目录或每行一个路径的清单。"""
    skip = EXCLUDE_DIRS | set(extra_exclude)
    p = Path(target)
    if p.is_file() and p.suffix != ".py":
        return [Path(l.strip()) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if p.is_file():
        return [p]
    out = []
    root = p.resolve()
    for f in sorted(p.rglob("*.py")):
        try:
            rel = f.resolve().relative_to(root)
        except ValueError:
            rel = f
        # 只按「目标根之下的路径分量」排除，否则 --exclude 会把路径中的祖先目录也误伤
        if any(part in skip for part in rel.parts[:-1]):
            continue
        out.append(f)
    return out


def lex_comments(src):
    """返回 [(是否整行注释, 文本)]；解析失败时退化为正则近似。"""
    lines = src.splitlines()
    out = []
    try:
        for tk in tokenize.generate_tokens(io.StringIO(src).readline):
            if tk.type == tokenize.COMMENT:
                row, col = tk.start
                prefix = lines[row - 1][:col] if row <= len(lines) else ""
                out.append((prefix.strip() == "", tk.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 退化路径：按行首 # 粗判整行注释，行尾出现的 # 算行尾注释
        for l in lines:
            m = re.search(r"#(.*)$", l)
            if not m:
                continue
            full = bool(re.match(r"^\s*#", l))
            out.append((full, "#" + m.group(1)))
    return out


def is_commented_code(text):
    body = text.lstrip("#").strip()
    if len(body) < 3:
        return False
    if not re.match(r"^(def |class |return |import |from |if |for |while |print\(|self\.|[a-z_]+\s*=\s*[^=])", body):
        return False
    try:
        ast.parse(body)
        return True
    except SyntaxError:
        return False


def analyze(path):
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return None

    lines = src.splitlines()
    nonempty = sum(1 for l in lines if l.strip())
    comments = lex_comments(src)
    comment_texts = [c[1] for c in comments]

    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    documented = [f for f in funcs if ast.get_docstring(f) is not None]

    def cov(sub):
        return (sum(1 for f in sub if ast.get_docstring(f) is not None) / len(sub)) if sub else None

    private = [f for f in funcs if f.name.startswith("_") and not f.name.startswith("__")]
    public = [f for f in funcs if not f.name.startswith("_")]

    imported = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported += [(a.asname or a.name.split(".")[0]) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            imported += [(a.asname or a.name) for a in n.names if a.name != "*"]
    stripped = "\n".join(l for l in lines if not re.match(r"^\s*(import |from \S+ import )", l))
    unused = [n for n in imported if not re.search(r"\b" + re.escape(n) + r"\b", stripped)]

    r = {
        "path": str(path),
        "sloc": nonempty,
        "funcs": len(funcs),
        "cmt_ratio": round(len(comments) / nonempty, 3) if nonempty else 0.0,
        "comments": len(comments),
        "cmt_trailing": sum(1 for c in comments if not c[0]),
        "cmt_trail_share": round(sum(1 for c in comments if not c[0]) / len(comments), 3) if comments else None,
        "doc_cov": None if not funcs else round(cov(funcs), 3),
        "doc_cov_priv": None if not private else round(cov(private), 3),
        "doc_cov_pub": None if not public else round(cov(public), 3),
        "commented_code": sum(1 for t in comment_texts if is_commented_code(t)),
        "todo": len(re.findall(r"\b(TODO|FIXME|XXX|HACK)\b", src)),
        "unused_import": len(unused),
        "sections": sum(1 for t in comment_texts if re.search(r"[=\-]{4,}|[═─]{4,}", t)),
    }
    r["sect_per_kloc"] = round(r["sections"] * 1000 / nonempty, 2) if nonempty else 0.0
    r.update(score(r))
    return r


def score(r):
    """纪律分 0-3。三条**互相独立**的判据，不适用时返回 None 而不是 0。

    独立性是这一层成立的前提，踩过的坑：早先把"文档覆盖"与"可见性无落差"分成两条，
    实测两者高度共线（都是 docstring 全覆盖的两种说法），于是任何强制 docstring 风格的
    项目（如 ultralytics，116 个文件里 48 个同时命中）会被整片判成 AI。现在合并为一条 D。

    D 文档风格偏离：docstring 全覆盖 + 注释稀疏 + 私有/公共无落差
    S 注释组织偏离：分区注释高密度（人写行尾注释，模型写块前标题）
    T 注释位置偏离：行尾注释占比过低（人边写边标注，模型另起一行解释）
    """
    priv_doc = r["doc_cov_priv"] if r["doc_cov_priv"] is not None else 0.0
    pub_doc = r["doc_cov_pub"] if r["doc_cov_pub"] is not None else 0.0
    doc_ok = (r["doc_cov"] is not None and r["funcs"] >= TH["funcs_min"]
              and r["doc_cov"] >= TH["doc_cov_high"] and r["cmt_ratio"] <= TH["cmt_ratio_low"])
    vis_ok = (r["doc_cov_priv"] is not None and r["doc_cov_pub"] is not None
              and priv_doc >= TH["vis_floor"] and pub_doc >= TH["vis_floor"]
              and pub_doc > 0 and priv_doc / pub_doc >= TH["vis_ratio"])

    flags = {
        "D_doc_style": bool(doc_ok and vis_ok),
        "S_section_density": bool(r["sloc"] >= TH["scorable_sloc"]
                                  and r["sect_per_kloc"] >= TH["sect_per_kloc"]),
        "T_trailing_rare": None,
    }
    if r["comments"] >= TH["trail_min_comments"]:
        flags["T_trailing_rare"] = bool(r["cmt_trail_share"] is not None
                                        and r["cmt_trail_share"] <= TH["trail_share_max"])
    return {"flags": flags, "score": sum(1 for v in flags.values() if v),
            "decisive": bool(flags["D_doc_style"] and flags["S_section_density"])}


def aggregate(rows):
    scored = [r for r in rows if r["sloc"] >= TH["scorable_sloc"]]
    hits = [r for r in scored if r["decisive"]]
    n = len(scored)

    crit = {}
    for k in ("D_doc_style", "S_section_density", "T_trailing_rare"):
        applicable = [r for r in scored if r["flags"].get(k) is not None]
        hit = sum(1 for r in applicable if r["flags"][k])
        crit[k] = {"hit": hit, "applicable": len(applicable),
                   "rate": (hit / len(applicable)) if applicable else 0.0}
    passing = [k for k, v in crit.items() if v["rate"] >= TH["axis_min_rate"] and v["applicable"]]
    requires = ("D_doc_style", "S_section_density")
    axis_ok = (all(crit[k]["rate"] >= TH["axis_min_rate"] for k in requires)
               and len(hits) >= TH["axis_min_hits"])

    return {
        "files": len(rows),
        "scorable": n,
        "hits": len(hits),
        "rate": (len(hits) / n) if n else 0.0,
        "criteria": crit,
        "criteria_passing": passing,
        "axis_b_ok": axis_ok,
        "sloc": sum(r["sloc"] for r in rows),
        "debris": {
            "commented_code": sum(r["commented_code"] for r in rows),
            "todo": sum(r["todo"] for r in rows),
            "unused_import": sum(r["unused_import"] for r in rows),
        },
        "cmt_trail_share": (sum(r["cmt_trailing"] for r in rows) / sum(r["comments"] for r in rows))
        if sum(r["comments"] for r in rows) else None,
        "sect_per_kloc": (sum(r["sections"] for r in rows) * 1000 / sum(r["sloc"] for r in rows))
        if sum(r["sloc"] for r in rows) else 0.0,
    }


def show(rows, top, json_out, base_agg=None):
    a = aggregate(rows)
    if json_out:
        payload = {"aggregate": a, "files": rows}
        if base_agg is not None:
            payload["baseline"] = base_agg
            payload["multiplier"] = (a["rate"] / base_agg["rate"]) if base_agg["rate"] else None
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return a
    rows = sorted(rows, key=lambda r: (-r["score"], -r["sloc"]))
    print(f"文件 {a['files']} 个 / 计分文件 {a['scorable']} 个（非空行 ≥ {TH['scorable_sloc']}）"
          f" / {a['sloc']} 非空行")
    print(f"命中（D∧S，两条同时成立）的文件：{a['hits']}/{a['scorable']} = {a['rate']:.1%}")
    names = {"D_doc_style": "D 文档风格", "S_section_density": "S 注释组织", "T_trailing_rare": "T 注释位置"}
    seg = []
    for k, v in a["criteria"].items():
        seg.append(f"{names[k]} {v['hit']}/{v['applicable']}={v['rate']:.0%}")
    print("  单条命中率：" + " · ".join(seg))
    ok = "成立" if a["axis_b_ok"] else "不成立"
    print(f"  轴 B 判定：D≥{TH['axis_min_rate']:.0%} 且 S≥{TH['axis_min_rate']:.0%}"
          f" 且命中 ≥{TH['axis_min_hits']} 个文件 → 轴 B {ok}")
    print(f"辅助读数（不计分）：注释掉的代码 {a['debris']['commented_code']} 处 · TODO {a['debris']['todo']} 处"
          f" · 未用 import {a['debris']['unused_import']} 处")
    trail = f"{a['cmt_trail_share']:.0%}" if a["cmt_trail_share"] is not None else "—"
    print(f"分区注释密度：{a['sect_per_kloc']:.2f} / 千行 · 行尾注释占比：{trail}\n")
    print(f"{'分':>2}  {'非空行':>6} {'函数':>4} {'注率':>5} {'文档':>5} {'私/公':>7} {'分区/k':>7}  {'文件':<40} 命中项")
    for r in rows[:top]:
        ratio = (f"{r['doc_cov_priv']:.2f}/{r['doc_cov_pub']:.2f}"
                 if r["doc_cov_priv"] is not None and r["doc_cov_pub"] is not None else "—")
        flags = ",".join(k.split("_")[0] for k, v in r["flags"].items() if v) or "—"
        name = r["path"].replace("\\", "/")
        if len(name) > 40:
            name = "…" + name[-39:]
        print(f"{r['score']:>2}  {r['sloc']:>6} {r['funcs']:>4} {r['cmt_ratio']:>5.2f} "
              f"{(r['doc_cov'] if r['doc_cov'] is not None else 0):>5.2f} {ratio:>7} "
              f"{r['sect_per_kloc']:>7.2f}  {name:<40} {flags}")
    return a


def main():
    ap = argparse.ArgumentParser(description="纪律探针：静态统计 T4 纪律痕迹，须配人工基线解读")
    ap.add_argument("target", help="待测目录，或每行一个 .py 路径的清单文件")
    ap.add_argument("--baseline", help="人工对照组目录，用于校准命中率")
    ap.add_argument("--exclude", action="append", default=[],
                    help="额外排除的目录名，可重复。例：--exclude 备份 --exclude 软著相关")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给了 --baseline 时一并输出换算倍数）")
    ap.add_argument("--top", type=int, default=25, help="逐文件表显示前 N 行")
    args = ap.parse_args()

    rows = [r for r in (analyze(f) for f in iter_files(args.target, args.exclude)) if r]
    if not rows:
        print("没有可解析的 .py 文件", file=sys.stderr)
        return 1

    base_agg = None
    if args.baseline:
        base = [r for r in (analyze(f) for f in iter_files(args.baseline, args.exclude)) if r]
        if base:
            base_agg = aggregate(base)
        else:
            print(f"[!] 基线目录没有可解析的 .py（检查路径或 --exclude 是否排空）", file=sys.stderr)

    a = show(rows, args.top, args.json, base_agg)
    if args.json:
        return 0

    if base_agg:
        print(f"\n对照基线：{base_agg['hits']}/{base_agg['scorable']} = {base_agg['rate']:.1%}"
              f"（{base_agg['sloc']} 非空行，残留物 {base_agg['debris']['commented_code']} / "
              f"{base_agg['debris']['todo']} / {base_agg['debris']['unused_import']}）")
        mult = (a["rate"] / base_agg["rate"]) if base_agg["rate"] else float("inf")
        print(f"倍数 = {mult:.1f}x   （<3x 不可解释；≥10x 才可作强证据）")
    elif args.baseline:
        print("\n[!] 基线不可用，本次没有倍数——纪律分只能作中信号。")
    else:
        print("\n[!] 未提供 --baseline：纪律分缺少基线，只能作中信号，不得单独定罪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
