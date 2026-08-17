#!/usr/bin/env python3
"""BabelDOC 论文翻译封装脚本（零外部 API 版）。

翻译由 Agent 在对话中完成，脚本只负责「解析版面 → 收集源文本」和
「回填译文 → 渲染中文 PDF」两端，**不调用任何外部 API、不需要 API key**。

三阶段用法
-----------------------------------------------------------------
阶段 1（收集）::

    python translate.py <输入> --output-dir DIR --collect-only

产出 ``work/source_texts.jsonl``（全部待译段落）、``work/pending.jsonl``
（去掉白名单后真正需要翻译的部分）、``work/meta.json``。

阶段 2（翻译，由 Agent 完成）::

    读 pending.jsonl，按《翻译守则》逐条直译，分批追加写入
    work/translations.jsonl，每行 {"input": <原文>, "output": <译文>}。
    标题用特殊键 "__TITLE__"。
    随时可用 --status 查看覆盖率与剩余项。

阶段 3（渲染）::

    python translate.py <输入> --output-dir DIR

产出 ``<中文标题>.pdf``。

输入支持 arxiv URL / arxiv ID / 本地 PDF 路径。
运行环境：需带 babeldoc + torch + pymupdf 的 Python 解释器；若机主未指定解释器路径，运行前需向机主询问使用哪个环境。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout_rules import is_no_translate_layout  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TITLE_KEY = "__TITLE__"
COVERAGE_THRESHOLD = 0.95


# ---------- 输入处理 ----------

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def resolve_input(src: str, workdir: Path) -> Path:
    """把输入 (URL / arxiv ID / 本地路径) 解析成本地 PDF 路径。

    远程文件缓存在 workdir/source.pdf，两遍流程之间复用，不重复下载。
    """
    src = src.strip()

    p = Path(src)
    if p.suffix.lower() == ".pdf" and p.exists():
        return p.resolve()

    cached = workdir / "source.pdf"
    if cached.exists() and cached.stat().st_size > 0:
        print(f"[INPUT] 复用已缓存的 PDF: {cached}", flush=True)
        return cached

    if src.startswith(("http://", "https://")):
        m = ARXIV_ID_RE.search(src)
        url = f"https://arxiv.org/pdf/{m.group(1)}.pdf" if ("arxiv.org" in src and m) else src
        _download(url, cached)
        return cached

    if ARXIV_ID_RE.fullmatch(src):
        arxiv_id = ARXIV_ID_RE.fullmatch(src).group(1)
        _download(f"https://arxiv.org/pdf/{arxiv_id}.pdf", cached)
        return cached

    raise SystemExit(f"无法识别的输入: {src}")


def _download(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[DOWNLOAD] {url} -> {out}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
        shutil.copyfileobj(r, f)
    print(f"[DOWNLOAD] Done, {out.stat().st_size // 1024}KB", flush=True)


# ---------- 标题提取 ----------


def extract_title(pdf: Path) -> str:
    """从 PDF 第一页按最大字号提取标题文本（本地 pymupdf，无 API）。"""
    import pymupdf

    doc = pymupdf.open(pdf)
    meta_title = ((doc.metadata or {}).get("title", "") or "").strip()

    candidate = ""
    try:
        page = doc[0]
        spans = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                for sp in line.get("spans", []):
                    txt = (sp.get("text") or "").strip()
                    if len(txt) < 3:
                        continue
                    spans.append((sp.get("size", 0), sp.get("bbox", [0, 0, 0, 0])[1], txt))
        if spans:
            max_size = max(s[0] for s in spans)
            top = [s for s in spans if s[0] >= max_size - 0.1]
            top.sort(key=lambda x: x[1])
            top_y = top[0][1]
            candidate = " ".join(s[2] for s in top if abs(s[1] - top_y) < max_size * 3).strip()
    finally:
        doc.close()

    if len(candidate) >= 6 and not candidate.lower().startswith("arxiv"):
        return candidate
    if len(meta_title) >= 6:
        return meta_title
    return candidate or meta_title or "paper"


# ---------- 文件名清理 ----------

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _normalize_mixed_spacing(text: str) -> str:
    """对标题不做任何中英文/数字间距处理，原样返回（仅裁掉首尾空白）。

    翻译模型给的标题（如「受 Transformer 驱动的」）直接用作文件名，
    不在 CJK 与 Latin/数字之间增删空格，保留 LLM 输出的原始排版。
    """
    if not text:
        return text
    return text.strip()


def sanitize_filename(name: str, maxlen: int = 80) -> str:
    name = INVALID_CHARS.sub("", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name or "paper"


# ---------- 短文本白名单（§6.1 规则 5 / §7 规则 11）----------

TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
PLACEHOLDER_RE = re.compile(
    r"\{\s*v\d+\s*\}"          # 公式占位符 {v1}
    r"|\{\s*[A-Za-z_]\w*\s*\}"  # 具名占位符 {name}
    r"|%[sd]"                    # printf 占位符
    r"|\[\[[^\]]*\]\]"
    r"|%%[^%]*%%"
)

LABEL_RE = re.compile(
    r"(?i)^(fig|figure|tab|table|eq|equation|alg|algorithm|sec|section|app|appendix)"
    r"\.?\s*[0-9ivxlc]{1,5}[a-z]?\s*[.:)]?$"
)
URL_RE = re.compile(r"(?i)^(https?://|www\.|doi\s*:|arxiv\s*:)\S*$")
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-+/_.]*$")
ARXIV_STAMP_RE = re.compile(r"(?i)^arxiv[:\s]")

# 参考文献识别：仅有「编号前缀」远远不够 —— 正文里的编号列表（"1. Residual Inverted
# Bottlenecks, ..."）会被误伤。必须再要求出现「姓, 首字母.」的作者署名模式。
REF_NUM_RE = re.compile(r"^(\[\d{1,3}\]|\d{1,3}\s*[.)])\s+\S")
AUTHOR_RE = re.compile(r"[A-Z][A-Za-z\-']+,\s*[A-Z]\.")
YEAR_RE = re.compile(r"\((?:19|20)\d{2}\)")


def looks_like_reference(core: str) -> bool:
    if not REF_NUM_RE.match(core):
        return False
    n = len(AUTHOR_RE.findall(core))
    return n >= 2 or (n >= 1 and bool(YEAR_RE.search(core)))


def core_text(text: str) -> str:
    """剥掉标签与占位符后的人类可读内容。"""
    s = TAG_RE.sub(" ", text)
    s = PLACEHOLDER_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def classify_passthrough(
    text: str, translate_refs: bool = False, layout_label: str | None = None
) -> str | None:
    """判断是否可原样透传（不必占用 Agent 翻译预算）。返回理由或 None。"""
    # 版面标签优先级最高：图/表/公式内部文字整体跳过，不看内容长什么样。
    # 规则见 layout_rules.py —— 图注/表注不在该集合内，仍会正常翻译。
    if is_no_translate_layout(layout_label):
        return f"layout:{layout_label.strip().lower()}"
    core = core_text(text)
    if not core:
        return "empty"
    if not re.search(r"[A-Za-z]", core):
        return "no-letter"
    if len(core) <= 2:
        return "tiny"
    if LABEL_RE.match(core):
        return "label"
    if URL_RE.match(core):
        return "url"
    if IDENT_RE.match(core) and sum(1 for c in core if c.isupper()) >= 2:
        return "identifier"
    if not translate_refs and len(core) > 40 and looks_like_reference(core):
        return "reference"
    return None


SECTION_NUM_RE = re.compile(r"^\d+(\.\d+)*\s*[.:)]?\s")
COMMON_SECTION_RE = re.compile(
    r"(?i)^(abstract|introduction|related\s+work|背景|method(s|ology)?|experiments?|results?|"
    r"discussion|conclusions?|references|acknowledg(e)?ments?|appendix|supplementary)\b\W*$"
)


def title_from_sources(sources: list[dict]) -> str | None:
    """优先采用 babeldoc 版面分析给出的 title 段落（比字号启发式可靠得多）。

    layout_label=="title" 会同时包含论文大标题与各级章节标题，因此需要排除
    「编号开头」「常见章节名」「arXiv 水印戳」，取第一条剩下的作为论文标题。
    """
    for it in sources:
        if it.get("layout_label") != "title":
            continue
        c = core_text(it.get("input") or "")
        if len(c) < 12 or ARXIV_STAMP_RE.match(c):
            continue
        if SECTION_NUM_RE.match(c) or COMMON_SECTION_RE.match(c):
            continue
        if " " not in c:  # 单个词几乎不可能是论文标题
            continue
        return c
    return None


# ---------- 工作目录与中间文件 ----------


def work_dir_for(out_dir: Path, source: str) -> Path:
    """由输入源推导出稳定的工作目录（两遍流程必须解析到同一个）。"""
    h = hashlib.md5(source.strip().encode("utf-8")).hexdigest()[:8]
    raw = Path(source).stem if not source.startswith("http") else source.rstrip("/").split("/")[-1]
    return out_dir / "work" / f"{sanitize_filename(raw, 40)}_{h}"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def write_jsonl(path: Path, items: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + ("\n" if items else ""),
        encoding="utf-8",
    )


def key_of(text: str) -> str:
    """内容哈希键：只依赖文本本身，与顺序无关，重算 pending 也不会错位。

    译者（Agent）可以写 {"k": "<12位hash>", "output": ...} 代替逐字重抄
    原文，彻底规避「input 少一个空格 / 吃掉占位符」导致注入未命中的问题。
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def build_key_index(sources: list[dict]) -> dict[str, str]:
    idx = {key_of(TITLE_KEY): TITLE_KEY}
    for it in sources:
        t = it.get("input")
        if isinstance(t, str):
            idx.setdefault(key_of(t), t)
    return idx


def load_translations(work: Path, sources: list[dict] | None = None) -> dict[str, str]:
    """合并译文来源，优先级：auto_filled < translations.json < translations.jsonl。

    translations.jsonl 每行支持两种写法（可混用）::

        {"input": "<原文逐字一致>", "output": "<译文>"}
        {"k": "<pending.jsonl 里的 k 字段>", "output": "<译文>"}
    """
    merged: dict[str, str] = {}
    key_idx = build_key_index(sources or [])

    auto = work / "auto_filled.json"
    if auto.exists():
        try:
            data = json.loads(auto.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if isinstance(v, str)})
        except Exception as e:
            print(f"[WARN] auto_filled.json 解析失败: {e}", flush=True)

    tj = work / "translations.json"
    if tj.exists():
        try:
            data = json.loads(tj.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if isinstance(v, str)})
        except Exception as e:
            print(f"[WARN] translations.json 解析失败: {e}", flush=True)

    bad_keys: list[str] = []
    for obj in load_jsonl(work / "translations.jsonl"):
        v = obj.get("output")
        if not isinstance(v, str):
            continue
        src = obj.get("input")
        if not isinstance(src, str):
            kk = obj.get("k")
            if not isinstance(kk, str):
                continue
            src = key_idx.get(kk)
            if src is None:
                bad_keys.append(kk)
                continue
        merged[src] = v

    if bad_keys:
        print(
            f"[WARN] translations.jsonl 中有 {len(bad_keys)} 条 k 值在源文本里找不到（已跳过）: "
            f"{bad_keys[:5]}",
            flush=True,
        )

    return merged


def refresh_auto_filled(work: Path, sources: list[dict], translate_refs: bool) -> dict:
    """按白名单规则重算 auto_filled.json（派生产物，可反复重算）。"""
    auto: dict[str, str] = {}
    stats: dict[str, int] = {}
    for it in sources:
        text = it.get("input")
        if not isinstance(text, str):
            continue
        reason = classify_passthrough(
            text,
            translate_refs=translate_refs,
            layout_label=it.get("layout_label"),
        )
        if reason:
            auto[text] = text
            stats[reason] = stats.get(reason, 0) + 1
    (work / "auto_filled.json").write_text(
        json.dumps(auto, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return stats


def compute_pending(sources: list[dict], translations: dict[str, str]) -> list[dict]:
    pending = []
    for it in sources:
        k = it.get("input")
        if not isinstance(k, str):
            continue
        v = translations.get(k)
        if isinstance(v, str) and v.strip():
            continue
        pending.append(it)
    return pending


def dump_pending(work: Path, pending: list[dict], title_en: str, need_title: bool) -> Path:
    rows: list[dict] = []
    if need_title:
        rows.append(
            {
                "k": key_of(TITLE_KEY),
                "input": TITLE_KEY,
                "layout_label": "_title",
                "source_title": title_en,
                "note": "译论文标题；译文写回该 k，用作输出 PDF 的文件名",
            }
        )
    rows.extend(
        {"k": key_of(it["input"]), "input": it["input"], "layout_label": it.get("layout_label")}
        for it in pending
    )
    p = work / "pending.jsonl"
    write_jsonl(p, rows)
    return p


TAG_TOKEN_RE = re.compile(r"</?[A-Za-z][^>]*>")


def verify_pair(src: str, out: str) -> list[str]:
    """校验译文是否保住了原文的标签与占位符（版面保真的硬约束）。"""
    problems = []

    st, ot = TAG_TOKEN_RE.findall(src), TAG_TOKEN_RE.findall(out)
    if st != ot:
        if sorted(st) == sorted(ot):
            problems.append(f"标签顺序改变: {st} -> {ot}")
        else:
            miss = sorted({t for t in st if st.count(t) > ot.count(t)})
            extra = sorted({t for t in ot if ot.count(t) > st.count(t)})
            problems.append(f"标签不一致 缺失={miss} 多余={extra}")

    sp, op = PLACEHOLDER_RE.findall(src), PLACEHOLDER_RE.findall(out)
    if sorted(sp) != sorted(op):
        problems.append(f"占位符不一致: {sorted(sp)} -> {sorted(op)}")

    if src.strip() and not out.strip():
        problems.append("译文为空")

    return problems


def stage_merge(work: Path) -> None:
    """把 work 下的 _batch_*.jsonl 合并进 translations.jsonl，并做一致性校验。

    分批翻译时每批单独落一个 _batch_NN.jsonl，可重跑、可覆盖、可回溯；
    本阶段负责汇总 + 体检，避免坏数据直接进入渲染。
    """
    sources = load_jsonl(work / "source_texts.jsonl")
    if not sources:
        raise SystemExit(f"未找到 {work/'source_texts.jsonl'}，请先运行 --collect-only。")
    key_idx = build_key_index(sources)

    batches = sorted(work.glob("_batch_*.jsonl"))
    if not batches:
        raise SystemExit(f"未找到任何 _batch_*.jsonl（目录: {work}）")

    merged: dict[str, str] = {}
    unknown: list[str] = []
    for b in batches:
        rows = load_jsonl(b)
        for obj in rows:
            v = obj.get("output")
            if not isinstance(v, str):
                continue
            src = obj.get("input")
            if not isinstance(src, str):
                kk = obj.get("k")
                src = key_idx.get(kk) if isinstance(kk, str) else None
                if src is None:
                    unknown.append(f"{b.name}:{kk}")
                    continue
            merged[src] = v
        print(f"[MERGE] {b.name}: {len(rows)} 行", flush=True)

    bad = 0
    for src, out in merged.items():
        if src == TITLE_KEY:
            continue
        probs = verify_pair(src, out)
        if probs:
            bad += 1
            print(f"[CHECK] k={key_of(src)} {' | '.join(probs)}", flush=True)
            print(f"         原文: {src[:110]}", flush=True)
            print(f"         译文: {out[:110]}", flush=True)

    out_path = work / "translations.jsonl"
    write_jsonl(out_path, [{"input": k, "output": v} for k, v in merged.items()])

    print(
        f"[MERGE] 合并 {len(batches)} 个批次 -> {out_path}（{len(merged)} 条）；"
        f"校验异常 {bad} 条；未知 k {len(unknown)} 个",
        flush=True,
    )
    if unknown:
        print(f"[MERGE] 未知 k 示例: {unknown[:5]}", flush=True)


def report_status(work: Path, sources: list[dict], translations: dict[str, str]) -> dict:
    keys = [it["input"] for it in sources if isinstance(it.get("input"), str)]
    total = len(keys)
    done = sum(1 for k in keys if isinstance(translations.get(k), str) and translations[k].strip())
    coverage = (done / total) if total else 1.0
    return {"total": total, "translated": done, "missing": total - done, "coverage": round(coverage, 4)}


# ---------- 阶段实现 ----------


def stage_collect(args, work: Path, pdf: Path) -> None:
    from offline_translator import run_babeldoc_offline

    print("[PASS1] 解析版面并收集源文本（零外部 API，不渲染 PDF）...", flush=True)
    t = run_babeldoc_offline(
        pdf,
        work,
        mode="collect",
        pages=args.pages,
        render=False,
        tolerate_errors=True,
        progress_cb=lambda s: print(f"  - {s}", flush=True),
    )
    if t.error:
        print(f"[WARN] 管线后段报错（不影响已收集文本）: {t.error}", flush=True)

    sources = t.unique_collected()
    if not sources:
        raise SystemExit("未收集到任何源文本，请检查输入 PDF 或 --pages 范围。")

    write_jsonl(work / "source_texts.jsonl", sources)

    # 图/表内部文字的跳过清单落盘，便于核查有没有误伤正文
    dump_figure_table_skipped(work)

    # 标题优先取 babeldoc 版面分析的 title 段落，退化时才用字号启发式
    title_en = title_from_sources(sources) or extract_title(pdf)
    print(f"[TITLE-EN] {title_en}", flush=True)

    meta = {
        "title_en": title_en,
        "pdf": str(pdf),
        "pages": args.pages,
        "source_count": len(sources),
        "translate_refs": bool(args.translate_refs),
    }
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    stats = refresh_auto_filled(work, sources, args.translate_refs)
    translations = load_translations(work, sources)
    pending = compute_pending(sources, translations)
    pending_path = dump_pending(work, pending, title_en, need_title=TITLE_KEY not in translations)
    st = report_status(work, sources, translations)

    print(f"[COLLECT] 源文本 {len(sources)} 条；白名单自动透传 {sum(stats.values())} 条 {stats or ''}", flush=True)
    print(f"[COLLECT] 待 Agent 翻译 {len(pending)} 条 -> {pending_path}", flush=True)
    print(
        "[DONE-COLLECT]",
        json.dumps(
            {
                "work_dir": str(work),
                "source_texts": str(work / "source_texts.jsonl"),
                "pending": str(pending_path),
                "meta": str(work / "meta.json"),
                "write_translations_to": str(work / "translations.jsonl"),
                "count": len(sources),
                "pending_count": len(pending),
                "auto_filled": sum(stats.values()),
                "auto_filled_by_rule": stats,
                "coverage": st["coverage"],
                "title_en": title_en,
            },
            ensure_ascii=False,
        ),
    )


def dump_figure_table_skipped(work: Path) -> int:
    """把「因落在图/表区域而整体跳过」的段落写盘，供人工核查是否误伤正文。

    只在 collect 阶段调用；文件是派生产物，每次重跑覆盖。
    """
    try:
        from offline_translator import FIGURE_TABLE_SKIPPED
    except Exception:  # noqa: BLE001
        return 0

    # 同一段落可能在多轮 pass 中重复出现，按 (页码, 文本) 去重
    seen = set()
    rows = []
    for page_no, label, text in FIGURE_TABLE_SKIPPED:
        key = (page_no, text)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"page": page_no, "layout_label": label, "text": text})

    if not rows:
        return 0

    write_jsonl(work / "skipped_figure_table.jsonl", rows)
    long_ones = [r for r in rows if len(r["text"]) >= 60]
    print(
        f"[FIG-TABLE] 图/表内部文字整体跳过 {len(rows)} 段（不翻译）"
        f" -> {work / 'skipped_figure_table.jsonl'}",
        flush=True,
    )
    if long_ones:
        print(
            f"[FIG-TABLE] 其中 {len(long_ones)} 段长度 ≥60 字符，"
            "若发现正文被误伤请调低 layout_rules.OVERLAP_THRESHOLD",
            flush=True,
        )
    return len(rows)


def stage_status(args, work: Path) -> None:
    sources = load_jsonl(work / "source_texts.jsonl")
    if not sources:
        raise SystemExit(f"未找到 {work/'source_texts.jsonl'}，请先运行 --collect-only。")

    meta = {}
    mp = work / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    title_en = meta.get("title_en", "")
    translate_refs = args.translate_refs or bool(meta.get("translate_refs"))

    refresh_auto_filled(work, sources, translate_refs)
    translations = load_translations(work, sources)
    pending = compute_pending(sources, translations)
    pending_path = dump_pending(work, pending, title_en, need_title=TITLE_KEY not in translations)
    st = report_status(work, sources, translations)
    st.update(
        {
            "work_dir": str(work),
            "pending": str(pending_path),
            "pending_count": len(pending),
            "title_translated": TITLE_KEY in translations,
            "write_translations_to": str(work / "translations.jsonl"),
        }
    )
    print("[STATUS]", json.dumps(st, ensure_ascii=False))


def stage_render(args, work: Path, pdf: Path, out_dir: Path) -> None:
    from offline_translator import run_babeldoc_offline

    sources = load_jsonl(work / "source_texts.jsonl")
    if not sources:
        raise SystemExit(
            f"未找到 {work/'source_texts.jsonl'}。\n"
            f"请先运行：python {Path(__file__).name} \"<输入>\" --output-dir {out_dir} --collect-only"
        )

    meta = {}
    mp = work / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    title_en = meta.get("title_en") or title_from_sources(sources) or extract_title(pdf)
    if meta.get("pages") != args.pages:
        print(
            f"[WARN] 本次 --pages={args.pages} 与收集阶段 --pages={meta.get('pages')} 不一致，"
            "可能导致覆盖率异常。",
            flush=True,
        )

    refresh_auto_filled(work, sources, args.translate_refs or bool(meta.get("translate_refs")))
    translations = load_translations(work, sources)
    if not translations:
        raise SystemExit(
            f"缺少译文：请把 Agent 的译文写入 {work/'translations.jsonl'}"
            '（每行 {"input": ..., "output": ...}）后重试。'
        )

    st = report_status(work, sources, translations)
    print(
        f"[COVERAGE] {st['translated']}/{st['total']} = {st['coverage']:.1%}"
        f"（缺失 {st['missing']} 条）",
        flush=True,
    )
    if st["coverage"] < COVERAGE_THRESHOLD and not args.allow_partial:
        pending = compute_pending(sources, translations)
        pending_path = dump_pending(work, pending, title_en, need_title=TITLE_KEY not in translations)
        sample = [p["input"][:80] for p in pending[:5]]
        raise SystemExit(
            f"译文覆盖率 {st['coverage']:.1%} 低于阈值 {COVERAGE_THRESHOLD:.0%}，已中止以避免产出半英半中的 PDF。\n"
            f"  未翻译条目已写入: {pending_path}（{len(pending)} 条）\n"
            f"  示例: {sample}\n"
            f"  补齐后重试，或加 --allow-partial 强制渲染（缺失项将保留英文原文）。"
        )

    title_zh = translations.get(TITLE_KEY) or translations.get(title_en) or title_en
    title_zh = _normalize_mixed_spacing(title_zh)
    # 把规范化后的标题写回 translations，使渲染出的 PDF 标题也带正确空格
    if TITLE_KEY in translations:
        translations[TITLE_KEY] = title_zh
    if title_en in translations:
        translations[title_en] = title_zh
    print(f"[TITLE-ZH] {title_zh}", flush=True)

    print("[PASS2] 回填译文并渲染中文 PDF（零外部 API）...", flush=True)
    t = run_babeldoc_offline(
        pdf,
        work,
        mode="lookup",
        translations=translations,
        pages=args.pages,
        dual=args.dual,
        render=True,
        progress_cb=lambda s: print(f"  - {s}", flush=True),
    )
    print(
        f"[INJECT] 命中 {t.hit} 段，未命中 {t.miss} 段（未命中保留英文原文）；"
        f"图/表/公式整体跳过 {getattr(t, 'skipped_layout', 0)} 段",
        flush=True,
    )
    if t.miss and t.missed_texts:
        print(f"[INJECT] 未命中示例: {t.missed_texts[0][:100]}", flush=True)

    monos = sorted(work.glob("*.zh.mono.pdf"), key=lambda p: p.stat().st_mtime)
    if not monos:
        raise SystemExit(f"未找到 babeldoc 输出的 *.zh.mono.pdf（目录: {work}）")
    mono = monos[-1]

    out_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(title_zh)

    # 最终成品文件名即论文中文标题（完整中文名，不带任何随机后缀）；
    # 若同名文件已存在（如上一轮未重排版），直接覆盖，保证目录里只有一个干净的成品。
    delivered = out_dir / f"{safe}.pdf"

    dual_final = None
    if args.dual:
        duals = sorted(work.glob("*.zh.dual.pdf"), key=lambda p: p.stat().st_mtime)
        if duals:
            dual_final = out_dir / f"{safe}_双语.pdf"  # 双语版显式标注，不与单语成品冲突
            shutil.copy2(duals[-1], dual_final)

    # 默认对参考文献区做悬挂缩进重排（--no-reflow 可跳过）。
    # 重排后的成品即主交付，文件名为 {中文标题}.pdf；不再单独保留未重排版。
    reflow_on = bool(args.reflow)
    try:
        if reflow_on:
            from reflow_references import reflow_pdf

            reflow_pdf(str(mono), str(delivered))
            print(f"[REFLOW] 参考文献悬挂缩进重排完成 -> {delivered}", flush=True)
        else:
            shutil.copy2(mono, delivered)
    except Exception as e:  # 重排/复制失败都不应拖垮整条流水线
        print(
            f"[{'REFLOW' if reflow_on else 'RENDER'}] 处理失败，回退为直接复制原始渲染 PDF：{e}",
            flush=True,
        )
        if not delivered.exists() or delivered.stat().st_size == 0:
            shutil.copy2(mono, delivered)

    zh_chars, latin_words = _measure_pdf(delivered)

    print(
        "[DONE]",
        json.dumps(
            {
                "title_en": title_en,
                "title_zh": title_zh,
                "output_pdf": str(delivered),
                "output_pdf_plain": None,
                "output_dual_pdf": str(dual_final) if dual_final else None,
                "size_kb": delivered.stat().st_size // 1024,
                "coverage": st["coverage"],
                "inject_hit": t.hit,
                "inject_miss": t.miss,
                "layout_skipped": getattr(t, "skipped_layout", 0),
                "zh_chars": zh_chars,
                "latin_words": latin_words,
                "reflow": reflow_on,
            },
            ensure_ascii=False,
        ),
    )

    # 注意：本脚本**不自动删除**工作目录 work/ —— 沙箱对单次删除文件数有限额
    #（work/ 内文件数常超过上限会被拒），且按约定删除操作一律由用户亲自执行。
    # 任务完成后，调用方（智能体）应主动告知用户：<输出目录>/work/ 为可安全删除的
    # 中间产物，以及最终生成的翻译版 PDF 路径（即上面的 output_pdf）。


def _measure_pdf(pdf: Path) -> tuple[int, int]:
    """粗略统计输出 PDF 的中文字符数与英文单词数，用于验收。"""
    try:
        import pymupdf

        doc = pymupdf.open(pdf)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        return -1, -1
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"\b[A-Za-z]{3,}\b", text))
    return zh, latin


# ---------- 主流程 ----------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="BabelDOC 论文翻译（零外部 API，译文由 Agent 在对话中提供）"
    )
    ap.add_argument("source", help="arxiv URL / arxiv ID / 本地 PDF 路径")
    ap.add_argument("--output-dir", default=None, help="最终 PDF 输出目录（默认：本地源 PDF 所在目录；传入 URL/arxiv 时退回 ~/babeldoc_output）")
    ap.add_argument("--work-dir", default=None, help="中间文件目录（默认 <output-dir>/work/<自动>，运行后保留不自动删除）")
    ap.add_argument("--pages", default=None, help='仅处理指定页，如 "1-5" 或 "1,2,5-"')
    ap.add_argument("--dual", action="store_true", help="额外输出双语对照 PDF")
    ap.add_argument("--collect-only", action="store_true", help="阶段 1：只收集源文本，交给 Agent 翻译")
    ap.add_argument("--status", action="store_true", help="查看译文覆盖率并刷新 pending.jsonl")
    ap.add_argument(
        "--merge",
        action="store_true",
        help="把 work 下的 _batch_*.jsonl 合并成 translations.jsonl 并做标签/占位符校验",
    )
    ap.add_argument("--allow-partial", action="store_true", help="覆盖率不足时仍强制渲染（缺失项保留英文）")
    ap.add_argument("--translate-refs", action="store_true", help="参考文献条目也交给 Agent 翻译（默认原样保留）")
    ap.add_argument(
        "--reflow",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="渲染完成后对参考文献区做悬挂缩进重排（默认开；用 --no-reflow 关闭）。重排后的成品即主交付，文件名为 {中文标题}.pdf",
    )
    # 兼容旧调用：这些参数已废弃，保留仅为不报错
    for dead in ("--api-key", "--api-base", "--model", "--qps"):
        ap.add_argument(dead, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if any(getattr(args, d) is not None for d in ("api_key", "api_base", "model", "qps")):
        print("[WARN] --api-key/--api-base/--model/--qps 已废弃并被忽略：本 skill 不再调用外部 API。", flush=True)

    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
    else:
        # 默认输出到源 PDF 所在目录，避免落到 ~/babeldoc_output（用户要求产物就近存放）
        src = Path(args.source)
        out_dir = (
            src.resolve().parent
            if src.exists() and src.is_file()
            else (Path.home() / "babeldoc_output")
        ).resolve()
    work = Path(args.work_dir).resolve() if args.work_dir else work_dir_for(out_dir, args.source)
    work.mkdir(parents=True, exist_ok=True)
    print(f"[WORK] {work}", flush=True)

    if args.merge:
        stage_merge(work)
        if not args.status:
            return

    if args.status:
        stage_status(args, work)
        return

    pdf = resolve_input(args.source, work)
    print(f"[INPUT] PDF: {pdf}", flush=True)

    if args.collect_only:
        stage_collect(args, work, pdf)
    else:
        stage_render(args, work, pdf, out_dir)


if __name__ == "__main__":
    main()
