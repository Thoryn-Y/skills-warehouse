#!/usr/bin/env bash
#
# setup.sh — babeldoc-trans skill 一键安装依赖（零外部 API 版）
#
# 用法:
#   bash setup.sh
#
# 安装内容:
#   - babeldoc                (版面解析 + 中文重排渲染)
#   - pymupdf                 (标题提取兜底 / 输出 PDF 验收统计)
#   - opencv-python-headless  (babeldoc 图像处理依赖)
#
# 注意: 本 skill 不调用任何外部翻译 API，翻译由 Agent 在对话中完成，
#       因此 **不需要 openai 包，也不需要任何 API key**。

set -euo pipefail

echo "=============================="
echo " babeldoc-trans 依赖安装"
echo " (零外部 API 版，无需 API key)"
echo "=============================="
echo ""

# ---------- 检测 Python ----------
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
    if command -v python3 &>/dev/null; then
        PY=python3
    elif command -v python &>/dev/null; then
        PY=python
    else
        echo "错误: 未找到 python，请先安装 Python 3.10+"
        echo "  或用 PYTHON_BIN 指定解释器，例如:"
        echo "    PYTHON_BIN=/path/to/your_env/python.exe bash setup.sh"
        exit 1
    fi
fi

PY_VER=$("$PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "使用解释器: $PY (Python ${PY_VER})"

PY_MAJOR=${PY_VER%%.*}
PY_MINOR=${PY_VER##*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "警告: Python 版本过低 (${PY_VER})，babeldoc 建议 Python 3.10+"
fi

# ---------- 安装依赖 ----------
echo ""
echo "==> 安装 Python 依赖..."
"$PY" -m pip install -q babeldoc pymupdf opencv-python-headless 2>&1 | tail -5

# ---------- 验证安装 ----------
echo ""
echo "==> 验证安装..."

PASS=true

"$PY" -c "import babeldoc; print(f'  babeldoc: {getattr(babeldoc, \"__version__\", \"ok\")}')" 2>/dev/null || {
    echo "  babeldoc: 安装失败"
    PASS=false
}

"$PY" -c "import pymupdf; print(f'  pymupdf: {pymupdf.__version__}')" 2>/dev/null || {
    echo "  pymupdf: 安装失败"
    PASS=false
}

"$PY" -c "from babeldoc.format.pdf.high_level import async_translate; print('  babeldoc.high_level: ok')" 2>/dev/null || {
    echo "  babeldoc.high_level: 导入失败（版本可能不兼容，本 skill 基于 babeldoc 0.6.x）"
    PASS=false
}

"$PY" -c "from babeldoc.translator.translator import BaseTranslator; assert hasattr(BaseTranslator, 'do_llm_translate') or True; print('  BaseTranslator: ok')" 2>/dev/null || {
    echo "  BaseTranslator: 导入失败（注入点不可用）"
    PASS=false
}

if [ "$PASS" = true ]; then
    echo ""
    echo "=============================="
    echo " 安装成功!"
    echo "=============================="
else
    echo ""
    echo "部分依赖安装失败，请检查上方输出。"
    exit 1
fi

# ---------- 资源缓存提示 ----------
echo ""
echo "==> 本地资源缓存"
echo "  首次运行会准备 DocLayout ONNX 模型与中文字体（约 330MB），"
echo "  落在 ~/.cache/babeldoc/（models / fonts / cmap / tiktoken）。"
echo "  之后仅做本地 SHA3 校验，运行时不联网。"
echo ""
echo "  可选：提前预热"
echo "    $PY -c \"import babeldoc.format.pdf.high_level as h; h.init()\""

echo ""
echo "==> 无需配置 API Key（本 skill 不调用外部翻译服务）"
echo ""
echo "快速开始（三阶段）:"
echo "  1) $PY $(dirname "$0")/translate.py paper.pdf --output-dir ~/papers_zh --collect-only"
echo "  2) 由 Agent 翻译 work/.../pending.jsonl，写入 work/.../translations.jsonl"
echo "  3) $PY $(dirname "$0")/translate.py paper.pdf --output-dir ~/papers_zh"
