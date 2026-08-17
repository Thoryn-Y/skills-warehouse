#!/usr/bin/env python3
"""版面标签规则：哪些段落整体不翻译。

设计铁律
-------------------------------------------------
**图和表「本身」一律不翻译**。这里的「本身」是完整概念，包括但不限于：

- 图：坐标轴标签、刻度数字、图例、图内注记、子图编号（a)/(b)）、箭头旁的说明文字
- 表：表头、行标题、列标题、单元格内容、表内单位标注

**只有「注」才翻译**：图注（Figure N: ...）、表注（Table N: ...）、表脚注、公式说明。

原因：图表内文字与像素/矢量图形是同一套坐标系里的东西，替换成中文会导致
字宽变化 → 覆盖曲线、撑破单元格、与坐标轴错位；而且学术图表里的
axis label / legend 本就是国际通用记法，翻译反而降低可读性。

标签取值来源
-------------------------------------------------
babeldoc 版面分析（doclayout YOLO + hybrid 标签），完整清单见
``babeldoc/format/pdf/document_il/utils/layout_helper.py`` 的
``get_character_layout()`` 的 ``layout_priority`` 列表与 ``is_text_layout()``。

这两个集合互斥，改动时务必成对检查。
"""

from __future__ import annotations

__all__ = [
    "NO_TRANSLATE_LAYOUT_LABELS",
    "CAPTION_LAYOUT_LABELS",
    "is_no_translate_layout",
]


#: 整体不翻译 —— 命中即原样透传
NO_TRANSLATE_LAYOUT_LABELS = frozenset(
    {
        # —— 图本体及图内一切文字（坐标轴/图例/刻度/注记）——
        "figure",
        "image",
        "chart",
        "figure_text",
        "figure_text_hybrid",
        # —— 表本体及表内一切文字（表头/单元格）——
        "table",
        "table_text",
        "table_cell",
        "table_cell_hybrid",
        "wired_table_cell",
        "wireless_table_cell",
        "form_or_table_hybrid",
        # —— 公式本体 ——
        "formula",
        "isolate_formula",
        "formula_hybrid",
        # —— 版面附属物：页眉页脚/页码/行号/印章 ——
        "header",
        "footer",
        "page_header",
        "page_footer",
        "page_header_hybrid",
        "page_footer_hybrid",
        "page_number_hybrid",
        "line_number_hybrid",
        "seal",
    }
)

#: 必须翻译的「注」—— 与上面互斥，仅作自检与文档说明
CAPTION_LAYOUT_LABELS = frozenset(
    {
        "figure_caption",
        "figure_title",
        "chart_title",
        "table_caption",
        "table_title",
        "table_footnote",
        "formula_caption",
        "caption",
        "caption_hybrid",
    }
)

# 自检：两个集合绝不能有交集，否则规则自相矛盾
assert not (NO_TRANSLATE_LAYOUT_LABELS & CAPTION_LAYOUT_LABELS), (
    "NO_TRANSLATE 与 CAPTION 标签集合出现交集: "
    f"{sorted(NO_TRANSLATE_LAYOUT_LABELS & CAPTION_LAYOUT_LABELS)}"
)


def is_no_translate_layout(layout_label: str | None) -> bool:
    """该版面标签是否属于「整体不翻译」。"""
    if not layout_label:
        return False
    return layout_label.strip().lower() in NO_TRANSLATE_LAYOUT_LABELS


def is_caption_layout(layout_label: str | None) -> bool:
    """该版面标签是否是「注」（图注/表注/表脚注），必须翻译。"""
    if not layout_label:
        return False
    return layout_label.strip().lower() in CAPTION_LAYOUT_LABELS


# ---------------------------------------------------------------------------
# 几何兜底：仅靠 layout_label 不够
# ---------------------------------------------------------------------------
# 实测发现（MedNeXt 论文，189 段）：doclayout 模型对不少论文给不出精细分类，
# 189 段里有 121 段是 ``fallback_line``，图内的坐标轴标签、图例与正文混在一起，
# 光看标签根本分不出来。因此必须再加一道**几何判据**：
#   段落框有 ≥ OVERLAP_THRESHOLD 的面积落在 figure/table 版面框内 → 判为图表内部文字。
#
# 图注/表注通常排在图表框的**外部**（上方或下方），重叠比例很低，不会被误伤；
# 且带 caption 类标签的段落会无条件豁免。

#: 页面版面框里代表「图/表」区域的 class_name
FIGURE_TABLE_LAYOUT_CLASSES = frozenset(
    {
        "figure",
        "image",
        "chart",
        "table",
        "form_or_table_hybrid",
    }
)

#: 段落框落在图表框内的面积比例阈值，超过即判为图表内部文字
OVERLAP_THRESHOLD = 0.55


def box_overlap_ratio(inner, outer) -> float:
    """``inner`` 框有多大比例的面积落在 ``outer`` 框内（0.0–1.0）。

    两个参数都是 babeldoc 的 ``Box``（含 x / y / x2 / y2）。
    """
    if inner is None or outer is None:
        return 0.0
    try:
        ix, iy = max(inner.x, outer.x), max(inner.y, outer.y)
        ax, ay = min(inner.x2, outer.x2), min(inner.y2, outer.y2)
    except (AttributeError, TypeError):
        return 0.0
    if ax <= ix or ay <= iy:
        return 0.0
    inter = (ax - ix) * (ay - iy)
    area = (inner.x2 - inner.x) * (inner.y2 - inner.y)
    return inter / area if area > 0 else 0.0


def figure_table_boxes(page) -> list:
    """取出页面上所有 figure/table 版面框。"""
    boxes = []
    for layout in getattr(page, "page_layout", None) or []:
        name = (getattr(layout, "class_name", None) or "").strip().lower()
        box = getattr(layout, "box", None)
        if name in FIGURE_TABLE_LAYOUT_CLASSES and box is not None:
            boxes.append(box)
    return boxes


def is_inside_figure_table(paragraph, boxes, threshold: float = OVERLAP_THRESHOLD) -> bool:
    """段落是否落在图/表区域内（→ 整体不翻译）。

    caption 类标签无条件豁免，避免把图注/表注误判进去。
    """
    if not boxes:
        return False
    if is_caption_layout(getattr(paragraph, "layout_label", None)):
        return False
    pbox = getattr(paragraph, "box", None)
    if pbox is None:
        return False
    return any(box_overlap_ratio(pbox, b) >= threshold for b in boxes)
