# babeldoc-trans

PDF 论文自动翻译 Skill，基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 引擎。

## 功能

- 支持 arxiv URL / arxiv ID / 本地 PDF 作为输入
- 智能版式分析，保留原始排版（图表、公式、图片位置不变）
- 公式保护，不破坏数学公式
- 自动提取论文标题并翻译为中文作为文件名（`{中文标题}.pdf`）
- 渲染后默认对参考文献区做悬挂缩进重排（`--reflow` 默认开），成品即主交付
- 可选双语对照输出（`--dual`，默认关，一般只要一个单语成品即可）
- 四位一体翻译质量方法论：术语一致性引擎（译前建术语表强制套用）+ 形态冻结 + 数据保真（去幻觉）+ 学术文体与回译（去 AI 味），详见 `references/` 下各方法论文档（`babeldoc_翻译守则.md` 总纲，配套 `translation-rules.md` / `glossary-template.md` / `academic-format-cheatsheet.md` / `academic-style-guide.md` / `faq.md`）
- 内置专业学术译者立场与学术文体规范（`references/academic-style-guide.md`），以"中文写作水准"为首要目标，显著降低翻译腔、贴近"学者愿意直接引用的中文文本"
- 附可累积的故障案例库（`references/故障案例与恢复.md`），遇到"想不通为什么"的流水线异常可快速对照根因与恢复流程
- 渲染成功后**不自动清理** work 工作目录；任务完成后会提示用户该中间产物文件夹可手动删除（不直接代删）
- 零外部 API：翻译由 Agent 在对话中完成，不调用任何外部翻译服务、不需要 API key。注意"零外部 API"指的是不依赖外部翻译接口——译文由需要联网的智能体（如 WorkBuddy）撰写，但全程不访问任何外部 API

## 零外部 API 的工作方式

本 skill 不调用任何外部翻译服务。BabelDOC 只负责：

1. **版面解析 + 文本抽取**（阶段 1，collect）
2. **把译文注回版面 + 中文重排渲染**（阶段 3，render）

中间由 **Agent** 在对话里完成真正的翻译（阶段 2），译文以 `k`（内容哈希）键写入
`translations.jsonl`，脚本据此注入 babeldoc 渲染管线。

## 安装

```bash
# 1. 复制本文件夹到任意 Agent 的 skill 目录，例如：
cp -r babeldoc-trans/ ~/.codebuddy/skills/babeldoc-trans/
#   或
cp -r babeldoc-trans/ ~/.claude/skills/babeldoc-trans/

# 2. 安装依赖（仅本地包：babeldoc / pymupdf / opencv 等，无需 openai）
bash ~/.codebuddy/skills/babeldoc-trans/scripts/setup.sh
```

> **不需要任何 API key**，也不需要去设置 `BABELDOC_API_KEY`。

## 快速开始

在对话中发送 arxiv 链接或 PDF 文件路径即可触发；或手动调用：

```bash
python ~/.codebuddy/skills/babeldoc-trans/scripts/translate.py "<输入>" --output-dir "<输出目录>" --collect-only
# 阶段 2：Agent 翻译 work/.../pending.jsonl → translations.jsonl
python ~/.codebuddy/skills/babeldoc-trans/scripts/translate.py "<输入>" --output-dir "<输出目录>"
```

完整流程见 `SKILL.md`，阶段 2 的翻译守则见 `references/babeldoc_翻译守则.md`。

## 依赖

- Python 3.10+
- babeldoc
- torch（PyTorch 推理后端，babeldoc 的依赖，安装 babeldoc 时自动装入。默认安装 CPU 版；询问机主是否换成安装 GPU 版；需要自行确认 torch 及其相关库的版本，比较麻烦，虽然运行起来会更快，但更适合有基础的用户，没有基础的用户建议直接按照默认流程安装 CPU 版）
- pymupdf
- opencv-python-headless
- （无需 openai / 无需任何外部 API）

## 文件结构

```
babeldoc-trans/
├── SKILL.md                     # AI 触发指令与三阶段流程
├── README.md                    # 本文件
├── references/
│   ├── babeldoc_翻译守则.md        # 阶段 2 必读：版面保全翻译规则 + 四支柱质量方法论
│   ├── translation-rules.md        # 翻译质量四支柱方法论详述（术语/形态/数据/文体）
│   ├── glossary-template.md        # 术语表模板：译前建表、强制套用、译后汇报
│   ├── academic-format-cheatsheet.md  # 公式/符号/引用语法形态冻结速查
│   ├── academic-style-guide.md    # 学术文体落地模板：译者身份与立场 + 句式/时态/术语参考/翻译陷阱/质量清单
│   ├── faq.md                      # 高频问答与反模式（含数据保真、去 AI 味）
│   ├── 故障案例与恢复.md          # 可累积的故障案例库：异常现象、根因与恢复流程
│   └── usage_guide.md              # 完整参数、中间文件格式、故障排查
└── scripts/
    ├── setup.sh                 # 安装本地依赖
    ├── translate.py             # 三阶段执行脚本
    ├── offline_translator.py    # babeldoc 自定义翻译器（查表注入）
    ├── reflow_references.py     # 参考文献悬挂缩进重排（--reflow 调用）
    └── layout_rules.py          # 图/表/公式透传判定规则
```

## 与原版（上游）的区别

本 skill 修改自 [babeldoc-trans](https://github.com/kaixindelele/wechat_agent_bridge_skills/tree/main/babeldoc-trans)。原版为「接外部 API」模式（必须配置 `BABELDOC_API_KEY`、依赖 `openai`、翻译由外部 LLM API 完成）；本版在此基础上做了如下修改与优化：

| | 原版 | 本修改版 |
|---|---|---|
| 翻译方式 | 外部 LLM API（`BABELDOC_API_KEY` 必填） | **零外部 API**：译文由需联网的智能体在对话中撰写 |
| `openai` 依赖 | 必须 | 已移除 |
| 翻译质量约束 | 仅一份 `usage_guide.md` | 四位一体质量方法论 + 多份学术文体参考文档 |
| `references/` 文档数 | 1 份 | 8 份 |

核心修改：

1. **零外部 API（最大架构变化）**：去掉 `openai` 依赖与 `BABELDOC_API_KEY` 必填，翻译全程不访问外部翻译服务；流水线改为「智能体在环直译」（`--collect-only` 解析 → 读 `pending.jsonl` 直译 → `--merge` → 渲染）。"零 API"指无外部翻译接口，译文由联网智能体撰写。
2. **翻译质量方法论（最大优化点）**：引入「术语一致性引擎 + 形态冻结 + 数据保真（去幻觉）+ 学术文体与回译（去 AI 味）」四位一体方法论，集中在 `babeldoc_翻译守则.md`（总纲）与 `academic-style-guide.md`（落地模板）。
3. **内置「专业学术译者」身份与学术文体规范**：`academic-style-guide.md` 开篇即「译者身份与立场」，把目标锚定在"中文写作水准"而非"逐词对应"；含话题与主语即时性、动词驱动（介词框架警示）、主语后置与跨句指代、译后质量清单等。
4. **中间产物管理更可控**：所有中间 / 最终产物只出现在用户指定 PDF 同目录；`work/` 收进源目录；渲染后不自动清理，由用户自行删除。
5. **新增可累积的故障案例库**：`故障案例与恢复.md` 记录实际异常现象、根因与恢复流程，并预留案例模板；SKILL.md / usage_guide.md 增加通用指向。
6. **依赖与运行说明修正**：补列 `torch`（babeldoc 传递依赖，随 babeldoc 自动安装，默认 CPU 版、可换 GPU 版）；删除 `openai`；修正"全程不联网"误述。

## 其他借鉴仓库

除上游 `babeldoc-trans` 外，本 skill 还融合了以下仓库的提示词与方法论——均已消化整合进统一的学术文体与翻译质量体系，而非简单堆叠文件：

- [xindaya-translator](https://github.com/rongxinzy/RongxinAI/tree/main/SKILLs/xindaya-translator) — 学术英→中句式与文体参考，用于 `academic-style-guide.md` 的句式模板与文体规范
- [translate-polisher](https://github.com/rookie-ricardo/erduo-skills/tree/main/skills/translate-polisher) — 翻译腔诊断与润色要点，用于去 AI 味与译后质量清单
- [paper-translation](https://github.com/Thoryn-Y/skills-warehouse/tree/main/paper-translation) — 中文流畅度与可读性优化（同在本仓库中，由作者本人创作）
- [学术论文翻译师（中文版）](https://skillhub.cn/skills/chenlin-academic-paper-translator-zh) — 术语一致性引擎 + 形态冻结 + 数据保真 + 学术文体与回译的四支柱方法论内核（提炼自 PDFMathTranslate，经 credible-writer / humanizer-zh 写作哲学重构）；本 skill 的翻译质量总纲 `babeldoc_翻译守则.md` 即源于此
