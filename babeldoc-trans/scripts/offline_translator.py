#!/usr/bin/env python3
"""babeldoc 零外部 API 翻译注入器。

机制（已读 babeldoc 0.6.4 源码确认并实测）
-------------------------------------------------
1. ``high_level.translator_supports_llm()`` 只检查 ``hasattr(translator, "do_llm_translate")``；
   ``BaseTranslator`` 必然带该方法，因此 **任何 BaseTranslator 子类都会走 ILTranslatorLLMOnly**。
2. ``ILTranslatorLLMOnly.translate_paragraph()`` 调用 ``translate_engine.llm_translate(final_input, ...)``。
3. ``BaseTranslator.llm_translate()`` 先查缓存，未命中才调 ``do_llm_translate(final_input, ...)``。
   —— 这是唯一注入点，无需修改 babeldoc 源码，也不经过 CLI 的 ``--openai``。

``final_input`` 是完整 LLM prompt，结尾固定为::

    ## Here is the input:

    [{"id": 0, "input": "<段落内部文本(含 {vN}/<style> 占位符)>", "layout_label": "text"}, ...]

返回契约：裸 JSON 字符串 ``[{"id": 0, "output": "<译文>"}, ...]``，
必须与输入 **等长**（否则 il_translator_llm_only 会抛异常）。

两遍流水线
-------------------------------------------------
- ``mode="collect"``：不翻译，只把每个 ``input`` 记录到 ``self.collected``，原样返回。
- ``mode="lookup"``：按 ``input`` 查 Agent 译文表，返回译文；查不到则退化为原文。

全程不发起任何外部 API 请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout_rules import figure_table_boxes  # noqa: E402
from layout_rules import is_inside_figure_table  # noqa: E402
from layout_rules import is_no_translate_layout  # noqa: E402

from babeldoc.format.pdf.high_level import async_translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.translator.translator import BaseTranslator
from babeldoc.translator.translator import set_translate_rate_limiter

MARKER = "## Here is the input:"

# babeldoc 富文本占位符：<bN>（开）/ </bN>（闭）。N 为 babeldoc 分配的样式 id。
_LEFT_TAG = re.compile(r"<b(\d+)>")
_RIGHT_TAG = re.compile(r"</b(\d+)>")


def _clean_rich_text(text: str) -> str:
    """保留参考文献编号占位符，剔除其余 <bN> 标签。

    判定规则（基于 nnWNet 等论文实测）：

    * 参考文献编号 = 连续、未闭合的 ``<b1><b2>…<bN>``（N 即条目号，
      babeldoc 抽取时把数字本身丢了，只留下 <bN> 标记）。这类必须保留，
      否则译文参考文献会整体丢失 [1][2]… 编号。
    * 正文粗体 = ``<b1>…</b1>`` 成对的闭合标签，删除即可（粗体非必需）。
    * 正文孤立未闭合标签（如 ``…benchmark <b1>.``）= babeldoc 抽取失败，
      会渲染成字面量 ``<b1>``，必须删除。

    因此：仅当某个 <bN> 既「无对应 </bN>」又「与另外 ≥1 个无闭合标签构成
    连续整数序列（如 1,2,3…）」时才保留；其余一律剔除。
    """
    lefts = [(m.start(), int(m.group(1))) for m in _LEFT_TAG.finditer(text)]
    if not lefts:
        return text
    right_ids = {int(m.group(1)) for m in _RIGHT_TAG.finditer(text)}
    unmatched = [n for (_, n) in lefts if n not in right_ids]
    keep: set[int] = set()
    if len(unmatched) >= 2:
        nums = sorted(unmatched)
        best = run = [nums[0]]
        for x in nums[1:]:
            if x == run[-1] + 1:
                run.append(x)
            else:
                if len(run) > len(best):
                    best = run
                run = [x]
        if len(run) > len(best):
            best = run
        if len(best) >= 2:
            keep = set(best)

    def _repl(m):
        return m.group(0) if int(m.group(1)) in keep else ""

    return _LEFT_TAG.sub(_repl, _RIGHT_TAG.sub(_repl, text))


__all__ = [
    "OfflineTranslator",
    "run_babeldoc_offline",
    "MARKER",
    "_clean_rich_text",
    "OVERLAP_LIFT_SKIPPED",
    "install_figure_table_guard",
    "install_typesetting_overlap_guard",
]


class _NullCache:
    """替代 TranslationCache：本地注入无需持久化缓存，且避免未初始化属性。"""

    def get(self, *_a, **_k):
        return None

    def set(self, *_a, **_k):
        return None

    def add_params(self, *_a, **_k):
        return None


class OfflineTranslator(BaseTranslator):
    """本地注入式翻译器：不调用任何外部 API。"""

    name = "offline"

    def __init__(self, mode: str = "collect", translations: dict | None = None):
        # 刻意不调用 BaseTranslator.__init__ —— 它会初始化 OpenAI 客户端与 SQLite 翻译缓存。
        # 这里手动设置调用链实际用到的属性即可。
        self.lang_in = "en"
        self.lang_out = "zh"
        self.ignore_cache = True          # 绕过 SQLite 翻译缓存，直达 do_llm_translate
        self.cache = _NullCache()         # 兜底：fallback 路径可能触碰 self.cache
        self.translate_call_count = 0
        self.translate_cache_call_count = 0

        if mode not in ("collect", "lookup"):
            raise ValueError(f"unknown mode: {mode}")
        self.mode = mode
        self.translations: dict[str, str] = translations or {}

        self.collected: list[dict] = []   # collect 模式下累积的源文本
        self.hit = 0                      # lookup 模式：命中译文表的条数
        self.miss = 0                     # lookup 模式：未命中（退化为原文）的条数
        self.skipped_layout = 0           # lookup 模式：因图/表/公式版面标签而跳过的条数
        self.missed_texts: list[str] = []
        self._lock = threading.Lock()

    # ---- babeldoc 调用入口 ----------------------------------------------

    def do_translate(self, text, rate_limit_params=None):
        """非 LLM 路径（fallback）。与 do_llm_translate 共用同一译文表。"""
        if self.mode == "collect":
            with self._lock:
                self.collected.append({"input": text, "layout_label": "_plain"})
            return text
        return self.translations.get(text, text)

    def do_llm_translate(self, final_input, rate_limit_params=None):
        # ILTranslatorLLMOnly.__init__ 会用 None 探测是否支持 LLM 模式，必须放行
        if final_input is None:
            return None

        items = self._extract(final_input)
        out = []
        for it in items:
            inp = it.get("input")
            if inp is None:
                continue
            label = it.get("layout_label")
            if self.mode == "collect":
                with self._lock:
                    self.collected.append({"input": inp, "layout_label": label})
                out.append({"id": it.get("id"), "output": inp})
            elif is_no_translate_layout(label):
                # 硬闸门：图/表/公式本体及其内部文字（坐标轴标签、图例、表头、
                # 单元格……）一律原样返回，绝不注入译文 —— 即便译文表里恰好
                # 存了同名文本的中文。图注/表注不在该集合内，不受影响。
                with self._lock:
                    self.skipped_layout += 1
                out.append({"id": it.get("id"), "output": inp})
            else:
                tgt = self.translations.get(inp)
                with self._lock:
                    if tgt is None:
                        self.miss += 1
                        if len(self.missed_texts) < 50:
                            self.missed_texts.append(inp)
                    else:
                        self.hit += 1
                out_text = tgt if tgt is not None else inp
                # babeldoc 内部富文本占位符处理（关键，不能一刀切）：
                #   * 参考文献编号以「连续、未配对的 <b1><b2>…<bN>」形式编码，
                #     必须保留 —— 删掉它们会导致译文参考文献丢失 [1][2]… 编号。
                #   * 正文里的粗体样式是「成对的 <b1>…</b1>」，用户未要求保留，
                #     且成对的标签本就会渲染成正常粗体（不会成字面量）。
                #   * 正文里偶发的「孤立未闭合 <b1>」（如 "...benchmark <b1>."）
                #     是 babeldoc 抽取失败的产物，会渲染成字面量 <b1>，必须删。
                # 策略：只保留「连续且未闭合」的 <bN> 序列（参考文献编号），其余全删。
                out_text = _clean_rich_text(out_text)
                out.append({"id": it.get("id"), "output": out_text})
        return json.dumps(out, ensure_ascii=False)

    # ---- 工具 -------------------------------------------------------------

    @staticmethod
    def _extract(final_input: str) -> list:
        """从完整 prompt 尾部抽出待译 JSON 数组。"""
        if not final_input:
            return []
        idx = final_input.rfind(MARKER)
        if idx < 0:
            return []
        payload = final_input[idx + len(MARKER):].strip()
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.lower().startswith("json"):
                payload = payload[4:]
        try:
            data = json.loads(payload)
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def unique_collected(self) -> list[dict]:
        """collect 模式：按首次出现顺序去重。"""
        seen: set[str] = set()
        uniq: list[dict] = []
        for c in self.collected:
            key = c["input"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        return uniq


# ---- 图/表整体不翻译：几何闸门 -------------------------------------------

#: 记录被几何闸门拦下的段落（诊断用）：[(page_no, layout_label, text_preview), ...]
FIGURE_TABLE_SKIPPED: list[tuple] = []

_guard_installed = False


def install_figure_table_guard(verbose: bool = False) -> None:
    """让 babeldoc 跳过落在 figure/table 区域内的段落。

    为什么必须打这个补丁
    ------------------------------------------------------------------
    「图/表本身不翻译」不能只靠 ``layout_label``：实测 MedNeXt 论文 189 段里
    有 121 段被 doclayout 标成 ``fallback_line``，架构图里的 ``Stem Block``、
    ``Down Block``、``inc1,``、``DW, GNorm`` 与正文共用同一个标签，光看标签分不出来。

    补丁挂在哪
    ------------------------------------------------------------------
    babeldoc 有两条选段路径，**必须都堵上**：

    1. ``process_page``  —— 主路径，直接遍历 ``page.pdf_paragraph``，
       不经过 ``_filter_paragraphs``（这是最初 patch 错入口的坑）。
       它内部有一句 ``if id(paragraph) in translated_ids: continue``，
       因此只要在调用前把要跳过的段落 id 预先塞进 ``translated_ids``，
       就能干净地让它略过，无需复制其内部逻辑。
    2. ``_filter_paragraphs`` —— 跨页段落合并路径。

    判据：段落框有 ≥55% 面积落在 figure/table 版面框内 → 判为图表内部文字。
    babeldoc 会原样保留这些文字，既不翻译也不重排。
    图注/表注排在图表框外部（实测重叠仅约 7%），且 caption 类标签无条件豁免。
    """
    global _guard_installed
    if _guard_installed:
        return

    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
        ILTranslatorLLMOnly,
    )

    def _mark_skipped(page, translated_ids: set) -> None:
        """把该页图表内部段落预先标记为『已翻译』，使 babeldoc 略过它们。"""
        boxes = figure_table_boxes(page)
        if not boxes:
            return
        for para in getattr(page, "pdf_paragraph", None) or []:
            if id(para) in translated_ids:
                continue
            if not is_inside_figure_table(para, boxes):
                continue
            translated_ids.add(id(para))
            text = (getattr(para, "unicode", None) or "").strip()
            FIGURE_TABLE_SKIPPED.append(
                (
                    getattr(page, "page_number", None),
                    getattr(para, "layout_label", None),
                    text[:80],
                )
            )
            if verbose and text:
                print(f"    [图表内·跳过] {text[:60]}", flush=True)

    # ---- 路径 1：主路径 process_page ----
    orig_process_page = ILTranslatorLLMOnly.process_page

    def patched_process_page(
        self, page, executor, pbar=None, tracker=None, executor2=None, translated_ids=None
    ):
        if translated_ids is None:
            translated_ids = set()
        _mark_skipped(page, translated_ids)
        return orig_process_page(
            self, page, executor, pbar, tracker, executor2, translated_ids
        )

    ILTranslatorLLMOnly.process_page = patched_process_page

    # ---- 路径 2：跨页段落合并 ----
    orig_filter = ILTranslatorLLMOnly._filter_paragraphs

    def patched_filter(self, page, translated_ids=None, require_body_text=False):
        paragraphs = orig_filter(self, page, translated_ids, require_body_text)
        boxes = figure_table_boxes(page)
        if not boxes:
            return paragraphs
        return [p for p in paragraphs if not is_inside_figure_table(p, boxes)]

    ILTranslatorLLMOnly._filter_paragraphs = patched_filter
    _guard_installed = True


# ---- 「防重叠抬升」守卫：防版框被压扁导致正文字号塌陷 --------------------

#: 被守卫拦下的抬升次数（诊断用，`translate.py` 会把末值报进 [INJECT] / [DONE]）
OVERLAP_LIFT_SKIPPED: list[int] = [0]

#: 抬升后至少要保留原版框高度的比例；低于此值判为「恶性抬升」并跳过。
MIN_KEEP_RATIO = 0.5

#: `Typesetting.render_page` 内唯一一处「把版框底边上抬」的语句（源码锚点）。
_LIFT_ANCHOR = "if p_upper.box and new_y < p_upper.box.y2:"
_LIFT_STMT = "p_upper.box.y = new_y"

_typesetting_guard_installed = False


def install_typesetting_overlap_guard(
    keep_ratio: float = MIN_KEEP_RATIO, verbose: bool = False
) -> bool:
    """给 babeldoc 的 ``Typesetting.render_page`` 打「防重叠抬升」守卫。

    治的是什么缺陷（babeldoc 0.6.4，bioengineering-12-00311 实测）
    ------------------------------------------------------------------
    ``render_page`` 在渲染前有一段防重叠逻辑：某段的版框若碰到「紧贴其下方
    的段落」，就把该段**底边上抬**到 ``下方段落顶端 + required_gap``，以此让出
    间距。紧接着 render 阶段会拿这个（已经变矮的）版框去
    ``_find_optimal_scale_and_layout`` 搜字号 —— **框越矮，搜出的 scale 越小**。

    问题在于那个「下方的段落」可能根本不是邻居：同一段文字在 IL 里会出现两份
    对象（一份来自图 XObject ``xobj_id != 0``，一份来自页面正文 ``xobj_id == 0``），
    两者版框几乎完全重合。此时循环把副本当成下方邻居，算出的 ``new_y`` 远高于
    自身底边，一抬就把版框压成一条线。第 6 页实测一条：

    ==============  =============================  ===================
    阶段            版框                           随后得到的字号
    ==============  =============================  ===================
    preprocess      h=94.12 → optimal_scale=1.0     （完全放得下）
    render 抬升后   h=4.63（底边上抬 91.7pt）       scale=0.1999… → 1.99pt
    ==============  =============================  ===================

    结果就是整段正文塌成 2pt「面条字」。**根子不在「字号调太小」，而在「版框被
    自己人压扁了」**，所以守卫必须钉在抬升那一步，而不是等缩完再抢救。

    守卫规则
    ------------------------------------------------------------------
    抬升后剩余高度 < ``keep_ratio`` × 原高度 → **不抬升**（不写 ``box.y``）。
    物理含义：给下方邻居让位，最多只该压掉一半高度；要压掉一半以上，说明那个
    「冲突段落」与自身大面积重叠、并非真正的下方邻居。此时保持原版框，让文字以
    原字号正常渲染 —— 宁可稍微贴近，也不要塌成不可读的 2pt。
    ``keep_ratio <= 0`` 等价于关闭守卫（退回官方行为）。

    怎么打的（不改 site-packages）
    ------------------------------------------------------------------
    源码锚点替换：``inspect.getsource`` 取出 ``render_page`` → 找到锚点那一处
    ``p_upper.box.y = new_y`` → 就地换成带守卫的三行 → ``exec`` 到一个以模块
    全局**副本**为命名空间的字典里 → 取回新函数回填到类上。副本保证注入名
    （``_KEEP_RATIO`` 等）和改名后的 ``render_page`` 都不会泄回 babeldoc 模块。

    锚点找不到（babeldoc 升级改了实现）时**不补丁、只告警**并返回 ``False``，
    保持官方行为，绝不半途抛异常。

    实测效果（本论文）：第 6/7 页 <5pt 异常小字 710 字 → **0 字**；覆盖率
    173/173、注入 190 段、中文 8115 字全部不变；文字行重叠对数（≥15% 阈值）
    与打补丁前**逐对相同**，未新引入任何重叠。
    """
    global _typesetting_guard_installed
    if _typesetting_guard_installed:
        return True

    import inspect
    import textwrap

    from babeldoc.format.pdf.document_il.midend import typesetting as _ts

    try:
        src = textwrap.dedent(inspect.getsource(_ts.Typesetting.render_page))
    except (OSError, TypeError) as e:  # 源码不可得（如只装了 pyc）
        _ts.logger.warning(f"[overlap-guard] 取不到 render_page 源码，跳过补丁：{e}")
        return False

    lines = src.splitlines()
    tgt = None
    for idx in range(len(lines) - 1):
        if lines[idx].strip() == _LIFT_ANCHOR and lines[idx + 1].strip() == _LIFT_STMT:
            tgt = idx + 1
            break
    if tgt is None:
        _ts.logger.warning(
            "[overlap-guard] 未找到抬升锚点（babeldoc 可能已升级）"
            "→ 跳过补丁，保持官方行为"
        )
        return False

    indent = lines[tgt][: len(lines[tgt]) - len(lines[tgt].lstrip())]
    lines[tgt : tgt + 1] = [
        f"{indent}if (p_upper.box.y2 - new_y) < _KEEP_RATIO * para_height:",
        f"{indent}    _LIFT_SKIPPED[0] += 1",
        f"{indent}else:",
        f"{indent}    p_upper.box.y = new_y",
    ]
    # 补上 future import：原文件若依赖延迟求值注解，去掉后重新编译会 NameError
    patched_src = "from __future__ import annotations\n" + "\n".join(lines) + "\n"

    ns: dict = {"_KEEP_RATIO": float(keep_ratio), "_LIFT_SKIPPED": OVERLAP_LIFT_SKIPPED}
    ns.update(vars(_ts))
    try:
        exec(compile(patched_src, "<babeldoc-overlap-guard>", "exec"), ns)  # noqa: S102
        func = ns["render_page"]
    except Exception as e:  # noqa: BLE001
        _ts.logger.warning(
            f"[overlap-guard] 补丁编译失败，跳过：{type(e).__name__}: {e}"
        )
        return False

    _ts.Typesetting.render_page = func
    _typesetting_guard_installed = True
    if verbose:
        print(f"    [overlap-guard] 已启用（keep_ratio={keep_ratio}）", flush=True)
    return True


# ---- 驱动 ----------------------------------------------------------------


def build_config(
    pdf,
    out_dir,
    translator: OfflineTranslator,
    pages: str | None = None,
    dual: bool = False,
    render: bool = True,
) -> TranslationConfig:
    return TranslationConfig(
        translator=translator,
        input_file=str(pdf),
        lang_in="en",
        lang_out="zh",
        doc_layout_model=None,             # None -> 自动 load_available()（本地 ONNX 模型）
        pages=pages,
        output_dir=str(out_dir),
        no_mono=not render,                # Pass1 只收集文本，不写 PDF
        no_dual=(not render) or (not dual),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        disable_same_text_fallback=True,   # 避免「译文与原文相同」被判为失败而回退
        skip_scanned_detection=True,
        auto_extract_glossary=False,       # 关闭自动术语提取（原本也走外部 API）
        debug=False,
    )


def run_babeldoc_offline(
    pdf,
    out_dir,
    mode: str,
    translations: dict | None = None,
    pages: str | None = None,
    dual: bool = False,
    render: bool = True,
    tolerate_errors: bool = False,
    progress_cb=None,
) -> OfflineTranslator:
    """跑一遍 babeldoc 管线（零外部 API）。返回携带统计信息的 translator。

    ``tolerate_errors=True`` 时，管线后段（渲染/写盘）报错不会抛出，
    以免丢掉 collect 阶段已经攒在内存里的源文本。错误记录在 ``trans.error``。
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # babeldoc 全局限流器默认 5 QPS（为外部 API 设计，每次调用 sleep 200ms）。
    # 本地注入只是查字典，无需限速，否则数百个批次会白白多花几十秒。
    set_translate_rate_limiter(100000)

    # 图/表整体不翻译：在 babeldoc 选段阶段就把图表内部文字剔除。
    FIGURE_TABLE_SKIPPED.clear()
    _verbose = bool(os.environ.get("BABELDOC_TRANS_VERBOSE"))
    install_figure_table_guard(verbose=_verbose)

    # 防重叠抬升守卫：不许「为下方邻居让位」把版框压掉一半以上高度，
    # 否则 render 阶段会在压扁的框里把字号搜到 1–4pt（正文塌成「面条字」）。
    # 详见 install_typesetting_overlap_guard 的 docstring。
    OVERLAP_LIFT_SKIPPED[0] = 0
    install_typesetting_overlap_guard(
        keep_ratio=MIN_KEEP_RATIO, verbose=_verbose
    )

    trans = OfflineTranslator(mode=mode, translations=translations)
    trans.error = None
    cfg = build_config(pdf, out_dir, trans, pages=pages, dual=dual, render=render)

    async def _run():
        last_stage = None
        async for event in async_translate(cfg):
            etype = event.get("type")
            if etype == "error":
                raise RuntimeError(f"babeldoc 执行失败: {event.get('error')}")
            if etype in ("progress_start", "progress_update", "progress_end"):
                stage = event.get("stage")
                if progress_cb and stage and stage != last_stage:
                    last_stage = stage
                    progress_cb(stage)
            if etype == "finish":
                return event.get("translate_result")
        return None

    try:
        asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        trans.error = f"{type(e).__name__}: {e}"
        if not tolerate_errors:
            raise
    return trans
