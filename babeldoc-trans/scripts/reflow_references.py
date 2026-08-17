#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应悬挂缩进修复（支持单栏 / 双栏）。

自动探测版心几何：参考文献标题、字号、左右栏边界、栏数；
把参考文献重排为「首行顶格、续行悬挂缩进」，
且悬挂缩进按条目序号做渐变：首条少缩进约一个大写字母宽、末条几乎不缩进。

只处理参考文献区；正文 / 图表 / 其它页零改动。
操作对象应为已翻译渲染的成品 PDF（副本）。

用法:
    python reflow_references.py <输入PDF> <输出PDF> [--cap CAP] [--tiny TINY]
"""
import re
import sys
import unicodedata
import fitz
from pathlib import Path
from collections import Counter

TITLE_CANDIDATES = ("参考文献", "References", "REFERENCES")
COLUMN_GAP_PT = 80.0     # 判定两栏的最小栏间空白阈值
TOP_MARGIN = 50.0        # 非标题页参考文献区顶部留白
BOTTOM_MARGIN = 40.0     # 页脚留白


def find_title(doc):
    for pno in range(doc.page_count):
        d = doc[pno].get_text("dict")
        for b in d["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    if sp["text"].strip() in TITLE_CANDIDATES:
                        return pno, sp
    return None, None


def find_ref_end_pno(doc, title_pno, title_bbox, title_size, ref_size):
    """定位参考文献 section 的最后一页。

    很多论文在参考文献之后还有 Supplementary Material / Appendix /
    Acknowledgements / Code / Results / 补充材料 等章节；若继续把后续页面
    当成参考文献处理，会把这些章节 wiping 掉并导致排版灾难。本函数扫描后续
    页面，遇到明显的大章节标题时返回其前一页索引。

    判定依据（而非单纯看字号，避免把运行页眉「10 Roy et al.」误判为章节）：
      · 字号接近参考文献大标题（≥ title_size * 0.8）；
      · 文本命中章节关键词（中英文均可）；
      · 且不是运行页眉（如 "10 Roy et al." / "MedNeXt: ... N"）。
    """
    if title_pno is None or title_pno >= doc.page_count - 1:
        return doc.page_count - 1

    section_pat = re.compile(
        r"^\s*(补充材料|附录|致谢|结论|讨论|参考资料|补充说明|"
        r"Supplementary|Appendix|Acknowledgements?|Acknowledgments?|"
        r"Data\s+Availability|Code\s+Availability|Ethics|Declarations?|"
        r"Author\s+Contributions?|List\s+of\s+(Figures?|Tables?)|"
        r"References|REFERENCES|参考文献)\b",
        re.I,
    )
    # 运行页眉形如 "10 Roy et al." / "MedNeXt: Transformer-driven ... 9"
    header_pat = re.compile(
        r"^\s*(\d+\s+[A-Z][\w.\- ]*et\s+al\.?|MedNeXt)", re.I)

    for pno in range(title_pno + 1, doc.page_count):
        page = doc[pno]
        page_h = page.rect.height
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    text = unicodedata.normalize("NFKC", sp["text"].strip())
                    if not text:
                        continue
                    y0 = sp["bbox"][1]
                    # 跳过页眉/页脚带；章节标题通常位于版心顶部区域
                    if y0 < 45 or y0 > page_h - 40:
                        continue
                    size = sp["size"]
                    # 章节标题字号应接近（不小于 0.8 倍）参考文献大标题；
                    # 运行页眉字号较小会被此门槛自然挡掉
                    if size < title_size * 0.8:
                        continue
                    if header_pat.match(text):
                        continue
                    if section_pat.search(text):
                        return pno - 1
    return doc.page_count - 1


def gather_ref_spans(doc, title_pno, title_bbox, end_pno=None):
    """收集标题之后的所有参考文献文本 span（跳过标题本身），用于几何探测。
    双栏时右栏可能从标题页顶部开始，因此只跳过与标题 bbox 重叠的 span。
    end_pno 为参考文献 section 的最后一页（含），避免把后续章节算进来。"""
    tx0, ty0, tx1, ty1 = title_bbox
    margin = 3.0
    title_rect = fitz.Rect(tx0 - margin, ty0 - margin, tx1 + margin, ty1 + margin)
    spans = []
    last = doc.page_count - 1 if end_pno is None else end_pno
    for p in range(title_pno, last + 1):
        d = doc[p].get_text("dict")
        for b in d["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    if not t:
                        continue
                    if p == title_pno and fitz.Rect(sp["bbox"]).intersects(title_rect):
                        continue  # 跳过标题本身
                    spans.append({
                        "pno": p,
                        "x0": sp["bbox"][0], "y0": sp["bbox"][1],
                        "x1": sp["bbox"][2], "y1": sp["bbox"][3],
                        "text": t, "size": sp["size"], "font": sp["font"],
                    })
    return spans


def detect_columns(spans):
    """按 x0 聚类判断栏数；返回每栏的 (x0, x1)。
    双栏论文的栏间 gutter 可能只有 15-30 pt，用峰值检测 + 首行边界法分隔。"""
    if not spans:
        return []
    xs = [round(s["x0"], 1) for s in spans]
    c = Counter(xs)
    if len(c) < 2:
        return [(min(s["x0"] for s in spans), max(s["x1"] for s in spans))]

    most = c.most_common()
    peak1 = most[0][0]
    # 第二峰值需与主峰明显分离，且数量不可过少（避免悬挂缩进/首行缩进等内部偏移被误判为第二栏）
    MIN_COL_SEP = 100.0
    peak2 = None
    for x, n in most[1:]:
        if abs(x - peak1) >= MIN_COL_SEP and n >= max(5, most[0][1] * 0.03):
            peak2 = x
            break
    if peak2 is None:
        return [(min(s["x0"] for s in spans), max(s["x1"] for s in spans))]

    # 用主峰附近首行 span 的右/左边界定位真实栏边界（避免 clip 重叠导致条目碎裂）
    win = 10.0
    left_first = [s for s in spans if peak1 - win <= s["x0"] <= peak1 + win]
    right_first = [s for s in spans if peak2 - win <= s["x0"] <= peak2 + win]
    if not left_first or not right_first:
        return [(min(s["x0"] for s in spans), max(s["x1"] for s in spans))]
    left_x1 = max(s["x1"] for s in left_first)
    right_x0 = min(s["x0"] for s in right_first)
    split = (left_x1 + right_x0) / 2

    left = [s for s in spans if s["x0"] < split]
    right = [s for s in spans if s["x0"] >= split]
    if not left or not right:
        return [(min(s["x0"] for s in spans), max(s["x1"] for s in spans))]
    return [
        (min(s["x0"] for s in left), left_x1),
        (right_x0, max(s["x1"] for s in right)),
    ]


def dominant_size(spans):
    c = Counter(round(s["size"], 1) for s in spans)
    return c.most_common(1)[0][0]


def build_cells(doc, title_pno, title_bbox, title_size, cols, end_pno=None):
    """按阅读顺序（页序 × 栏序）构建单元格列表。
    标题页中：含标题的栏从标题下方开始，其余栏可从页顶开始。
    end_pno 为参考文献 section 的最后一页（含）。"""
    page_h = doc[title_pno].rect.height
    page_w = doc[title_pno].rect.width
    tx0, ty0, tx1, ty1 = title_bbox
    cells = []
    last = doc.page_count - 1 if end_pno is None else end_pno
    for p in range(title_pno, last + 1):
        for (cx0, cx1) in cols:
            if p == title_pno:
                # 该栏是否与标题水平相交？
                col_overlaps_title = not (cx1 < tx0 or cx0 > tx1)
                y_top = (ty0 + title_size * 1.6 + 4) if col_overlaps_title else TOP_MARGIN
            else:
                y_top = TOP_MARGIN
            y_bottom = page_h - BOTTOM_MARGIN
            cells.append((p, cx0, cx1, y_top, y_bottom))
    return cells, page_w


def extract_ref_text(doc, cells):
    """逐单元格 clip 抽取全文，按阅读顺序拼接；把 PDF 换行规范化成空格，
    避免换行符被 wrap 当成单词的一部分导致渲染混乱。"""
    parts = []
    for (p, cx0, cx1, y_top, y_bottom) in cells:
        clip = fitz.Rect(cx0 - 3, y_top - 3, cx1 + 3, y_bottom + 3)
        t = doc[p].get_text("text", clip=clip)
        t = " ".join(t.split())  # 规范化空白（含换行）
        parts.append(t)
    return " ".join(parts)


def mask_false_positives(t):
    """长度保持掩码：把 'Fig. 3' / 'doi:10.1' / 'et al.' 等里的点换成 '#'，
    使它们不再被误判为条目编号。全部为 1:1 替换，故坐标与原串对齐。"""
    t = re.sub(r"\b(Fig|Figs|Figure|Table|Tables|Eq|Eqs|Sec|Section|Scheme)\.\s*(\d+)",
               lambda m: m.group(1) + "#" + m.group(2) + "#", t)
    t = re.sub(r"\b(doi|DOI|arXiv|http|https)\S*",
               lambda m: m.group(0).replace(".", "#"), t)
    t = re.sub(r"\b(et al|e\.g|i\.e|vs|cf|approx|Vol|pp|No|Proc|IEEE)\.",
               lambda m: m.group(0)[:-1] + "#", t)
    return t


def detect_ref_style(refs_text):
    """探测参考文献编号风格：'[N]'（如 [1] Author）或 'N.'（如 1. Author）。"""
    if re.search(r"\[\d+\]", refs_text):
        return "bracket"
    return "dot"


def split_entries_text(refs_text):
    """按连续编号切分条目。在掩码副本上找边界（避开 'Fig. 3.' / 'doi:10.' 误判），
    再回原文字符串切片。同时支持 '[N]' 与 'N.' 两种编号风格。"""
    masked = mask_false_positives(refs_text)
    style = detect_ref_style(refs_text)
    pat = r"\[(\d+)\]" if style == "bracket" else r"(?<!\d)(\d+)\. "
    expected = 1
    positions = []
    for m in re.finditer(pat, masked):
        n = int(m.group(1))
        if n == expected:
            positions.append(m.start())
            expected += 1
    if not positions:
        return [refs_text.strip()]
    entries = []
    for i in range(len(positions) - 1):
        entries.append(refs_text[positions[i]:positions[i + 1]].strip())
    entries.append(refs_text[positions[-1]:].strip())
    return entries


def wrap(text, first_w, rest_w, font, size):
    words = text.split(" ")
    lines, cur, avail = [], "", first_w
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or font.text_length(trial, size) <= avail:
            cur = trial
        else:
            lines.append(cur)
            cur, avail = w, rest_w
            while font.text_length(cur, size) > avail:
                cut = len(cur)
                while cut > 1 and font.text_length(cur[:cut], size) > avail:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def reflow_pdf(inp, outp, cap=None, tiny=None):
    """入口函数：对成品 PDF 做参考文献悬挂缩进重排。
    供 translate.py 的 --reflow 调用，也可在 CLI 直接使用。"""
    doc = fitz.open(inp)
    title_pno, title_sp = find_title(doc)
    assert title_pno is not None, "未找到参考文献标题"
    title_bbox = title_sp["bbox"]
    title_text = title_sp["text"].strip()
    title_size = title_sp["size"]
    title_font = "china-s" if re.search(r"[一-鿿]", title_text) else "helv"
    print(f"[定位] 标题'{title_text}' 第{title_pno + 1}页 y={title_bbox[1]:.0f} 字号{title_size:.1f}")

    # 先估算参考文献正文字号，再判断参考文献 section 的边界
    tmp_spans = gather_ref_spans(doc, title_pno, title_bbox)
    ref_size = dominant_size(tmp_spans) if tmp_spans else title_size
    ref_end_pno = find_ref_end_pno(doc, title_pno, title_bbox, title_size, ref_size)
    post_doc = None
    if ref_end_pno < doc.page_count - 1:
        post_doc = fitz.open()
        post_doc.insert_pdf(doc, from_page=ref_end_pno + 1, to_page=doc.page_count - 1)
        doc.delete_pages(range(ref_end_pno + 1, doc.page_count))
        print(f"[边界] 参考文献到第 {ref_end_pno + 1} 页；后续 {post_doc.page_count} 页暂不处理，重排后恢复")

    spans = gather_ref_spans(doc, title_pno, title_bbox, ref_end_pno)
    assert spans, "未收集到参考文献文本"
    cols = detect_columns(spans)
    size = dominant_size(spans)
    font = fitz.Font("helv")
    print(f"[栏] 检测到 {len(cols)} 栏: {cols}")
    print(f"[字号] 参考文献正文 {size:.1f}pt")

    cells, page_w = build_cells(doc, title_pno, title_bbox, title_size, cols, ref_end_pno)
    refs_text = extract_ref_text(doc, cells)
    entries = split_entries_text(refs_text)
    print(f"[切分] 共 {len(entries)} 条；首条={entries[0][:35]!r} 末条={entries[-1][:35]!r}")

    CAP = cap if cap is not None else font.text_length("M", size)
    TINY = tiny if tiny is not None else 0.4
    N = len(entries)
    def corr(ei):
        return TINY + (CAP - TINY) * (N - 1 - ei) / max(1, N - 1)

    # 悬挂基线 = 编号标记宽度（'N. '/'[N] '）+ 余量
    style = detect_ref_style(refs_text)
    marker = "[00] " if style == "bracket" else "00. "
    base_hang = font.text_length(marker, size) + 2

    col_w = cols[0][1] - cols[0][0]
    wrapped = [wrap(e, col_w, col_w - base_hang, font, size) for e in entries]

    # 覆盖白底（标题行 + 各单元格）
    ty = title_bbox[1]
    doc[title_pno].add_redact_annot(
        fitz.Rect(0, ty - 3, page_w, ty + title_size * 1.6 + 4), fill=(1, 1, 1))
    for (p, cx0, cx1, y_top, y_bottom) in cells:
        doc[p].add_redact_annot(fitz.Rect(cx0 - 3, y_top - 3, cx1 + 3, y_bottom + 3), fill=(1, 1, 1))
    for p in range(title_pno, doc.page_count):
        doc[p].apply_redactions()

    # 重绘标题
    doc[title_pno].insert_text(
        (title_sp["bbox"][0], title_bbox[1] + title_size * 0.8),
        title_text, fontname=title_font, fontsize=title_size, color=(0, 0, 0))

    # 按单元格流填
    ci = 0
    pno, cx0, cx1, y_top, y_bottom = cells[0]
    y = y_top
    line_h = size * 1.35
    for ei, ls in enumerate(wrapped):
        hang_i = base_hang - corr(ei)
        for i, line in enumerate(ls):
            x = cx0 + (hang_i if i > 0 else 0)
            if y > y_bottom:
                ci += 1
                if ci >= len(cells):
                    doc.insert_page(doc.page_count)
                    np_ = doc.page_count - 1
                    cells.append((np_, cx0, cx1, TOP_MARGIN, doc[np_].rect.height - BOTTOM_MARGIN))
                    pno, cx0, cx1, y_top, y_bottom = cells[ci]
                else:
                    pno, cx0, cx1, y_top, y_bottom = cells[ci]
                y = y_top
            doc[pno].insert_text((x, y), line, fontname="helv", fontsize=size, color=(0, 0, 0))
            y += line_h

    # 恢复参考文献之后的章节（ Supplementary / Appendix / Results 等）
    if post_doc is not None and post_doc.page_count:
        doc.insert_pdf(post_doc)
        post_doc.close()

    doc.save(outp, garbage=4, deflate=True)
    doc.close()
    print(f"[完成] 写入 {outp}  (首条续行x={cols[0][0] + (base_hang - corr(0)):.1f}, "
          f"末条续行x={cols[0][0] + (base_hang - corr(N - 1)):.1f})")


def main():
    args = sys.argv[1:]
    outp = inp = None
    cap_arg = tiny_arg = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--cap":
            cap_arg = float(args[i + 1]); i += 2; continue
        if a == "--tiny":
            tiny_arg = float(args[i + 1]); i += 2; continue
        if inp is None:
            inp = a
        elif outp is None:
            outp = a
        i += 1
    if not inp or not outp:
        print("用法: python reflow_references.py <输入PDF> <输出PDF> [--cap CAP] [--tiny TINY]")
        sys.exit(1)
    reflow_pdf(inp, outp, cap_arg, tiny_arg)


if __name__ == "__main__":
    main()
