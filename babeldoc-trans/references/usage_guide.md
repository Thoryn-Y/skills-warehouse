# babeldoc-trans 使用指南（零外部 API 版）

本 skill **不调用任何外部翻译 API，也不需要 API key**。
BabelDOC 只负责「版面解析 + 文本抽取 + 中文重排渲染」，**翻译由 Agent 在对话中完成**。

## 一、三阶段工作流

```
阶段 1  --collect-only   →  babeldoc Pass1，抽出全部待译段落 → pending.jsonl
阶段 2  （无需命令）      →  Agent 读 pending.jsonl，逐条直译，写 translations.jsonl
阶段 3  （默认模式）      →  babeldoc Pass2，把译文注回版面 → 中文 PDF
```

两遍之间靠 `work` 目录里的中间文件衔接，**必须解析到同一个 work 目录**（脚本按输入源字符串的 md5 派生；跨轮次调用建议显式传 `--work-dir` 锁死）。

### 阶段 1：收集

```bash
python scripts/translate.py <输入> --output-dir DIR --collect-only
```

产出（都在 `DIR/work/<名称>_<hash>/` 下）：

| 文件 | 内容 |
|------|------|
| `source_texts.jsonl` | babeldoc 抽出的全部段落，每行 `{"input","layout_label"}`，**顺序即版面顺序** |
| `auto_filled.json` | 白名单自动透传项（编号、URL、模型名、参考文献等），派生产物，每次运行重算 |
| `pending.jsonl` | 真正需要 Agent 翻译的条目 |
| `meta.json` | 标题、页码范围、条目数等元信息 |
| `source.pdf` | 远程输入的缓存副本（本地输入则无） |

结束时打印一行 `[DONE-COLLECT] {...}` JSON，含 `work_dir` / `pending` / `write_translations_to` / `pending_count`。

### 阶段 2：翻译（Agent 执行，无命令）

读 `pending.jsonl` **之前**，先按 `references/babeldoc_翻译守则.md` 的「§四 术语一致性引擎」通读全部待译条目，
用 `references/glossary-template.md` 的格式建一张 `<terminology>` 术语表（领域术语 / 专名 / 缩写）；
之后**每批翻译都把这张表置于上下文顶部强制套用**，杜绝概念漂移，译完把最终术语表汇报给用户。

然后严格按 `references/babeldoc_翻译守则.md` 逐条直译，**分批**（建议 30–50 条/批）追加写入 `work/translations.jsonl`：

```jsonl
{"input": "<原文，必须与 pending 里的 input 逐字节一致>", "output": "<译文>"}
{"input": "__TITLE__", "output": "<论文标题中译，用作输出文件名>"}
```

关键约束：

- `input` 是查表主键，**一个字符都不能改**（含首尾空格、`{v1}` 占位符、`<style>` 标签）。
- 译文里必须原样保留 `input` 中出现的所有占位符和标签，数量、顺序一致。
- 随时可用 `--status` 查看覆盖率与剩余项：

```bash
python scripts/translate.py <输入> --output-dir DIR --status
```

输出 `[STATUS] {"total":189,"translated":150,"missing":39,"coverage":0.7937,...}`，并重新生成 `pending.jsonl` 只留缺失项。

### 阶段 3：渲染

```bash
python scripts/translate.py <输入> --output-dir DIR
```

覆盖率 ≥ 95% 才会渲染；不足会中止并列出缺失项（避免产出半英半中的 PDF）。确实想强渲染时加 `--allow-partial`，缺失条目保留英文原文。

默认还会对参考文献区做悬挂缩进重排（`--reflow` 默认开），重排成品即主交付 `{中文标题}.pdf`。
重排前会自动识别参考文献 section 的边界：扫描后续页面，遇到 Supplementary Material / Appendix / Acknowledgements / 补充材料 / 附录 / 致谢 等大章节标题时停止，并把该页及其之后**原样保留**，避免把补充材料误当成参考文献 wiping 掉、导致后续排版混乱。

结束时打印 `[DONE] {...}`，含 `output_pdf` / `coverage` / `inject_hit` / `inject_miss` / `zh_chars` / `latin_words`。
注意：**`work` 工作目录不会自动删除**——沙箱对单次删除文件数有限额、且删除操作须由用户亲自执行。任务完成后请主动告知用户输出目录下除最终 PDF 外还有一个 `work/` 中间产物文件夹（可手动删除），并给出其绝对路径；不要自行执行任何删除。
最终 PDF 直接用论文中文标题全名（如 `MedNeXt：受Transformer驱动的ConvNet规模化用于医学图像分割.pdf`），不带随机后缀；若同名文件已存在会直接覆盖，保证目录里只有一个干净成品。

## 二、参数列表

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `source` | 输入源（必填）：arxiv URL / arxiv ID / 本地 PDF 路径 | - |
| `--output-dir` | 最终 PDF 输出目录 | `~/babeldoc_output` |
| `--work-dir` | 中间文件目录（运行后保留，不自动删除） | `<output-dir>/work/<自动>` |
| `--pages` | 只处理指定页，如 `1-5`、`1,3,5-` | 全部页面 |
| `--dual` | 额外输出双语对照 PDF（默认关，一般只要一个单语成品即可） | 关闭 |
| `--collect-only` | 阶段 1：只收集源文本 | 关闭 |
| `--status` | 查看译文覆盖率并刷新 `pending.jsonl` | 关闭 |
| `--allow-partial` | 覆盖率不足时仍强制渲染 | 关闭 |
| `--translate-refs` | 参考文献条目也交给 Agent 翻译 | 关闭（默认保留英文） |
| `--reflow` / `--no-reflow` | 渲染后自动对参考文献区做悬挂缩进重排，重排后的成品即主交付 `{中文标题}.pdf`（默认开；`--no-reflow` 关闭） | 开启 |

已废弃（传了只会打一条 WARN 然后忽略）：`--api-key` `--api-base` `--model` `--qps`。
**环境变量 `BABELDOC_API_KEY` / `BABELDOC_API_BASE` / `BABELDOC_MODEL` 全部不再使用。**

## 三、使用示例

```bash
# 用带 babeldoc + torch + pymupdf 的 Python（路径由机主指定；未指定时运行前询问机主）。
# 下文统一用变量 $PY 指代该解释器。
PY="/path/to/python_with_babeldoc"

# 阶段 1
$PY scripts/translate.py "/path/to/paper.pdf" --output-dir ~/papers_zh --collect-only

# （Agent 翻译，写 work/.../translations.jsonl）

# 查看进度
$PY scripts/translate.py "/path/to/paper.pdf" --output-dir ~/papers_zh --status

# 阶段 3
$PY scripts/translate.py "/path/to/paper.pdf" --output-dir ~/papers_zh --dual

# arxiv 输入同理
$PY scripts/translate.py https://arxiv.org/abs/2412.13211 --output-dir ~/papers_zh --collect-only

# 只处理前 5 页（两遍都要带同样的 --pages，否则覆盖率会对不上）
$PY scripts/translate.py paper.pdf --output-dir ~/papers_zh --pages 1-5 --collect-only
```

## 四、白名单自动透传

为了不让 Agent 把预算浪费在「不需要翻译的东西」上，脚本会自动把下列条目原样填进 `auto_filled.json`：

| 规则 | 命中示例 |
|------|---------|
| `layout:<标签>` | **图/表/公式本体及其内部一切文字**——优先级最高，不看内容长什么样（详见下方铁律） |
| `empty` | 剥掉标签/占位符后为空 |
| `no-letter` | 纯数字、纯符号（表格数值、公式残片） |
| `tiny` | 长度 ≤ 2 |
| `label` | `Fig. 3`、`Table 2`、`Eq. 5` |
| `url` | `https://...`、`doi:...`、`arXiv:...` |
| `identifier` | `MedNeXt-B`、`nnUNet`、`BraTS21`、`SwinUNETR`（含 ≥2 个大写字母的单 token） |
| `reference` | 参考文献条目（编号前缀 **且** 含作者署名 `Xxx, Y.` 模式，长度 > 40） |

`reference` 判定刻意收紧过：只看编号前缀会把正文里的编号列表（`1. Residual Inverted Bottlenecks...`）误吞掉，因此额外要求「≥2 个作者署名」或「1 个作者 + 年份 `(2021)`」。想连参考文献一起翻译就加 `--translate-refs`。

### 4.1 铁律：图和表整体不翻译，只翻译图注 / 表注

**「图表本身」是完整概念**，落在 figure / table 版面框里的所有文字都算，一律保持英文原样：

- **图**：坐标轴标签（`Dice Score`、`Kernel Size`）、刻度数字、图例、图内注记、子图编号 `(a)/(b)`、箭头旁说明
- **表**：表头、行标题、列标题、单元格内容、表内单位标注
- **公式**：公式本体及其内部符号

**只有「注」翻译**：图注（`Figure 3: ...`）、表注（`Table 2: ...`）、表脚注（`Best results in bold.`）、公式说明。

为什么：图表内文字和矢量图形共用一套坐标系，换成中文后字宽变化会覆盖曲线、撑破单元格、与坐标轴错位；而 axis label / legend 本就是国际通用记法，翻译反而降低可读性。

**实现（两道闸门，规则单一真源在 `scripts/layout_rules.py`）**：

1. **收集端**：`classify_passthrough()` 先看 `layout_label`，命中 `NO_TRANSLATE_LAYOUT_LABELS` 直接透传，理由记为 `layout:<标签>`，这些条目根本不会进 `pending.jsonl`。
2. **注入端**：`OfflineTranslator.do_llm_translate()` 在 lookup 模式下二次拦截——即便译文表里恰好存了同名文本的中文，图表段落也强制返回原文。渲染日志会打印 `图/表/公式整体跳过 N 段`。

标签清单（24 个不翻译 / 9 个注类必翻）：

| 类别 | 不翻译的 `layout_label` |
|------|------------------------|
| 图 | `figure` `image` `chart` `figure_text` `figure_text_hybrid` |
| 表 | `table` `table_text` `table_cell` `table_cell_hybrid` `wired_table_cell` `wireless_table_cell` `form_or_table_hybrid` |
| 公式 | `formula` `isolate_formula` `formula_hybrid` |
| 版面附属 | `header` `footer` `page_header` `page_footer` `page_header_hybrid` `page_footer_hybrid` `page_number_hybrid` `line_number_hybrid` `seal` |

| 类别 | **必须翻译**的 `layout_label` |
|------|------------------------------|
| 注 | `figure_caption` `figure_title` `chart_title` `table_caption` `table_title` `table_footnote` `formula_caption` `caption` `caption_hybrid` |

两个集合在 `layout_rules.py` 里有 `assert` 保证互斥，改动时必须成对检查。

## 五、BabelDOC 在本方案中承担的能力

| 特性 | 说明 |
|------|------|
| 智能版式分析 | DocLayout ONNX 模型识别文本/图片/表格/公式区域（本地推理，不联网） |
| 公式保护 | 公式被替换成 `{v1}` 占位符，翻译时原样保留，渲染时还原 |
| 富文本保留 | `<style>` 标签承载粗体/斜体/字号，译文里必须带回去 |
| 中文重排 | 按目标语言重新计算换行与字距 |
| 字体子集化 | 自动嵌入中文字体子集，输出体积可控 |
| 中文标题重命名 | 用 `__TITLE__` 的译文作为输出文件名 |

标题识别优先采用 babeldoc 版面分析给出的 `layout_label=="title"` 首个非章节段落，
排除 arXiv 水印戳与 `1 Introduction` 这类编号章节；退化时才回落到 pymupdf 的「最大字号」启发式。

## 六、性能参考

耗时只取决于本地版面解析与渲染，**没有 token 消耗、没有 API 费用、不受限流影响**。

| 论文页数 | 阶段 1 收集 | 阶段 3 渲染 | 待译条目量级 |
|----------|------------|------------|-------------|
| 2 页 | ~15 秒 | ~20 秒 | 20–40 条 |
| 10 页 | ~40 秒 | ~1 分钟 | 100–160 条 |
| 30 页 | ~2 分钟 | ~3 分钟 | 300–450 条 |

真正的瓶颈在阶段 2：Agent 的翻译轮次。条目多时按批推进，`translations.jsonl` 是追加写，中断可续。

## 七、常见问题

> 遇到「想不通为什么」的异常（如 `pending` 为空、覆盖率异常、无法重跑、渲染命中率低等），先看 `references/故障案例与恢复.md` 这本故障案例库——它收录实际遇到的案例与恢复流程，会随使用继续扩充。

**Q: 完全不需要 API key 吗？**
是。整条链路只有本地推理和本地字体，运行时不访问任何 LLM 服务。

**Q: 两遍之间 work 目录对不上怎么办？**
显式传 `--work-dir`。脚本按「输入源字符串」的 md5 派生目录，正斜杠/反斜杠写法不同会算出不同 hash。

**Q: 渲染时报「译文覆盖率低于阈值」？**
说明还有条目没翻。看提示里的 `pending.jsonl` 路径，补齐后重跑；急着看效果就加 `--allow-partial`。

**Q: `[INJECT] 未命中 N 段` 是什么意思？**
Pass2 查表时有 N 段没找到对应译文，这些段落保留了英文。通常是 `input` 被改动过（多/少了空格、占位符被吃掉）。对照 `[INJECT] 未命中示例` 修正 `translations.jsonl` 里的 `input`。

**Q: 首次运行很慢？**
首次会准备 DocLayout ONNX 模型与字体缓存（落在 `~/.cache/babeldoc/`，约 330MB）。之后本地校验即可，不再联网。

**Q: 输出 PDF 里模型名、数据集名还是英文？**
这是刻意的（见《翻译守则》规则 11）。`MedNeXt`、`nnUNet`、`BraTS21` 这类专名保留英文更利于检索与对照。

**Q: 中文标题里有特殊字符导致文件名异常？**
脚本已过滤 `<>:"/\|?*` 并限长 80 字。

## 八、依赖

| 包名 | 用途 | 安装命令 |
|------|------|---------|
| babeldoc | 版面解析 + 中文重排渲染 | `pip install babeldoc` |
| torch | PyTorch 推理后端（babeldoc 依赖，随 babeldoc 自动安装；默认 CPU 版，运行前询问机主是否改装 GPU 版；需要自行确认 torch 及其相关库的版本，比较麻烦，虽然运行起来会更快，但更适合有基础的用户，没有基础的用户建议直接按照默认流程安装 CPU 版） | 无需单独安装 |
| pymupdf | 标题提取兜底、输出 PDF 验收统计 | `pip install pymupdf` |
| opencv-python-headless | babeldoc 图像处理依赖 | `pip install opencv-python-headless` |

**已移除 `openai`**——不再需要。

一键安装：`bash scripts/setup.sh`

运行所需的 Python 环境需带 babeldoc + torch + pymupdf。解释器路径由机主指定；若机主未指定，运行前需向其询问使用哪个环境。

## 九、翻译质量方法论参考

阶段 2 直译的质量由下列文件共同保障，翻译时按需查阅：

| 文件 | 作用 | 何时读 |
|---|---|---|
| `references/babeldoc_翻译守则.md` | 阶段 2 必读规则书（结构 / 禁止改动 / 术语一致性引擎 / 数据保真 / 学术文体+回译） | 每轮翻译前必读 |
| `references/translation-rules.md` | 四支柱方法论溯源（为什么这样翻） | 想理解原则时 |
| `references/glossary-template.md` | 术语表格式 + 译前建表 / 译后输出 `<terminology>` 清单 | 建术语表、出清单时 |
| `references/academic-format-cheatsheet.md` | LaTeX / 公式 / 引用 / 单位"必须原样"速查 | 遇拿不准的语法时 |
| `references/academic-style-guide.md` | 学术文体落地模板：中↔英句式、时态/语态/缩写、术语参考译法、翻译陷阱、译后质量清单 | 雕琢文风、自查反模式时 |
| `references/faq.md` | 高频问答与反模式（公式/数据/双语/术语/AI 味） | 用户提问或自查时 |
