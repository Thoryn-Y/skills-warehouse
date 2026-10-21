#!/usr/bin/env bash
# =============================================================================
# 全流程一键跑：按顺序执行本项目的各个训练 / 测试环节
# 用法：bash run_all.sh
#
# ★ 断点续训能力来自各 trainer.py（本脚本只负责按顺序调用它们）。
#   使用本脚本前，必须确保每个 trainer.py 已实现正确的断点续训逻辑：
#
#   · 训练过程中，每个 epoch 训练结束、验证完毕后，把【完整检查点】写入
#       <输出目录>/last_model.pth
#     内容至少包含：epoch、iter_num、best_loss、model_state_dict、
#     optimizer_state_dict、（若用 AMP）scaler_state_dict。
#     ——这是断点续训的唯一依据。
#     ⚠ 保存的 epoch 是「刚跑完的那个 epoch 的索引」，编号一般是从 0 起
#       （range(start_epoch, max_epoch) → 0 .. max_epoch-1）；
#       因此续训起点 = checkpoint['epoch'] + 1。
#
#   · <输出目录>/best_model.pth 只存【纯权重】（model.state_dict()），
#     供 test / 推理加载；它不承担续训责任，不需要往里塞 epoch / optimizer。
#     ⚠ 它不是每个 epoch 都写：只有当该 epoch 的验证指标优于历史最佳时才更新（即"打擂"式覆盖）。历史最佳值由训练脚本在内存中维护（变量 best_loss），并随 last_model.pth 一并持久化到其 best_loss 字段；续训时从该字段读回内存。
#
#   · 训练脚本启动时：
#       若 last_model.pth 不存在 → 从 epoch 0 开始；
#       若 last_model.pth 存在   → 从 checkpoint['epoch'] + 1 继续；
#       若 checkpoint['epoch'] + 1 >= max_epoch（即续训起点已达到上限，
#         range(start_epoch, max_epoch) 为空）
#                              → 循环不执行，等价于"已跑完"（日志提示 already finished）。
#     ⚠ checkpoint['epoch'] 存的是「最后完成的 epoch 索引」，编号 0 .. max_epoch-1；
#       "是否跑完"没有显式标志位，是启动时用「续训起点 vs max_epoch」隐式判断的。
#
#   · 想从头重训某个任务：删掉对应的 last_model.pth（或整个 train 目录）。
#
# 所有路径为相对路径，整个项目目录可整体拷贝 / 移动，无需改路径。
# 任一环节失败（退出码非 0）立即终止，便于定位。
# =============================================================================
set -u

# ------------------------------- 可配置区 ------------------------------------
PYTHON=""                    # 留空=用 PATH 里的 python；可填绝对路径
MAX_EPOCHS=150

# 本项目的「训练任务」标签列表（仅用于：① 输出状态预检 ② 约定输出目录名）。
# 一个任务 = 一次"训练 + 测试"单元；只有一次训练就只写一个；
# 有多个模型 / 多次独立训练就依次列出。
TASKS=(
    "<标签1>"
    # "<标签2>"
    # "<标签3>"
)
# 约定输出目录：<项目根>/output/<标签>/train 与 .../test
# -----------------------------------------------------------------------------

[ -z "$PYTHON" ] && PYTHON="python"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT/output"

# ---------------------- 输出状态预检（只读，不删不跳）------------------------
echo "================== 输出状态预检（只读）=================="
for T in "${TASKS[@]}"; do
    TR="$ROOT/output/$T/train"; TE="$ROOT/output/$T/test"
    if   [ -f "$TR/last_model.pth" ]; then TSTAT="有检查点(将续训/跳过；是否训满请看日志 epoch 数)"
    elif [ -f "$TR/best_model.pth" ]; then TSTAT="仅有 best(将从头训)"
    else TSTAT="未开始"; fi
    if [ -d "$TE/predictions" ] && [ -n "$(ls -A "$TE/predictions" 2>/dev/null)" ]; then ESTAT="已有预测"
    else ESTAT="未测试"; fi
    printf "  [%s]  train: %s | test: %s\n" "$T" "$TSTAT" "$ESTAT"
done
echo "======================================================"

run(){
    echo ""; echo "======================================================================"
    echo "[$(date +%H:%M:%S)] $*"
    echo "======================================================================"
    "$@"; local rc=$?
    if [ $rc -ne 0 ]; then
        echo "" >&2; echo "[失败] 上一条命令退出码 $rc，流程终止。" >&2; exit $rc
    fi
}

# =============================================================================
# 测试包装：若该任务"已测完"，直接跳过；否则执行测试。
#
# ⚠ 按项目改：下面 "判据" 那段是"测试已完成"的判断逻辑。
#
#   ⭐ 首选【判据 A：看日志里是否出现"跑完的完成标志行"】——它与"产物文件数量"
#      无关，最不容易出错，推荐优先保留它。
#
#   【判据 B：按 case 计数】也可用，但**必须让两边的口径一致**：
#      一个 case 若产出多个文件（很常见：预测 / 原图 / 金标准 三份），
#      就必须用"一个 case 只匹配一个文件"的 glob（如 "*_pred.nii.gz"）。
#      否则会变成拿"文件数"去比"case 数"：例如 6 个 case × 3 个文件时，
#      只跑完 2 个 case（6 个文件）就被判成"已完成"，
#      ⇒ **会错误跳过一个根本没跑完的测试**。
#
#   下面同时给出 A、B 两种写法的骨架；按项目实际情况**保留一种、删掉另一种**。
# =============================================================================
run_test(){
    # $1 = 任务标签；$2 = 该任务的测试输出目录（相对 $ROOT）；其余 = 测试命令
    local task="$1" testdir="$2"; shift 2

    # ---------------------------- 判据（按项目改）---------------------------
    local done=0

    # ── 判据 A（推荐）：日志里出现"跑完的完成标志行"
    #    若项目的测试日志在别的路径 / 标志字符串不同，就改这两处。
    local log
    log=$(ls -1t "$ROOT/$testdir"/test_log/*.txt \
                 "$ROOT/$testdir"/test_log.txt 2>/dev/null | head -1)
    if [ -n "$log" ] && grep -q "Testing performance in best val model" "$log"; then
        done=1
    fi

    # ── 判据 B（可选，与判据 A 二选一；用 B 时把上面 A 那段注释掉）
    #    ⚠ glob 必须"一个 case 只匹配一个文件"：若一个 case 产出 pred/img/gt 三份，
    #      就写 '*_pred.nii.gz'；写 '*' 会把文件数当 case 数、可能提前判成已完成。
    # local glob='*'               # ← 按项目改
    # local expect got
    # expect=$(wc -l < "$ROOT/lists/<数据集>/test.txt" 2>/dev/null || echo 0)
    # got=$(ls -1 "$ROOT/$testdir"/predictions/$glob 2>/dev/null | wc -l)
    # [ "${expect:-0}" -gt 0 ] && [ "${got:-0}" -ge "${expect:-0}" ] && done=1
    # -----------------------------------------------------------------------

    if [ "$done" = "1" ]; then
        echo ""; echo "======================================================================"
        echo "[$(date +%H:%M:%S)] [跳过] ${task} 测试已完成"
        echo "======================================================================"
        return 0
    fi
    run "$@"
}

# =============================================================================
# ⚠ 主体：按项目实际情况写每个训练 / 测试环节
#
#   不强行做成数据驱动（不同脚本参数差异太大），直接写清楚更好维护。
#   下面给两种常见形态，二选一，或按项目混合：
#
#   ── 形态 A：只训练一个模型一次 ─────────────────────────────────────────
#     cd "$ROOT/<你的工作目录>" || exit 1
#     run "$PYTHON" "<训练脚本>.py" \
#         --output_dir "../output/<标签>/train" \
#         --max_epochs "$MAX_EPOCHS" \
#         <其余训练参数...>
#     run_test "<标签>" "output/<标签>/test" \
#         "$PYTHON" "<测试脚本>.py" \
#         --output_dir "../output/<标签>/test" \
#         --ckpt_dir   "../output/<标签>/train" \
#         <其余测试参数...>
#
#   ── 形态 B：多个训练任务依次跑（1~N 个都行）────────────────────────────
#     # 每个任务单独一段；cd 到该任务的工作目录，再调 train / test。
#     # 若参数结构相似可以写 for 循环，否则逐段写更清楚。
#
#     cd "$ROOT/<目录1>" || exit 1
#     run "$PYTHON" "train-xxx.py" --output_dir "../output/<标签1>/train" \
#         --max_epochs "$MAX_EPOCHS" <参数...>
#     run_test "<标签1>" "output/<标签1>/test" \
#         "$PYTHON" "test-xxx.py"  --output_dir "../output/<标签1>/test" \
#         --ckpt_dir   "../output/<标签1>/train" <参数...>
#
#     cd "$ROOT/<目录2>" || exit 1
#     run "$PYTHON" "train-yyy.py" --output_dir "../output/<标签2>/train" \
#         --max_epochs "$MAX_EPOCHS" <参数...>
#     run_test "<标签2>" "output/<标签2>/test" \
#         "$PYTHON" "test-yyy.py"  --output_dir "../output/<标签2>/test" \
#         --ckpt_dir   "../output/<标签2>/train" <参数...>
# =============================================================================

# <在这里填入你项目的主体调用>

echo ""
echo "======================================================================"
echo "全部完成，输出见 $ROOT/output"
echo "======================================================================"