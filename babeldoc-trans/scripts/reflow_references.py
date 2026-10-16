#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应悬挂缩进修复（单栏 / 双栏均可）。

自动探测版式几何：栏数、参考文献标题、字号、左右栏边界；
把参考文献重排为「首行顶格、续行悬挂缩进」，
且悬挂缩进按条目序号做渐变：首条少缩进约一个大写字母宽、末条几乎不缩进。

栏数判定以**全文版式**为权威（逐页搜索「版心中缝」后取多数），而不是只看参考文献区
的 span 聚类——参考文献区是全文 x0 分布最乱的地方，单栏论文在那里也会被聚出「假第二峰」，
按它硬拆双栏会把单栏论文的参考文献压进窄栏、行尾截断。

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
TOP_MARGIN = 50.0        # 非标题页参考文献区顶部留白（下限）
BOTTOM_MARGIN = 40.0     # 页脚留白
HEADER_GAP_PT = 6.0      # 页眉与正文的最小垂直间隔，用于识别运行页眉
GUTTER_RATIO = 0.12      # 版心中段「被正文行穿过的比例」低于此值即认定存在栏间中缝（双栏）

# ---- 文献区终点的「结构特征」判定（不依赖章节标题）---------------------------------
# 大量 arXiv 论文的参考文献之后**没有任何章节标题**，直接排附录表格（页首即 "Table 2."
# 之类表题）。若边界只按章节关键词判定，边界会一路吃到文末，把附录表格当成文献条目
# 擦除重排：成品页数骤减、正文被拉丁字体重绘成「·」，而覆盖率 / 注入命中率 / 标签校验
# 全部照绿——没有任何自检能看出来。故边界必须同时依赖与语言无关的结构特征：
# 行首的条目编号、行首的表图标题、以及「整页数字/符号汤」的表格正文特征。
ENTRY_MARKER_LINE_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.(?:\s|$))")
CAPTION_LINE_RE = re.compile(
    r"^\s*(?:Table|Tables|Figure|Figs?\.?|Algorithm|Listing|Scheme)\s*\d+", re.I)


def content_lines(page):
    """版心内的文本行 [(y0, text)]，按 y 升序；跳过页眉/页脚带。"""
    lines = []
    page_h = page.rect.height
    for b in page.get_text("dict").get("blocks", []):
        for ln in b.get("lines", []):
            sps = [s for s in ln.get("spans", []) if s["text"].strip()]
            if not sps:
                continue
            y0 = min(s["bbox"][1] for s in sps)
            y1 = max(s["bbox"][3] for s in sps)
            if y0 < 45 or y1 > page_h - 40:
                continue
            txt = " ".join(s["text"] for s in sps).strip()
            if txt:
                lines.append((y0, txt))
    lines.sort(key=lambda x: x[0])
    return lines


def looks_like_table_body(lines):
    """整页以数字/符号为主（几无 3 字母以上的单词）→ 表格正文页，不是文献续页。"""
    if len(lines) < 5:
        return False
    plain = sum(1 for _, t in lines if not re.search(r"[A-Za-z]{3,}", t))
    return plain / len(lines) >= 0.6


def structural_ref_end(doc, title_pno):
    """按结构特征判定文献区终点，返回 (end_pno, cut_y)。

    与语言、章节标题无关，逐页前视：
      1. 后续页**没有行首条目编号**，且出现表图标题（或整页数字汤）→ 附录自该页起，
         文献区到上一页为止；
      2. 若同页内表图标题位于最后一个条目编号**下方** → 附录与文献同页，返回 cut_y
         作为分界（擦除与重绘都夹紧到该 y，附录部分原样保留）；
      3. 既无编号也无表图特征 → 可能只是某个长条目的续页，看下一页是否还有编号。

    返回 cut_y = None 表示分界落在页边界上。
    """
    for pno in range(title_pno + 1, doc.page_count):
        lines = content_lines(doc[pno])
        if not lines:
            continue
        markers, caption_y = [], None
        for y0, t in lines:
            if ENTRY_MARKER_LINE_RE.match(t):
                markers.append(y0)
            elif caption_y is None and CAPTION_LINE_RE.match(t):
                caption_y = y0
        if not markers:
            if caption_y is not None or looks_like_table_body(lines):
                return pno - 1, None
            if pno + 1 < doc.page_count:
                nxt = content_lines(doc[pno + 1])
                if any(ENTRY_MARKER_LINE_RE.match(t) for _, t in nxt):
                    continue  # 本页只是续页
            return pno - 1, None
        if caption_y is not None and caption_y > markers[-1]:
            return pno, caption_y
    return doc.page_count - 1, None


def find_title(doc):
    for pno in range(doc.page_count):
        d = doc[pno].get_text("dict")
        for b in d["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    if sp["text"].strip() in TITLE_CANDIDATES:
                        return pno, sp
    return None, None


def _cjk_block_cut(doc, title_pno, title_bbox):
    """【本地补丁】定位参考文献区里第一处「中文块」（含 CJK 的正文行），返回 (pno, y0)。

    本流水线的参考文献默认保持英文（--translate-refs 关闭），因此**译文里的中文块不可能
    是文献表**——它出现在文献之后时（MDPI 稿常见的「免责声明/出版者注：」等尾注），
    边界必须刹在它之前：否则该段会被 cell 整块擦除、又不会被当成文献条目重画，静默丢内容
    （实测 bioengineering-12-00311：末页「免责声明/出版者注」整段丢失，覆盖率/注入命中率
    均正常，肉眼不逐页看根本发现不了）。

    两道前提（否则中文不是异常信号，直接返回 None，保持原行为）：
      · 只有**文献表本身是拉丁文**时才启用。先看文献第一条是否含 CJK：若文献表自身就是
        中文（`--translate-refs` 模式），中文遍布全文，本判据无意义，返回 None。
      · 标题页上位于标题及其上方的行、页眉/页脚带不参与判定（标题本身是「参考文献」，
        上方是正文）。
    """
    tx0, ty0, tx1, ty1 = title_bbox
    seen_first_ref_line = False
    for pno in range(title_pno, doc.page_count):
        page = doc[pno]
        cbot = page.rect.height - BOTTOM_MARGIN
        ctop = page_content_top(doc, pno)
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                sps = [s for s in ln.get("spans", []) if s["text"].strip()]
                if not sps:
                    continue
                ly0 = min(s["bbox"][1] for s in sps)
                ly1 = max(s["bbox"][3] for s in sps)
                if ly1 <= ctop + 0.5 or ly0 >= cbot:
                    continue  # 运行页眉 / 页脚，不属于文献区
                if pno == title_pno and ly1 <= ty1 + 1:
                    continue  # 标题本身及其上方正文
                txt = "".join(s["text"] for s in sps)
                if not seen_first_ref_line:
                    seen_first_ref_line = True
                    if re.search(r"[\u4e00-\u9fff]", txt):
                        return None  # 文献表本身就是中文 → 中文非异常信号
                if re.search(r"[\u4e00-\u9fff]", txt):
                    return pno, ly0
    return None


def find_ref_end_pno(doc, title_pno, title_bbox, title_size, ref_size):
    """定位参考文献 section 的最后一页，返回 (end_pno, cut_y)。

    很多论文在参考文献之后还有 Supplementary Material / Appendix /
    Acknowledgements / Code / Results / 补充材料 等章节；若继续把后续页面
    当成参考文献处理，会把这些章节 wiping 掉并导致排版灾难。本函数扫描后续
    页面，遇到明显的大章节标题时返回其前一页索引。

    判定依据（而非单纯看字号，避免把运行页眉「10 Roy et al.」误判为章节）：
      · 字号接近参考文献大标题（≥ title_size * 0.8）；
      · 文本命中章节关键词（中英文均可）；
      · 且不是运行页眉（如 "10 Roy et al." / "MedNeXt: ... N"）。

    **与结构判定取更靠前的边界**：章节关键词只在附录带标题时有效；无标题附录
    （直接排 Table 2. 表格）由 structural_ref_end 用条目编号/表图标题 / 数字汤
    识别。两者取 min，任一命中即可刹住边界，避免把附录擦除重排。
    """
    if title_pno is None or title_pno >= doc.page_count - 1:
        return doc.page_count - 1, None

    s_end, s_cut = structural_ref_end(doc, title_pno)
    head_end = doc.page_count - 1

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

    for pno in range(title_pno + 1, min(s_end, doc.page_count - 1) + 1):
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
                        head_end = pno - 1
                        break
                else:
                    continue
                break
            else:
                continue
            break
        if head_end != doc.page_count - 1:
            break

    end = min(head_end, s_end)
    cut_y = s_cut if (end == s_end and s_cut is not None) else None
    _how = "章节标题" if head_end < s_end else ("表图标题分界" if cut_y is not None else "条目编号序列")

    # 【本地补丁】中文块兜底分界：文献之后直接排中文尾注（关键词表未必覆盖）时，
    # 用「出现 CJK」这一与语言无关的结构判据刹住边界，避免该段被擦除且不重画。
    _cjk = _cjk_block_cut(doc, title_pno, title_bbox)
    if _cjk is not None:
        _p, _y = _cjk
        if _p < end or (_p == end and (cut_y is None or _y < cut_y)):
            end, cut_y = _p, _y
            _how = "中文块分界"

    if end < doc.page_count - 1 or cut_y is not None:
        print(f"[边界] 文献区判定到第 {end + 1} 页"
              + (f" 的 y={cut_y:.0f} 处" if cut_y is not None else "")
              + f"（依据：{_how}）")
    return end, cut_y


def page_lines(page):
    """该页的文本行几何 (x0, y0, x1, y1)。行 = 同一基线上的若干 span 合并。"""
    lines = []
    for b in page.get_text("dict").get("blocks", []):
        for ln in b.get("lines", []):
            sps = [s for s in ln.get("spans", []) if s["text"].strip()]
            if not sps:
                continue
            lines.append((
                min(s["bbox"][0] for s in sps), min(s["bbox"][1] for s in sps),
                max(s["bbox"][2] for s in sps), max(s["bbox"][3] for s in sps),
            ))
    return lines


def page_rows(page):
    """把垂直重叠的行合并成「视觉行」。

    同一视觉行里的文字常被拆成多个 line（正文与页码基线差几 pt，如页眉
    "标题… 11" 会拆成两个 line，y 区间互相重叠）。按 line 直接比间隔会把它们
    当成两行、间隔算成负数，页眉识别随之失效。
    """
    rows = []
    for l in sorted(page_lines(page), key=lambda l: l[1]):
        if rows and l[1] <= rows[-1][3] + 2.0:
            rows[-1] = (min(rows[-1][0], l[0]), rows[-1][1],
                        max(rows[-1][2], l[2]), max(rows[-1][3], l[3]))
        else:
            rows.append(l)
    return rows


def content_top_from_rows(rows, page_h):
    """由视觉行推算正文起始 y（跳过运行页眉）。"""
    if not rows:
        return TOP_MARGIN
    y0, y1 = rows[0][1], rows[0][3]
    if len(rows) > 1 and y1 < page_h * 0.17 and rows[1][1] - y1 >= HEADER_GAP_PT:
        return max(TOP_MARGIN, rows[1][1] - 2)
    return max(TOP_MARGIN, y0 - 2)


def page_content_top(doc, pno):
    """该页正文起始 y（不含运行页眉）。

    页眉（如 "Title Suppressed Due to Excessive Length  11" / "12 J. Chen et al."）
    位于页顶且与下文有明显间隔。若把重绘/擦除区从固定的 TOP_MARGIN 起算，会连同
    页眉一起擦掉——参考文献页的页眉会整体消失。这里识别出页眉并从其下方起算。
    """
    page = doc[pno]
    return content_top_from_rows(page_rows(page), page.rect.height)


def detect_page_columns(page):
    """按版心中段的「垂直空带」判断单页栏数，返回 1 / 2 / None（信息不足）。

    思路：双栏版式必然存在一条纵贯版心的中缝——在那里几乎没有正文行穿过
    （只有通栏标题/通栏图这类跨栏元素穿过）。单栏版式不存在这样的空带。
    这比「按 span 的 x0 聚类找峰」稳得多：悬挂缩进、短末行、数学片段、图表内标签
    都会污染 x0 分布，而「有没有一条贯穿版心的空带」是版式的直接几何特征。

    两道护栏：
      · 只统计「成行宽度」的行（≥ 0.40 × 版心宽）。图表内的短标签、表格单元格、
        公式碎片是零碎短行，会在版心中段切出假空隙，把带图文的单栏页误判成双栏。
      · 中缝阈值取得很低（GUTTER_RATIO）：真中缝的穿越率≈0，而单栏的图文页
        即便滤掉短行后仍有 0.7+ 的穿越率，两者相差一个数量级，不需要折中阈值。
    """
    lines = page_lines(page)
    if len(lines) < 8:
        return None
    x_lo = min(l[0] for l in lines)
    x_hi = max(l[2] for l in lines)
    width = x_hi - x_lo
    if width < 120:
        return None
    keep = [l for l in lines if (l[2] - l[0]) >= 0.40 * width]
    if len(keep) < 8:
        return None

    # 在版心中段搜索「穿过的行最少」的竖直位置 = 候选中缝
    best_g, best_ratio = None, 1.0
    g = x_lo + 0.25 * width
    while g <= x_lo + 0.75 * width:
        ratio = sum(1 for l in keep if l[0] < g < l[2]) / len(keep)
        if ratio < best_ratio:
            best_ratio, best_g = ratio, g
        g += 2.0
    if best_g is None or best_ratio > GUTTER_RATIO:
        return 1
    # 中缝两侧都必须有足量正文，才算真的两栏（避免把「单栏页里一处空隙」当栏数）
    left = [l for l in keep if l[2] <= best_g]
    right = [l for l in keep if l[0] >= best_g]
    need = 0.15 * len(keep)
    return 2 if len(left) >= need and len(right) >= need else 1


def detect_doc_columns(doc):
    """全文栏数：逐页判定后取多数，返回 (1 或 2, 说明字符串)。

    以全文版式作权威，而不是只看参考文献区的 span 聚类——参考文献区恰恰是
    x0 分布最乱的地方（编号缩进 + 悬挂缩进）。单栏论文的参考文献必须按单栏重排，
    双栏论文的参考文献必须按双栏重排，两者不能用同一个兜底动作。
    """
    votes = Counter()
    for pno in range(doc.page_count):
        v = detect_page_columns(doc[pno])
        if v is not None:
            votes[v] += 1
    total = sum(votes.values())
    if not total:
        return 1, "无有效页面，默认单栏"
    cols = 2 if votes[2] > total * 0.5 else 1
    return cols, f"逐页投票 单栏={votes[1]} 双栏={votes[2]}（共 {total} 页有效）"


def gather_ref_spans(doc, title_pno, title_bbox, end_pno=None, cut_y=None, single_col=False):
    """收集参考文献文本 span（跳过标题本身与页眉/页脚），用于几何探测与擦除定位。

    三类内容必须排除，否则会被擦掉却不重画（静默丢内容）：
      1. 标题本身（"参考文献"/"References"）；
      2. 运行页眉/页脚（页顶页眉带 + 页底页脚带）；
      3. 标题**上方**、且与标题同栏的正文（单栏论文的参考文献页常从正文末段/致谢
         直接接排，这段不是参考文献）。双栏论文中位于标题右侧栏、标题上方的文字
         才是参考文献（阅读顺序左栏→右栏），故按 x 判断，不按 y 一刀切。
    end_pno 为参考文献 section 的最后一页（含），避免把后续章节算进来。
    cut_y 为**末页内的分界 y**（附录与文献同页时），y 在该值以下的 span 属于附录，
    必须排除，否则会被擦除重排。"""
    tx0, ty0, tx1, ty1 = title_bbox
    margin = 3.0
    title_rect = fitz.Rect(tx0 - margin, ty0 - margin, tx1 + margin, ty1 + margin)
    spans = []
    last = doc.page_count - 1 if end_pno is None else end_pno
    for p in range(title_pno, last + 1):
        page = doc[p]
        ctop = page_content_top(doc, p)
        cbot = page.rect.height - BOTTOM_MARGIN
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                sps = [s for s in ln.get("spans", []) if s["text"].strip()]
                if not sps:
                    continue
                lx0 = min(s["bbox"][0] for s in sps)
                ly0 = min(s["bbox"][1] for s in sps)
                lx1 = max(s["bbox"][2] for s in sps)
                ly1 = max(s["bbox"][3] for s in sps)
                if ly1 <= ctop + 0.5 or ly0 >= cbot:
                    continue  # 运行页眉 / 页脚，不属于参考文献
                if cut_y is not None and p == last and ly0 >= cut_y - 1:
                    continue  # 末页分界以下 = 附录，不属于参考文献
                if p == title_pno:
                    if fitz.Rect(lx0, ly0, lx1, ly1).intersects(title_rect):
                        continue  # 跳过标题本身
                    # 标题**上方**的正文行（参考文献页常从正文末段/致谢直接接排）不属于
                    # 参考文献，必须排除，否则会被擦掉却不重画、静默丢内容。
                    # 单栏版式：按 y 一刀切 —— 标题之上的任何行都不可能是参考文献（阅读
                    #   顺序自上而下）。**不能只靠 x 判据**：当参考文献标题左对齐而正文整段
                    #   缩进时（lx0 远大于 tx1），"lx0 < tx1 + 4" 判否，整页正文会被收进
                    #   spans 擦除且不重画（实测 bioengineering-12-00311：正文缩进至 x=166
                    #   而标题 x1=84，Conclusion/Author Contributions/Funding 等整节静默丢失）。
                    #   判据取标题**底部**（ly1 <= ty1 + 1）而不是顶部：单栏里标题上方的任何行
                    #   其底边必然不高于标题底边，取底部可连"紧贴标题"的那行一起排除；
                    #   而标题下方的文献条目底边恒大于 ty1，不会被误排除（方向只会更保守）。
                    # 双栏版式：阅读顺序为左栏→右栏，标题上方**右栏**的文字才是参考文献，
                    #   故保留按 x 的判断，不能按 y 一刀切。
                    if single_col:
                        if ly1 <= ty1 + 1:
                            continue
                    elif ly1 <= ty1 + 1 and lx0 < tx1 + 4:
                        continue
                for sp in sps:
                    spans.append({
                        "pno": p,
                        "x0": sp["bbox"][0], "y0": sp["bbox"][1],
                        "x1": sp["bbox"][2], "y1": sp["bbox"][3],
                        "text": sp["text"].strip(), "size": sp["size"], "font": sp["font"],
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


def build_cells(doc, title_pno, title_bbox, title_size, cols, spans, end_pno=None, cut_y=None):
    """按阅读顺序（页序 × 栏序）构建单元格列表。
    标题页中：含标题的栏从标题下方开始，其余栏可从页顶开始。
    end_pno 为参考文献 section 的最后一页（含）。
    cut_y 为末页内的分界 y（附录与文献同页）：该页的擦除/重绘区上边界夹紧到分界处，
    分界以下的附录内容原样保留——否则会把附录一起擦掉再当文献重排。
    每栏底部取到该页 references span 的最大 y1（夹紧到页底 -4），
    使重绘区延伸到页脚，覆盖被精确擦除的全部原文，避免文字被擦掉却没重画而丢失。"""
    page_h = doc[title_pno].rect.height
    page_w = doc[title_pno].rect.width
    tx0, ty0, tx1, ty1 = title_bbox
    # 每页 references 最低行的底边，用于把重绘区向下延伸到页脚
    max_y1_by_page = {}
    for s in spans:
        max_y1_by_page[s["pno"]] = max(max_y1_by_page.get(s["pno"], 0.0), s["y1"])
    cells = []
    last = doc.page_count - 1 if end_pno is None else end_pno
    for p in range(title_pno, last + 1):
        # y_bottom 取整页 references 区域（覆盖双栏整页），不再限于 dominant span 最远 y1；
        # 否则右栏 references 在 dominant span 之外，会被漏擦、漏重画，导致字叠字。
        y_bottom = page_h - 4
        if cut_y is not None and p == last:
            y_bottom = min(y_bottom, cut_y - 2)  # 末页分界以下为附录，不擦不重排
        # y_top 下移到运行页眉之下，避免把页眉一起擦掉（参考文献页的页眉会整个消失）
        page_top = page_content_top(doc, p)
        for (cx0, cx1) in cols:
            if p == title_pno:
                # 该栏是否与标题水平相交？
                col_overlaps_title = not (cx1 < tx0 or cx0 > tx1)
                y_top = (ty0 + title_size * 1.6 + 4) if col_overlaps_title else page_top
            else:
                y_top = page_top
            cells.append((p, cx0, cx1, y_top, y_bottom))
    # 去重 cells：detect_columns 在 references 区域偶尔会返回完全相同的栏区间
    # （如本论文就是 cols=[(58.5,553.3),(58.5,553.3)] 两个一样），导致每页构造 2 个重复 cells，
    # refs_text 被抽 2 遍、split 时出现镜像重复；用 (p, cx0, cx1) 唯一键去重。
    _seen = set()
    _uniq_cells = []
    for _c in cells:
        _k = (_c[0], round(_c[1], 2), round(_c[2], 2))
        if _k in _seen:
            continue
        _seen.add(_k)
        _uniq_cells.append(_c)
    cells = _uniq_cells
    return cells, page_w


def extract_ref_text(doc, cells):
    """逐单元格 clip 抽取全文，按阅读顺序拼接。

    换行统一规范化成空格；但**行尾连字符不能跟着变成空格**——原文在行尾断开的
    "large-\\nscale"、"Fei-\\nFei" 若按空格拼接，重排换行位置后就变成行内的
    "large- scale"，看着像错字。行尾连字符直接与下一行相接。
    （跨行断开的另一形态 "Pro-\\nceedings"（连字符后是换行而非空格）同样在下方拼接。）"""
    parts = []
    for (p, cx0, cx1, y_top, y_bottom) in cells:
        clip = fitz.Rect(cx0 - 3, y_top - 3, cx1 + 3, y_bottom + 3)
        t = doc[p].get_text("text", clip=clip)
        t = re.sub(r"-\s*\n\s*", "-", t)   # 行尾连字符：与下一行直接相接
        t = " ".join(t.split())            # 其余换行 / 连续空白规范化成单空格
        parts.append(t)
    out = " ".join(parts)
    # "pp. 4055- 4064" / "pp. 248- 255" → 连接符两侧不留空格（页码区间本来就该贴紧）。
    # 这类空格是上游段落重排留下的，行内看着像错字。
    return re.sub(r"(?<=\d)\s*([-–—−])\s*(?=\d)", r"\1", out)


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


def detect_ref_style(refs_text, head=2000):
    """探测参考文献编号风格：'[N]'（如 [1] Author）或 'N.'（如 1. Author）。

    **只看文本开头**（前 head 字符）——文献表的第一条必然以编号起始，这是最可靠的判据。
    绝不能全文 `re.search(r"\\[\\d+\\]")`：正文/附录里的**引用标记**（"[2]"、"[1,28]"）
    会让一整篇 "N." 风格的文献表被判成 bracket 风格，接着 split_entries_text 会按这些
    引用标记切分，把**首条标记之前的全部条目静默丢弃**（实测一篇 38 条文献的论文里，
    35 条文献连同附录一起被丢，成品页数 14 → 10）。
    """
    head_text = mask_false_positives(refs_text[:head])
    s = head_text.lstrip()
    if re.match(r"\[\d{1,3}\]", s):
        return "bracket"
    if re.match(r"\d{1,3}\.[\s\u00a0]", s):
        return "dot"
    br = len(re.findall(r"\[\d{1,3}\]", head_text))
    dt = len(re.findall(r"(?<![\d.])\d{1,3}\.[\s\u00a0]", head_text))
    return "bracket" if br > dt else "dot"


# 页码区间的尾巴会被误判成条目起点：区间形如 "pp. 248-255." / "pp. 4055– 4064."
# （连字符/短横线后面可能还带一个空格），其中的 "255." / "4064." 不是条目编号。
PAGE_RANGE_BEFORE = re.compile(r"(?i)(pp?[.#]?|pages?)\s*\d{1,5}\s*[-–—−/]\s*$")


def split_entries_text(refs_text):
    """按 [N] 编号起止切分条目。在掩码副本上找边界（避开 'Fig. 3.' / 'doi:10.' 误判），
    再回原文字符串切片。同时支持 '[N]' 与 'N.' 两种编号风格。

    收集所有按位置排序的 [N] 编号起止（N >= 1）作为条目边界。
    不要求连续编号——双栏布局下右栏的 [10] [11] ... 即使左栏 [9] 之后跳到 [13]，
    也能逐一起作用条目起点；这避免了「expected 卡在某值后永远不涨」的 bug。
    """
    masked = mask_false_positives(refs_text)
    style = detect_ref_style(refs_text)
    # 编号必须是 **1–3 位**：`\d+` 无上界会把条目末尾的**年份**（"…, 2022. 5. U. Baid…"）
    # 当成编号 "2022"，每条文献都被多切一刀 —— 实测 41 条文献被切成 78 条。
    pat = r"\[(\d{1,3})\]" if style == "bracket" else r"(?<!\d)(\d{1,3})\. "
    positions, seq = [], []
    for m in re.finditer(pat, masked):
        n = int(m.group(1))
        if not (1 <= n <= 999):
            continue
        # 页码区间会被误判成条目起点："pp. 248-255." / "pp. 4055- 4064." 里的
        # "255." / "4064." 前紧挨着区间连接符，或整体处在 "pp. …-…" 语境里。
        # （实测一篇 20 条文献会多切出 7 条假条目，末条只剩 "11. Springer (2018)" 这种游离短行。）
        if m.start() > 0 and masked[m.start() - 1] in "-–—−/":
            continue
        if PAGE_RANGE_BEFORE.search(masked[:m.start()]):
            continue
        # 序号连续性：条目编号大致单调递增。前跳超过 20 的候选基本是年份/卷号/页码
        # （如 "vol. 2022"、"Fig. 2022"）而非新条目；只做前跳限制，避免误杀正常的
        # 编号推进（不设后跳限制，兼容双栏阅读顺序的小幅抖动）。
        if seq and n > seq[-1] + 20:
            continue
        positions.append(m.start())
        seq.append(n)
    # 去重并按位置排序（refs_text 偶尔因 cells 重复而含镜像副本）
    positions = sorted(set(positions))
    if not positions:
        return [refs_text.strip()]
    entries = []
    # 首个编号之前的文字：正常情况为空（文献表从编号起始）。非空说明编号风格判错、
    # 或首条编号被掩码规则误杀——这段**必须保留**，否则丢的是几十条文献（实测丢过 35 条）。
    head = refs_text[:positions[0]].strip()
    if len(head) > 20:
        print(f"[WARN] 首个编号之前仍有 {len(head)} 字符未被识别为条目起点，"
              f"已保留为独立条目：{head[:40]!r}")
        entries.append(head)
    for i in range(len(positions) - 1):
        entries.append(refs_text[positions[i]:positions[i + 1]].strip())
    entries.append(refs_text[positions[-1]:].strip())
    return entries


def validate_entries(entries):
    """重排前的最后一道闸门：提取结果必须像一份文献表，否则宁可不重排。

    重排是「先擦除原文、再重画」——提取或切分一旦出错就是**不可逆的静默丢内容**，
    而覆盖率、注入命中率、标签校验全都照绿（没有任何自检能看出来）。因此动笔擦除
    之前必须自检，通过不了就抛错放弃重排（上层会回退为不改版式的单语成品）。

    判据：条目数 >= 3，且含四位年份（19xx/20xx）的条目占比 >= 50%
    （文献条目几乎必带年份，附录表格 / 图注 / 正文碎片不具备这一特征）。
    """
    if len(entries) < 3:
        return False, f"仅切出 {len(entries)} 条"
    dated = sum(1 for e in entries if re.search(r"(?:19|20)\d{2}", e))
    if dated < max(2, int(len(entries) * 0.5)):
        return False, f"仅 {dated}/{len(entries)} 条含四位年份，不像文献表"
    return True, ""


# PyMuPDF 用内建 "helv"（base-14 Helvetica）写字时，**Latin-1 之外的字符会被静默替换成
# "·"**——参考文献里的页码区间 "248–255" 会变成 "248·255"，弯引号、省略号同样中招。
# 这里把常见排版字符归一化成 Latin-1 内可正常显示的等价字符。
# （归一化在排版之前完成，行宽计算据此进行，不会出现「按原串算宽、按新串绘制」的错位。）
CHAR_MAP = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u2033": '"',
    "\u2026": "...", "\u2044": "/", "\u00a0": " ",
}


def normalize_for_helv(t):
    """把 helv 显示不了、会被静默改写成「·」的字符映射掉。返回 (新文本, 被映射字符集合)。"""
    changed, out = set(), []
    for ch in t:
        if ch in CHAR_MAP:
            changed.add(ch)
            out.append(CHAR_MAP[ch])
        else:
            out.append(ch)
    return "".join(out), changed


# 行尾断词的连字符：原 PDF 里 "A large-\nscale"、"In: Pro-\nceedings" 这类断词，
# 重排换行后可能落到行中间，读起来像错字（"large- scale"、"Pro- ceedings"）。
# 处理办法：能证明是「断词」的（两半拼回去在全文词表里确实存在）→ 去掉连字符直接拼接；
# 证不出来的（可能本来就是 large-scale / fixed-point 这类真连字符）→ 保留连字符、只去掉多余空格，
# 这样最坏也只是「多一个连字符」，不会凭空造出拼写错误的词。
WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']{3,}")
SOFT_HYPHEN_RE = re.compile(r"([A-Za-z]{2,})-\s+([A-Za-z]{2,})")


def build_vocab(doc):
    """全文英文完整词表（用于判别断词连字符）。

    只收「不以连字符结尾」的词——那种是行尾断词残片，不可信；
    再把它去连字符的写法一并计入，便于直接比对。"""
    vocab = set()
    for p in range(doc.page_count):
        for tok in WORD_RE.findall(doc[p].get_text()):
            if not tok.endswith("-"):
                vocab.add(tok.lower())
    for t in list(vocab):
        if "-" in t:
            vocab.add(t.replace("-", ""))
    return vocab


def join_soft_hyphens(t, vocab):
    """把行尾断词拼回去（可证明为断词则去掉连字符，否则保留连字符并去掉多余空格）。"""
    def rep(m):
        a, b = m.group(1), m.group(2)
        return a + b if (a + b).lower() in vocab else a + "-" + b
    for _ in range(4):          # 迭代若干次，兼容 "state- of- the- art" 这类多连字符链
        new = SOFT_HYPHEN_RE.sub(rep, t)
        if new == t:
            break
        t = new
    return t


def is_blank_page(page, hdr_bottom):
    """页面是否「只剩页眉页脚」：正文带内无文字、无图片、且没有非白色图元。

    hdr_bottom 是**全文标定的页眉底线**（各页页眉 y1 的最大值）。必须在擦除/重绘之前算：
    重画出来的行基线由 insert_text 指定，其 bbox 顶部比正文行更靠上，用重绘后的页面
    去标定会把刚画的内容误判成页眉，进而把有内容的页当空页删掉。
    """
    if page.get_images():
        return False
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None or any(c < 0.999 for c in fill):
            return False
    h = page.rect.height
    for (x0, y0, x1, y1) in page_rows(page):
        if y1 <= hdr_bottom + 2 or y0 >= h - BOTTOM_MARGIN:
            continue  # 运行页眉 / 页脚，不算正文
        return False
    return True


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

    # 栏数必须在**收集 span 之前**判定：gather_ref_spans 排除「标题上方正文」的判据
    # 依赖「全文是单栏还是双栏」（单栏按 y 一刀切，双栏按 x 分左右栏）。
    doc_cols, doc_why = detect_doc_columns(doc)

    # 先估算参考文献正文字号，再判断参考文献 section 的边界
    tmp_spans = gather_ref_spans(doc, title_pno, title_bbox, single_col=(doc_cols == 1))
    ref_size = dominant_size(tmp_spans) if tmp_spans else title_size
    ref_end_pno, ref_cut_y = find_ref_end_pno(doc, title_pno, title_bbox, title_size, ref_size)
    post_doc = None
    if ref_end_pno < doc.page_count - 1:
        post_doc = fitz.open()
        post_doc.insert_pdf(doc, from_page=ref_end_pno + 1, to_page=doc.page_count - 1)
        doc.delete_pages(range(ref_end_pno + 1, doc.page_count))
        print(f"[边界] 参考文献到第 {ref_end_pno + 1} 页；后续 {post_doc.page_count} 页暂不处理，重排后恢复")

    spans = gather_ref_spans(doc, title_pno, title_bbox, ref_end_pno, ref_cut_y,
                             single_col=(doc_cols == 1))
    assert spans, "未收集到参考文献文本"

    # 全文页眉底线：必须在擦除/重绘**之前**标定（重画的行顶部更靠上，事后标定会失准），
    # 用于事后判断「尾页是不是只剩页眉」。
    _hdr = []
    for i in range(doc.page_count):
        _r = page_rows(doc[i])
        if (len(_r) > 1 and _r[0][3] < doc[i].rect.height * 0.17
                and _r[1][1] - _r[0][3] >= HEADER_GAP_PT):
            _hdr.append(_r[0][3])
    _hdr_bottom = max(_hdr) if _hdr else 0.0

    # 栏数裁决：以**全文版式**为权威，参考文献区自身的 span 聚类只用来取精确栏边界。
    # 参考文献区是全文里 x0 分布最乱的地方（编号缩进 + 悬挂缩进 + 短末行 + 数学片段），
    # 单栏论文在这里也会被聚类出「假第二峰」，反之真双栏也可能聚类失败；
    # 只有「版式本身是单栏还是双栏」才是可靠的判据。
    span_cols = detect_columns(spans)
    _uniq = {(round(c[0], 2), round(c[1], 2)) for c in span_cols}
    full_span = (min(s["x0"] for s in spans), max(s["x1"] for s in spans))

    if doc_cols == 1:
        cols = [full_span]
        if len(_uniq) >= 2:
            print(f"[栏] 参考文献区聚类出 {len(_uniq)} 栏，但全文版式为单栏 → 按单栏重排")
    elif len(_uniq) >= 2:
        cols = span_cols
    else:
        # 真双栏论文，但参考文献区聚类失败（x 聚类不锐、找不到第二峰）→ 按版心中缝拆双栏
        cx0, cx1 = full_span
        mid = (cx0 + cx1) / 2
        gap = 6.0  # 栏间距 6pt
        cols = [(cx0, mid - gap), (mid + gap, cx1)]
        print(f"[栏] 参考文献区聚类失败，全文版式为双栏 → 按中缝拆双栏 {cols}")
    size = dominant_size(spans)
    font = fitz.Font("helv")
    print(f"[栏] 全文版式判定 {doc_cols} 栏（{doc_why}）→ 采用 {len(cols)} 栏: {cols}")
    print(f"[字号] 参考文献正文 {size:.1f}pt")

    cells, page_w = build_cells(doc, title_pno, title_bbox, title_size, cols, spans,
                                ref_end_pno, ref_cut_y)
    vocab = build_vocab(doc)
    refs_text = extract_ref_text(doc, cells)

    # 【本地补丁·安全闸门】若抽出来的文本里混着成片 CJK、而拉丁字母仍占绝对多数，
    # 说明**正文被误当成文献**收进了 cells（默认模式下参考文献保持英文）。此时擦除会
    # 静默丢正文，而覆盖率 / 注入命中率都看不出来。宁可放弃重排，也不能丢内容。
    # 「拉丁 ≥ 2×中文」这一半条件把 `--translate-refs` 的纯中文文献模式排除在外
    # ——那种模式下中文是正常内容，不该拦（否则该模式的重排功能会被整体禁用）。
    _cjk = len(re.findall(r"[\u4e00-\u9fff]", refs_text))
    _latin = len(re.findall(r"[A-Za-z]", refs_text))
    if _cjk > 20 and _latin >= 2 * _cjk:
        doc.close()
        raise RuntimeError(
            f"参考文献区抽取出 {_cjk} 个中文字符（拉丁字母 {_latin} 个，疑为正文被误纳入）"
            "→ 放弃悬挂缩进重排，保持原版式（未擦除任何内容）")
    _soft = len(SOFT_HYPHEN_RE.findall(refs_text))
    if _soft:
        refs_text = join_soft_hyphens(refs_text, vocab)
        print(f"[连字符] 行尾断词 {_soft} 处：可证实为断词的已拼回单词，其余保留连字符、去掉多余空格")
    refs_text, mapped = normalize_for_helv(refs_text)
    if mapped:
        print("[字形] 内建 helv 显示不了以下字符（会被静默替换成「·」），已归一化："
              + " ".join(f"{c}(U+{ord(c):04X})" for c in sorted(mapped)))
    entries = split_entries_text(refs_text)
    print(f"[切分] 共 {len(entries)} 条；首条={entries[0][:35]!r} 末条={entries[-1][:35]!r}")

    # 最后一道闸门：擦除前先确认提取结果确实像文献表，否则放弃重排。
    # 重排是「先擦后画」，出错即不可逆丢内容，而覆盖率 / 注入命中率都看不出来。
    ok, why = validate_entries(entries)
    if not ok:
        doc.close()
        raise RuntimeError(
            f"提取结果不像文献表（{why}）→ 放弃悬挂缩进重排，保持原版式（未擦除任何内容）")

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

    # 双重精确擦除（cells 整块 + spans 逐个），确保原参考文献文字 100% 清除：
    #   1) 标题行（白底整行）；
    #   2) 每栏 references 区按 cell 整块白底 redact —— 兜住所有非 char-level / 非 dominant 字号 / vector 装饰等 pymupdf `get_text("dict")` 拿不到的图元；
    #   3) 逐 span 精确 redact —— 兜住跨栏 gutter 残留、cell 边界外偏移字符。
    # 三层叠加可彻底消除「字叠字」重影，不再依赖单一层级的覆盖完整度。
    ty = title_bbox[1]
    doc[title_pno].add_redact_annot(
        fitz.Rect(0, ty - 3, page_w, ty + title_size * 1.6 + 4), fill=(1, 1, 1))
    for (p, cx0, cx1, y_top, y_bottom) in cells:
        r = fitz.Rect(cx0 - 2, y_top - 2, cx1 + 2, y_bottom + 2).intersect(doc[p].rect)
        if r.is_empty:
            continue
        doc[p].add_redact_annot(r, fill=(1, 1, 1))
    for s in spans:
        page = doc[s["pno"]]
        r = fitz.Rect(s["x0"] - 1, s["y0"] - 1, s["x1"] + 1, s["y1"] + 1).intersect(page.rect)
        if r.is_empty:
            continue
        page.add_redact_annot(r, fill=(1, 1, 1))
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
    used_pages = {title_pno}
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
            used_pages.add(pno)
            doc[pno].insert_text((x, y), line, fontname="helv", fontsize=size, color=(0, 0, 0))
            y += line_h

    # 重排会把参考文献排得更紧凑，原参考文献尾部所在的页可能整页空出来
    # （原文已被擦除，重画的内容又全放进了前面的页）→ 末尾会多出一张「只有页眉」的白页，删掉。
    # 双保险：① 本轮没往该页画过任何内容；② 该页正文带内确实什么都没有。
    #
    # **后面还有附录/补充材料时不能删**：post_doc 里的页是**原样搬运**过来的，页眉上带着
    # 原文档的页码。抽掉中间这整页会让附录页码与实际页序错开（如正文到 10 页，附录却从 12 页
    # 开始），读者会以为丢了内容。宁可留一张白页，也要保住页码连贯。
    if post_doc is None:
        while (doc.page_count - 1 > title_pno
               and (doc.page_count - 1) not in used_pages
               and is_blank_page(doc[doc.page_count - 1], _hdr_bottom)):
            doc.delete_page(doc.page_count - 1)
            print(f"[收尾] 删除重排后空出的尾页（原第 {doc.page_count + 1} 页）")

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
