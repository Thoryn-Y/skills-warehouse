---
name: babeldoc-trans
description: "PDF论文自动翻译工具。使用 BabelDOC 引擎将英文 PDF 论文翻译为中文，保留原始版面（图表、公式、图片位置不变），输出文件自动以论文的中文标题命名。翻译由 Agent 在对话中完成，不依赖任何外部翻译 API、无需 API key。支持 arxiv URL / arxiv ID / 本地 PDF 路径作为输入。当用户发送 arxiv 链接、上传 PDF 文件、或说翻译论文、翻译这篇 PDF、translate paper、把论文译成中文时触发。"
---

# BabelDOC 论文翻译（零外部 API）

基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 的论文 PDF 翻译封装。
版面分析、公式保护、字体嵌入全部本地完成；**翻译由你（Agent）在对话中直接完成**，
通过自定义翻译器注入回 babeldoc 渲染管线，**全程不调用任何外部 API、不需要 API key**。

## 触发条件

- 用户发送 arxiv 链接（`https://arxiv.org/abs/xxxx.xxxxx` 或 `https://arxiv.org/pdf/xxxx.xxxxx`）
- 用户发送 arxiv ID（如 `2412.13211`）
- 用户上传或指定 PDF 文件路径
- 用户说"翻译这篇论文"、"translate this paper"等

## 运行环境（重要）

必须用带 babeldoc / onnxruntime / pymupdf 的 Python 环境。若机主无指定，每次运行需向机主询问使用哪个环境。

下文用 `$PY` 代指该解释器（运行前由机主指定，或询问机主后确定），`$SKILL` 代指本 skill 目录。

## 工作流程（三阶段，agent 在环）

> **默认输出位置 = 源 PDF 所在目录**（本地 PDF 输入时）。
> 不传 `--output-dir` 时，脚本会自动把成品 PDF 与中间文件 `work/` 都放到**源文件旁边**；
> 只有用户明确要求「输出到别的目录」时，才传 `--output-dir`。**不要为了"整洁"自行指定其他输出目录**。

### 阶段 1 — 收集源文本（零 API）

```bash
$PY "$SKILL/scripts/translate.py" "<输入>" --collect-only
```

babeldoc 解析版面，把每个待译段落的**内部文本**（含 `{vN}` 公式占位符、`<style>` 标签）
落盘。脚本会用白名单自动透传图表编号、URL、纯标识符、参考文献等，减少翻译量。

输出末行 `[DONE-COLLECT] {JSON}`，关键字段：

| 字段 | 含义 |
|---|---|
| `work_dir` | 中间文件目录 |
| `pending` | **待你翻译**的条目文件（`pending.jsonl`） |
| `write_translations_to` | 译文要写入的目标文件（`translations.jsonl`） |
| `count` / `pending_count` / `auto_filled` | 总条数 / 待译条数 / 自动透传条数 |
| `title_en` | 英文标题 |

### 阶段 2 — Agent 翻译（关键步骤）

1. **先读 `references/babeldoc_翻译守则.md`（必读）**，逐条遵循；其「§〇 方法论依据与补充阅读」列出了 5 个深度参考文件
   （`translation-rules.md` / `glossary-template.md` / `academic-format-cheatsheet.md` / `academic-style-guide.md` / `faq.md`），翻译时**按需查阅**。
   质量四支柱贯穿全程：**术语一致性、形态冻结、数据保真（去幻觉）、学术文体 + 回译校验（去 AI 味）**。
   其中**学术文体**支柱的落地模板（中↔英句式、时态/语态、术语参考译法、翻译陷阱、译后质量清单）见 `references/academic-style-guide.md`（开篇含「译者身份与立场」设定，阶段 2 动笔前应通读），
   撰写或润色译文时对照使用，可显著降低翻译腔、提升术语与文风一致性。
2. **译前建术语表（术语一致性引擎，最关键的提分句）**：读 `pending.jsonl` 前，先通读全部待译条目，
   按 `references/glossary-template.md` 提取领域术语 / 专有名词 / 缩写，建一张 `<terminology>` 术语表；
   之后**每一批翻译都把这张表置于上下文顶部强制套用**，杜绝同一概念前后译法不一（概念漂移）。译完把最终术语表随成品汇报给用户。
3. 读取 `pending.jsonl`，**每批 30–40 条**（或累计 ≤ 8000 字符）翻译。
3. 每批单独写一个 `work_dir/_batch_NN.jsonl`（NN 为两位序号），每行**只用 `k` 键**：

```jsonl
{"k": "<pending.jsonl 该条的 k 字段>", "output": "<中文译文>"}
```

> `k` 是原文的 12 位内容哈希。用 `k` 就不必逐字重抄原文，
> 从根本上避免「少一个空格 / 吃掉 `{v1}` / 标签属性写错」导致注入未命中——
> 这是本流程最容易翻车的地方。（也支持 `{"input": ..., "output": ...}`，但不推荐。）

4. 标题条目的译文是**论文中文标题**，用作输出文件名（≤ 40 字）。
5. 每写完一批就合并 + 体检 + 看进度：

```bash
$PY "$SKILL/scripts/translate.py" "<输入>" --merge --status
```

- `--merge` 把所有 `_batch_*.jsonl` 汇总成 `translations.jsonl`，并逐条校验
  **标签序列**与**占位符集合**是否与原文一致，异常会打印 `[CHECK] ...`。**必须修到 0 异常**。
- `[STATUS] {...}` 给出 `coverage`（覆盖率）并把 `pending.jsonl` 刷新为「只剩没翻的」，
  所以下一批直接读 `pending.jsonl` 前 N 条即可。

**分批落盘 + 每批合并校验 = 天然断点续译**：中断后重跑只需翻译剩下的，已翻的批次文件不受影响。

### 阶段 3 — 回填渲染

```bash
$PY "$SKILL/scripts/translate.py" "<输入>"
```

脚本校验覆盖率（**低于 95% 会中止并报告缺失项**，避免产出半英半中的 PDF），
然后把译文注入 babeldoc 渲染出中文 PDF；默认再对参考文献区做悬挂缩进重排
（`--reflow` 默认开）。**`work` 工作目录不会自动删除**——任务完成后由你（智能体）
主动告知用户哪些中间产物可删（详见下方「任务收尾」）。

输出末行 `[DONE] {JSON}`：`title_zh`、`output_pdf`（已以完整中文标题命名、即悬挂缩进重排版，
文件名不含任何随机后缀）、`coverage`、`inject_hit`/`inject_miss`、`zh_chars`/`latin_words`（验收用）。

最后把 `output_pdf` 发送给用户，并按下方「任务收尾」告知用户可删的中间产物。

## 常用参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--output-dir` | 最终 PDF 输出目录（默认：本地 PDF → 源文件同目录；URL/arxiv → `~/babeldoc_output`） | 源 PDF 同目录 |
| `--work-dir` | 中间文件目录（运行后保留，不自动删除） | `<output-dir>/work/<自动>` |
| `--pages` | 只处理指定页，如 `1-5` | 全部 |
| `--collect-only` | 阶段 1：只收集源文本 | off |
| `--merge` | 合并 `_batch_*.jsonl` 并校验标签/占位符 | off |
| `--status` | 查看覆盖率、刷新 `pending.jsonl` | off |
| `--dual` | 额外输出双语对照 PDF | off |
| `--allow-partial` | 覆盖率不足时强制渲染（缺失项保留英文） | off |
| `--translate-refs` | 参考文献也翻译（默认保留英文） | off |
| `--reflow` | 渲染完成后对参考文献区做悬挂缩进重排，重排后的成品即主交付 `{中文标题}.pdf`（完整中文名、无随机后缀，会覆盖同名旧版；默认开；`--no-reflow` 可关闭） | on |

> **`--pages` 必须在阶段 1 和阶段 3 保持一致**，否则覆盖率校验会异常（脚本会给出警告）。

## 注意事项

- **遇到想不通的异常先看故障案例库**：凡是流水线行为「想不通为什么」的异常（覆盖率异常、`pending` 为空、无法重跑、渲染命中率低等），先看 `references/故障案例与恢复.md`——那是可累积的故障案例库，收录实际遇到的现象、根因与恢复流程。
- **无需任何 API key**，也不要去设置 `BABELDOC_API_KEY`。旧的 `--api-key/--api-base/--model/--qps`
  参数已废弃，传了会被忽略并告警。
- 版面分析会跑两遍（阶段 1 与阶段 3 各一次），这是两遍法的固有代价。
  阶段 1 已关闭 PDF 写盘，比阶段 3 略快。14 页论文单遍约 1–3 分钟（GPU）。
- `--reflow` **默认开启**：渲染完成后自动对参考文献区做悬挂缩进重排，重排后的成品即主交付
  `{中文标题}.pdf`（不再单独保留未重排版）。`--no-reflow` 可关闭，此时主交付为普通单语 PDF。
  重排只对**单语成品**生效（双语 PDF 不做，避免左右栏错位）；重排时参考文献正文用拉丁字体重绘，
  **因此假定参考文献保持英文**——即与默认的 `--translate-refs` 关闭搭配最稳。
  若同时开 `--translate-refs --reflow`，译文里的中文参考文献可能因缺少 CJK 字体而显示异常，慎用。
  重排前会自动识别参考文献 section 的边界：扫描后续页面，遇到 Supplementary Material / Appendix / Acknowledgements / 补充材料 / 附录 / 致谢 等大章节标题时停止，并把该页及其之后**原样保留**，避免把补充材料等内容误当成参考文献 wiping 掉、导致后续排版混乱。
- **`work` 工作目录不会自动删除**（沙箱对单次删除文件数有限额，且按约定删除操作一律由用户亲自执行）。
  任务完成后，请**主动告知用户**：输出目录下除最终翻译版 PDF 外，还有一个 `work/` 中间产物文件夹
  （含 babeldoc 缓存与临时 PDF），「如不需要可手动删除」，并给出该 `work/` 的绝对路径。
  同时**不要**自行执行任何删除（包括 `rm`、移入回收站、批量清理脚本）——只列出路径让用户决定。
- **成品文件命名**：最终 PDF 直接用论文中文标题全名（如
  `MedNeXt：受Transformer驱动的ConvNet规模化用于医学图像分割.pdf`），**不带 `_32104` 之类的随机后缀**；
  若同名文件已存在（例如上一轮未重排版），新成品会**直接覆盖**它，保证目录里始终只有一个干净的成品。
- 静态资源（CJK 字体 254 MB、版面模型 72 MB、cmap、tiktoken）已全量缓存在
  `C:\Users\username\.cache\babeldoc\`，本地 SHA3 校验，运行时不联网。

## 安装依赖

首次使用前运行：
```bash
bash "$SKILL/scripts/setup.sh"
```

## 详细文档

- `references/babeldoc_翻译守则.md` — **阶段 2 必读**的版面保全翻译规则（含 §〇 方法论依据、§四 术语一致性引擎、数据保真、学术文体+回译）
- `references/translation-rules.md` — 四支柱翻译方法论溯源（为什么这样翻）
- `references/glossary-template.md` — 术语表格式与译前建表 / 译后输出 `<terminology>` 清单
- `references/academic-format-cheatsheet.md` — LaTeX / 公式 / 引用 / 单位"必须原样"速查
- `references/academic-style-guide.md` — 学术文体落地模板（中↔英句式 / 时态语态 / 术语参考译法 / 翻译陷阱 / 译后质量清单）
- `references/faq.md` — 高频问答与反模式（公式/数据/双语/术语控制/AI 味）
- `references/故障案例与恢复.md` — 可累积的故障案例库：实际遇到的异常现象、根因与恢复流程（遇到想不通的异常先来翻相似案例）
- `references/usage_guide.md` — 完整参数、中间文件格式、性能参考、故障排查
