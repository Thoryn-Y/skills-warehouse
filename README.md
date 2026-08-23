# skills-warehouse

> 人工智能代理技能的个人收藏。如果它对我有用，它就在这里。  
> A personal stash of AI agent skills. If it's useful to me, it's in here.

请随意浏览，但不保证它会适合您的工作流程。  
Feel free to browse, but no promises it'll fit your workflow.

---

## 📦 仓库内容 / What's Inside

本仓库收录了面向 AI Agent（如 Claude、Kimi、WorkBuddy 等）的**可复用技能（Skills）**，涵盖学术翻译、论文解读等场景。每个技能均为独立文件夹，包含完整的提示词、执行脚本与参考文档。

| 技能 | 场景 | 核心特点 |
|------|------|----------|
| [`babeldoc-trans`](./babeldoc-trans/) | PDF 论文自动翻译 | 基于 BabelDOC 引擎，零外部 API，保留原始排版，包含翻译质量方法论 |
| [`deeplearning-digest`](./deeplearning-digest/) | 深度学习相关论文解读 | 纯提示词、零依赖，面向深度学习初学者的低门槛讲解生成 |
| [`paper-translation`](./paper-translation/) | 学术论文中译 | 以"中文写作水准"为核心导向，强调"先吃透意思，再用中文说话" |

---

### 🔤 babeldoc-trans — PDF 论文自动翻译

**输入**：arXiv 链接 / arXiv ID / 本地 PDF  
**输出**：保留原始排版的中文 PDF（可选双语对照）

- **零外部 API**：翻译由 Agent 在对话中完成，无需 OpenAI API Key 或任何外部翻译服务
- **智能版式保全**：图表、公式、图片位置不变；公式保护，不破坏数学符号
- **四位一体质量方法论**：术语一致性引擎 + 形态冻结 + 数据保真（去幻觉）+ 学术文体与回译（去 AI 味）
- **内置学术文体规范**：以"中文写作水准"为首要目标，显著降低翻译腔
- **自动文件名**：提取论文标题并翻译为中文作为输出文件名

📖 详见 [`babeldoc-trans/README.md`](./babeldoc-trans/README.md) 与 [`babeldoc-trans/SKILL.md`](./babeldoc-trans/SKILL.md)

---

### 🧠 deeplearning-digest — 深度学习论文解读

**输入**：任意深度学习相关论文  
**输出**：面向初学者的低门槛讲解文章

- 先给"地图"：问题背景、现有方法死穴、作者核心思路
- 陌生概念以「学术定义 + 直觉类比」双重解释
- 讲"为什么"而非仅仅"是什么"：关键设计连带动机与取舍讲透
- 实验以因果推理呈现，附带未来研究方向与可操作的后续实践路径

📖 详见 [`deeplearning-digest/README.md`](./deeplearning-digest/README.md) 与 [`deeplearning-digest/SKILL.md`](./deeplearning-digest/SKILL.md)

---

### 📝 paper-translation — 学术论文中译技能

**目标**：生产以中文为母语的研究者愿意直接引用、阅读无障碍的学术文本。

核心理念：**先吃透意思，再用中文说话。**

- 不追求逐词对应，理解整段逻辑后用中文自然语序重组
- 三大原则：主语即时性、信息密度动态调节、动词驱动去"进行"化
- 完整的关键禁忌清单与自检三问，确保输出地道中文而非翻译腔

📖 详见 [`paper-translation/README.md`](./paper-translation/README.md) 与 [`paper-translation/SKILL.md`](./paper-translation/SKILL.md)

---

## 🗂️ 仓库结构

```
skills-warehouse/
├── README.md                 # 本文件
├── LICENSE                   # MIT 许可证
├── babeldoc-trans/           # PDF 论文自动翻译 Skill
│   ├── SKILL.md              # AI 触发指令与三阶段流程
│   ├── README.md             # 详细说明
│   ├── LICENSE               # MIT 许可证（此技能的归属声明和原始版权信息）
│   ├── references/           # 翻译守则、术语表、文体指南、故障案例库等
│   └── scripts/              # 安装脚本、执行脚本、自定义翻译器等
├── deeplearning-digest/      # 深度学习论文解读 Skill
│   ├── SKILL.md              # 提示词
│   └── README.md             # 说明
└── paper-translation/        # 学术论文中译技能
    ├── SKILL.md              # 完整技能文档（规则、示例、反面模式）
    └── README.md             # 说明
```

---

## 🚀 快速开始

每个技能文件夹均为**独立可移植单元**，可直接复制到对应 Agent 的 skill 目录中使用：

```bash
# 示例：将 babeldoc-trans 复制到 CodeBuddy 技能目录
cp -r babeldoc-trans/ ~/.codebuddy/skills/babeldoc-trans/

# 示例：将 deeplearning-digest 复制到 Claude 技能目录
cp -r deeplearning-digest/ ~/.claude/skills/deeplearning-digest/
```

各技能的具体安装步骤与依赖要求，请查阅对应子目录的 `README.md`。

---

## 📜 许可证

本仓库默认采用 [MIT 许可证](./LICENSE) 授权。

`babeldoc-trans/` 目录包含修改和整合后的第三方代码与提示词文本。完整的归属声明和原始版权信息见 [`babeldoc-trans/LICENSE`](./babeldoc-trans/LICENSE)。
