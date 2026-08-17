# 术语表模板（glossary-template.md）

> 本模板用于 babeldoc-trans 的「术语一致性引擎」——阶段 2 翻译 `pending.jsonl` **之前**，先通读全部待译条目建立术语表，之后每一批翻译都带着这张表（强制套用，杜绝概念漂移）。译完把最终 `<terminology>` 清单一并汇报给用户，供人工核对。

> 用户提供术语表时，按此格式读取并严格套用。无术语表时，由 Agent 译前扫描自动生成。

## 格式 A · 简单对照（默认）
```
<terminology>
Transformer::Transformer
self-attention::自注意力
convolutional neural network::卷积神经网络
ImageNet::ImageNet（图像识别数据库）
</terminology>
```
- 左：原文术语；右：译文（含首次括注全称的写法）。
- 同一术语全篇必须同译。

## 格式 B · 带领域/备注（复杂场景）
```
<terminology>
term::译法::领域/备注
self-attention::自注意力::NLP 核心机制
ResNet::残差网络::CV 经典架构，勿译"残留网络"
BERT::BERT::常保留英文，首现括注"双向编码器表征"
</terminology>
```

## 一致性铁律
1. 缩写首次出现：保留原文缩写 + 中文括注全称（`CNN（卷积神经网络）`），之后用缩写原样。
2. 机构/会议/期刊/数据集：按领域惯例（常保留英文或固定译法），全程一致。
3. 译后输出：附一份 `<terminology>` 清单，供用户人工核对。

## 译前自动建表流程（无用户提供术语表时）
1. 扫描全文，提取：领域术语、专名、缩写、约定译法。
2. 形成术语表，置于每批 prompt 顶部强制套用。
3. 完成后输出该清单。
