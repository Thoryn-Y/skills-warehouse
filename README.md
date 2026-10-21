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
| [`autodl-gpu-training`](./autodl-gpu-training/) | 在 AutoDL 云服务器上租用 GPU 并运行深度学习训练的通用流程 | 全流程链条完整清晰，傻瓜式教学，但与 AutoDL 绑定，不通用 |
| [`babeldoc-trans`](./babeldoc-trans/) | PDF 论文自动翻译 | 基于 BabelDOC 引擎，零外部 API，保留原始排版，包含翻译质量方法论 |
| [`deeplearning-digest`](./deeplearning-digest/) | 深度学习相关论文解读 | 纯提示词、零依赖，面向深度学习初学者的低门槛讲解生成 |
| [`paper-translation`](./paper-translation/) | 学术论文中译 | 以"中文写作水准"为核心导向，强调"先吃透意思，再用中文说话" |

---

### ☁️ autodl-gpu-training — 云端 GPU 训练全流程

**定位**：不是单点功能工具，而是一套按阶段推进的操作规程——每个阶段都给命令、给判据、给踩坑提示。
**目标**：本地没卡、显存不够、或训练要跑很久时，把训练搬到 AutoDL 上跑完、跑对，并且只花该花的钱。
- **一条链走完**：选卡建实例 → 镜像配对 → 传数据 → 补环境 → 预检 → 后台启动 → 盯进度 → 定时关机 → 拉结果 → 清理，共十三章，可照做
- **判据而非感觉**：传完看「体积 + 文件数 + 命令退回提示符」、训完看按编号去重后的 epoch 数、开关机看 `nvidia-smi`，不靠猜
- **镜像一次配对**：RTX 50 系必须 CUDA ≥ 12.8 + PyTorch ≥ 2.7，选错开机即报 `no kernel image is available`
- **成本有取舍**：无卡模式传数据装环境（≈¥0.1/h）+ 定时关机防空转；数据盘免费 50 GB，扩容 +100 GB ≈ ¥0.67/天但关机也计费，训完要清可再生文件再缩容
- **计数口径防自欺**：epoch 按编号去重（续训会重跑一轮，按行数多计）、测试按 case 计数（一个 case 常有多份产物文件）
- **附 4 个脚本模板**：run_all / setup_env / monitor / pretest，按实际任务编写实际所用脚本内容

📖 详见 [`autodl-gpu-training/SKILL.md`](./autodl-gpu-training/SKILL.md)

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

📖 详见 [`deeplearning-digest/SKILL.md`](./deeplearning-digest/SKILL.md)

---

### 📝 paper-translation — 学术论文中译技能

**目标**：生产以中文为母语的研究者愿意直接引用、阅读无障碍的学术文本。

核心理念：**先吃透意思，再用中文说话。**

- 不追求逐词对应，理解整段逻辑后用中文自然语序重组
- 三大原则：主语即时性、信息密度动态调节、动词驱动去"进行"化
- 完整的关键禁忌清单与自检三问，确保输出地道中文而非翻译腔

📖 详见 [`paper-translation/SKILL.md`](./paper-translation/SKILL.md)

---

## 🗂️ 仓库结构

```
skills-warehouse/
├── README.md                 # 本文件
├── LICENSE                   # MIT 许可证
├── autodl-gpu-training/      # 云端 GPU 训练全流程 Skill
│   ├── SKILL.md              # 十三章主流程
│   └── scripts/              # 4 个脚本通用模板
│       ├── run_all.sh        # 全流程一键跑
│       ├── setup_env.sh      # 补依赖
│       ├── monitor.sh        # 查进度
│       └── pretest.sh        # 跑前自检 + 计时试跑预估时长
├── babeldoc-trans/           # PDF 论文自动翻译 Skill
│   ├── SKILL.md              # AI 触发指令与三阶段流程
│   ├── README.md             # 详细说明
│   ├── LICENSE               # MIT 许可证（此技能的归属声明和原始版权信息）
│   ├── references/           # 翻译守则、术语表、文体指南、故障案例库等
│   └── scripts/              # 安装脚本、执行脚本、自定义翻译器等
├── deeplearning-digest/      # 深度学习论文解读 Skill
│   └── SKILL.md              # 提示词
└── paper-translation/        # 学术论文中译技能
    └── SKILL.md              # 完整技能文档（规则、示例、反面模式）
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

各技能的具体安装步骤与依赖要求，请进入对应子目录查看相关文本。

---

## 📜 许可证

本仓库默认采用 [MIT 许可证](./LICENSE) 授权。

`babeldoc-trans/` 目录包含经修改和整合的衍生代码以及提示词文本（均源自其他 MIT 许可项目）。完整的归属声明和原始版权信息见 [`babeldoc-trans/LICENSE`](./babeldoc-trans/LICENSE)。
