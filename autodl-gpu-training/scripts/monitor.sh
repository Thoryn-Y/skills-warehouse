#!/usr/bin/env bash
# =============================================================================
# 训练进度监控面板（只读，不创建/修改/删除任何文件，也不影响训练进程）
# 用法：
#   bash monitor.sh                 # 打印一次
#   bash monitor.sh -w              # 每 30s 刷新（Ctrl-C 退出）
#   bash monitor.sh -w 60           # 每 60s
#   bash monitor.sh <总日志路径>     # 指定总日志
#
# TASKS 与 run_all.sh 里的 TASKS 保持一致；
# 脚本名里的标识词若能与某个 TASK 标签子串匹配，矩阵会自动高亮"训练中/测试中"。
#
# ⭐ 两个计数口径（务必按项目配好，否则面板数字会骗人，但都不是训练本身的问题）：
#   1) epoch 数 —— 按【epoch 编号去重】计，不要数日志行数。
#      训练日志一般是【追加】的；断点续训会把上次被打断的那一轮【重跑一遍】，
#      该 epoch 的日志行会出现两次 ⇒ 按行数计数会显示"训完 151"（真实仍是 150）。
#      ⇒ EPOCH_PAT_MAP 里请写【带捕获组】的正则，如 "Train epoch: ([0-9]+)"。
#   2) 测试数 —— 按【case】计，不要数文件数。
#      很多项目一个 case 会产出多份文件（预测/原图/金标准三份很常见），
#      数 '*' 会把 6 个 case 显示成"已测 18"。
#      ⇒ 把 TEST_CASE_GLOB 改成能唯一标识一个 case 的模式，如 "*_pred.nii.gz"。
# =============================================================================
set -u

# ------------------------------- 可配置区 ------------------------------------
# 训练任务标签列表 —— 与 run_all.sh 的 TASKS 保持一致。
# 一个任务 = 一个输出目录 <项目根>/output/<标签>/，内含 train/ 与 test/。
TASKS=(
    "<标签1>"
    # "<标签2>"
)
MAX_EPOCH=150
RUNALL_PAT="run_all.sh"                 # 用于 ps 匹配的总脚本名
SUB_PROC_RE="(train|test)-[^ ]*\.py"    # 用于识别当前训练/测试子进程的正则

# EPOCH_PAT_MAP —— 数"已完成多少个 epoch"，格式 "任务标签|正则(ERE)"。
# ⭐ 强烈建议写成【带一个捕获组、且匹配到的文本里只有这一个数字】的正则（见下例），
#    脚本会取该数字后【按编号去重】计数，断点续训重跑同一 epoch 不会多计。
#    若写成不带捕获组的普通特征（如 "Train epoch:"），只能退回"数日志行数"，
#    续训后会多计（如显示"训完 151"）——此时面板会给出提示。
EPOCH_PAT_MAP=(
    "<标签1>|Train epoch: ([0-9]+)"
    # "<标签2>|Epoch ([0-9]+):"
)

# TEST_CASE_GLOB —— 数"已测多少个 case"时，一个 case 只匹配【一个】文件的模式。
#   · 若一个 case 只产出一个文件 → 用 "*" 即可；
#   · 若一个 case 产出多个文件（如 <case>_pred.nii.gz / _img.nii.gz / _gt.nii.gz）
#     → **必须**改成能唯一标识 case 的那个（如 "*_pred.nii.gz"），
#       否则会把"文件数"当成"case 数"（实际 6 个 case 却显示"已测 18"）。
TEST_CASE_GLOB="*"
# TEST_TOTAL —— 该任务的测试 case 总数，可填数字，也可填列表文件路径（自动取其行数）。
#   留空=不显示总数；填了面板会显示 "N/TOTAL"，并在 N 恰为 TOTAL 的整数倍又大于
#   TOTAL 时提示"疑似把文件数当 case 数"。各任务测试规模不同时可留空，或用
#   TEST_TOTAL_MAP（格式 "任务标签|数字或列表路径"）逐条指定。
TEST_TOTAL=""
TEST_TOTAL_MAP=()      # 默认空数组；需要逐条指定时再填
# TEST_TOTAL_MAP=(
#     "<标签1>|lists/<数据集>/test.txt"
# )
OUT_DIR="output"
# -----------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH=0; INTERVAL=30; LOG=""
while [ $# -gt 0 ]; do
    case "$1" in
        -w|--watch) WATCH=1; shift
                    if [ $# -gt 0 ] && [ "$1" -eq "$1" ] 2>/dev/null; then INTERVAL="$1"; shift; fi ;;
        -h|--help)  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)          LOG="$1"; shift ;;
    esac
done
case "$LOG" in
    /*|[A-Za-z]:/*) : ;;
    "") LOG="$(ls -1t "$ROOT"/run_all_*.log 2>/dev/null | head -1)"
        [ -z "$LOG" ] && LOG="$ROOT/run_all.log" ;;   # 都没有就退回默认名，第4节会提示"不存在"
    *)  LOG="$ROOT/$LOG" ;;
esac

if [ -t 1 ]; then C_B=$'\033[1m'; C_D=$'\033[2m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_Z=$'\033[0m';
else C_B=""; C_D=""; C_G=""; C_Y=""; C_Z=""; fi

mtime_of(){ [ -e "$1" ] && stat -c '%y' "$1" 2>/dev/null | cut -c1-19 || echo ""; }
age_of(){
    [ -e "$1" ] || { echo "-"; return; }
    local t now d; t=$(stat -c '%Y' "$1" 2>/dev/null) || { echo "-"; return; }
    now=$(date +%s); d=$((now-t))
    if   [ "$d" -lt 60 ];    then echo "${d}s 前"
    elif [ "$d" -lt 3600 ];  then echo "$((d/60))m 前"
    elif [ "$d" -lt 86400 ]; then echo "$((d/3600))h$(((d%3600)/60))m 前"
    else                          echo "$((d/86400))d$(((d%86400)/3600))h 前"; fi
}
# 数已完成 epoch（task 用于查 EPOCH_PAT_MAP；带捕获组时按 epoch 编号去重）
count_epochs(){
    local f="$1" task="$2" pat=""
    [ -f "$f" ] || { echo ""; return; }
    for m in "${EPOCH_PAT_MAP[@]}"; do
        case "$m" in "$task|"*) pat="${m#*|}";; esac
    done
    [ -z "$pat" ] && { echo "0"; return; }
    if printf '%s' "$pat" | grep -q '('; then
        # 带捕获组 → 取编号后去重（续训重跑同一 epoch 不会重复计）
        local nums
        nums=$(grep -oE "$pat" "$f" 2>/dev/null | grep -oE '[0-9]+' | sort -n | uniq | wc -l)
        echo "${nums:-0}"
    else
        # 无捕获组 → 只能退回"数日志行数"（续训后可能多计，见面板提示）
        # 注意：grep -c 在"0 个匹配"时会打印 0 且退出码为 1，别写成 `grep -c ... || echo 0`，
        #       否则会输出 "0\n0" 这种脏值，后面做整数比较时会报错。
        local c; c=$(grep -c "$pat" "$f" 2>/dev/null | tr -dc '0-9')
        echo "${c:-0}"
    fi
}
# 该任务的 EPOCH 特征是否已"按编号去重"（返回 0=是 或 未配置；1=会退回数行数）
epoch_pat_is_dedup(){
    local task="$1" pat=""
    for m in "${EPOCH_PAT_MAP[@]}"; do case "$m" in "$task|"*) pat="${m#*|}";; esac; done
    [ -z "$pat" ] && return 0
    printf '%s' "$pat" | grep -q '('
}
# 数测试 case 数（按 TEST_CASE_GLOB 口径，避免把一个 case 的多个产物文件当成多个 case）
count_cases(){
    local d="$1"
    [ -d "$d" ] || { echo 0; return; }
    local n; n=$(ls -1 "$d"/$TEST_CASE_GLOB 2>/dev/null | wc -l)
    echo "${n:-0}"
}
# 测试 case 总数：TEST_TOTAL_MAP 优先，其次 TEST_TOTAL；数字直接用，否则当列表文件取行数
test_total(){
    local t="$1" v="$TEST_TOTAL"
    for m in "${TEST_TOTAL_MAP[@]:-}"; do case "$m" in "$t|"*) v="${m#*|}";; esac; done
    [ -z "$v" ] && { echo ""; return; }
    case "$v" in
        *[!0-9]*) [ -f "$ROOT/$v" ] && { wc -l < "$ROOT/$v" 2>/dev/null | tr -d ' '; } || echo "" ;;
        *)        echo "$v" ;;
    esac
}
# 显示 "n" 或 "n/总数"
ratio(){ local n="$1" tot="$2"
    if [ -n "$tot" ] && [ "$tot" -gt 0 ] 2>/dev/null; then echo "${n}/${tot}"; else echo "$n"; fi; }
# 个数恰为 case 总数的整数倍且更大 ⇒ 多半是把"每个 case 的多个产物文件"数成了多个 case
warn_case_count(){
    local got="$1" tot="$2"
    [ -n "$tot" ] && [ "$tot" -gt 0 ] 2>/dev/null || return 0
    [ "${got:-0}" -gt "$tot" ] 2>/dev/null || return 0
    [ $(( got % tot )) -eq 0 ] || return 0
    echo "    ${C_D}（提示：个数是 case 总数的 $((got/tot)) 倍，疑似把每个 case 的多个产物文件当成了多个 case；请把 TEST_CASE_GLOB 改成能唯一标识 case 的模式）${C_Z}"
}
tail_clean(){ [ -f "$1" ] || return 0
    tail -c 4000 "$1" 2>/dev/null | tr '\r' '\n' | grep -v '^[[:space:]]*$' | tail -n "${2:-5}"; }

panel(){
    local now; now=$(date '+%Y-%m-%d %H:%M:%S')
    echo "${C_B}==================== 训练进度面板 ====================${C_Z}"
    echo "${C_D}时间 $now   项目根 $ROOT${C_Z}"

    # 1) 总流程与当前环节
    local runall curline cur_script="" cur_task="" phase=""
    runall=$(ps -eo pid,etime,args 2>/dev/null | grep -F "$RUNALL_PAT" | grep -v grep | head -1)
    curline=$(ps -eo pid,etime,args 2>/dev/null | grep -E "$SUB_PROC_RE" | grep -v grep | head -1)
    echo; echo "${C_B}[1] 总流程${C_Z}"
    if [ -n "$runall" ]; then echo "    ${C_G}运行中${C_Z}（PID/已运行：$(echo "$runall" | awk '{print $1"  "$2}')）"
    else echo "    ${C_Y}未检测到总流程进程${C_Z}"; fi
    if [ -n "$curline" ]; then
        cur_script=$(echo "$curline" | grep -oE "$SUB_PROC_RE" | head -1)
        phase="${cur_script%%-*}"
        # 尝试用 TASKS 里的标签去脚本名里做子串匹配（命名一致就能命中）
        for _t in "${TASKS[@]}"; do
            case "$cur_script" in *"$_t"*) cur_task="$_t"; break;; esac
        done
        if [ -n "$cur_task" ]; then
            echo "    当前环节  ${C_B}${cur_task} / ${phase}${C_Z}（已运行 $(echo "$curline" | awk '{print $2}')）"
        else
            echo "    当前环节  ${C_B}${cur_script} / ${phase}${C_Z}（已运行 $(echo "$curline" | awk '{print $2}')）"
            echo "    ${C_D}（未能在 TASKS 中匹配到标签；如需高亮矩阵，请把脚本名里的标识词加进 TASKS）${C_Z}"
        fi
    elif [ -n "$runall" ]; then echo "    当前环节  环节之间的准备/收尾阶段"
    else echo "    当前环节  -"; fi

    # 2) 当前环节详情
    echo; echo "${C_B}[2] 当前环节进度${C_Z}"
    if [ -z "$cur_task" ]; then echo "    （无正在运行的环节；下方矩阵可看已完成）"
    else
        local base="$ROOT/$OUT_DIR/$cur_task"
        if [ "$phase" = "train" ]; then
            local tl="$base/train/log.txt" n
            n=$(count_epochs "$tl" "$cur_task")
            if [ -n "$n" ]; then
                local pct=$(( n*100/MAX_EPOCH ))
                printf "    训练 epoch  %s%d%s / %d (%d%%)   日志更新 %s\n" "$C_B" "$n" "$C_Z" "$MAX_EPOCH" "$pct" "$(age_of "$tl")"
            else echo "    （训练日志尚未生成：$tl）"; fi
            echo "    best  $(age_of "$base/train/best_model.pth")    last  $(age_of "$base/train/last_model.pth")"
            echo "    ${C_D}—— log.txt 末尾 ——${C_Z}"; tail_clean "$tl" 5 | sed 's/^/      /'
        else
            local pd="$base/test/predictions" npd tot
            npd=$(count_cases "$pd"); tot=$(test_total "$cur_task")
            if [ -n "$tot" ]; then
                printf "    测试 case  %s%s%s / %s（口径 '%s'）   目录更新 %s\n" \
                       "$C_B" "$npd" "$C_Z" "$tot" "$TEST_CASE_GLOB" "$(age_of "$pd")"
            else
                printf "    测试 case  %s%s%s 个（口径 '%s'；总数未知，可配 TEST_TOTAL）   目录更新 %s\n" \
                       "$C_B" "$npd" "$C_Z" "$TEST_CASE_GLOB" "$(age_of "$pd")"
            fi
            warn_case_count "$npd" "$tot"
        fi
    fi

    # 3) 进度矩阵（单层：一行一个任务）
    echo; echo "${C_B}[3] 进度矩阵（MAX_EPOCH=$MAX_EPOCH）${C_Z}"
    local t tr te n pd st ts tot
    for t in "${TASKS[@]}"; do
        tr="$ROOT/$OUT_DIR/$t/train"; te="$ROOT/$OUT_DIR/$t/test"
        n=$(count_epochs "$tr/log.txt" "$t"); n="${n:-0}"
        if [ -n "$curline" ] && [ "$cur_task" = "$t" ] && [ "$phase" = "train" ]; then st="${C_G}训练中${C_Z}"
        elif [ "$n" -ge "$MAX_EPOCH" ]; then st="${C_G}训完 ${n}${C_Z}"
        elif [ "$n" -gt 0 ]; then st="${C_Y}可续训 第${n}轮${C_Z}"
        elif [ -f "$tr/last_model.pth" ]; then st="${C_Y}有检查点${C_Z}"
        elif [ -f "$tr/best_model.pth" ]; then st="${C_D}仅best${C_Z}"
        else st="${C_D}未开始${C_Z}"; fi
        pd=$(count_cases "$te/predictions"); tot=$(test_total "$t")
        if [ -n "$curline" ] && [ "$cur_task" = "$t" ] && [ "$phase" = "test" ]; then ts="${C_G}测试中 $(ratio "$pd" "$tot")${C_Z}"
        elif [ "$pd" -gt 0 ]; then ts="${C_G}已测 $(ratio "$pd" "$tot")${C_Z}"
        else ts="${C_D}未测试${C_Z}"; fi
        printf "  %-16s train: %-22s test: %s\n" "$t" "$st" "$ts"
    done

    # 疑似口径错误（个数 = case 总数 × k，多半是把一个 case 的多个产物数成多个 case）
    # 正在测试的那个任务已在第 [2] 段末尾提示过，这里跳过避免重复
    for t in "${TASKS[@]}"; do
        [ "$t" = "$cur_task" ] && [ "$phase" = "test" ] && continue
        warn_case_count "$(count_cases "$ROOT/$OUT_DIR/$t/test/predictions")" "$(test_total "$t")"
    done

    # 计数口径提示：EPOCH 特征没写捕获组（只能数行数）时，续训后可能"多计 1"
    local _nod="" _globrisk=0
    for t in "${TASKS[@]}"; do
        if ! epoch_pat_is_dedup "$t"; then
            case "$t" in *'<标签'*) ;; *) _nod="${_nod}${_nod:+, }$t";; esac
        fi
    done
    [ -n "$_nod" ] && echo "    ${C_D}（注意：$_nod 的 EPOCH 特征未写捕获组，只能按日志行数计数；断点续训后可能比真实轮数多 1，如“训完 151”。建议改成带捕获组的正则，见脚本头部配置区）${C_Z}"
    # 默认 glob 下"有产物但不知总数"时，提醒口径可能把文件数当 case 数
    if [ "$TEST_CASE_GLOB" = "*" ]; then
        for t in "${TASKS[@]}"; do
            [ -n "$(test_total "$t")" ] && continue
            [ "$(count_cases "$ROOT/$OUT_DIR/$t/test/predictions")" -gt 1 ] 2>/dev/null && _globrisk=1
        done
        [ "$_globrisk" = "1" ] && echo "    ${C_D}（注意：TEST_CASE_GLOB 仍为默认 '*'，即“一个文件算一个 case”。若一个 case 产出多份文件（预测/原图/金标准），这里的数字会偏大；请改成如 '*_pred.nii.gz'）${C_Z}"
    fi

    # 4) 总日志末尾
    echo; echo "${C_B}[4] 总日志末尾（$LOG）${C_Z}"
    if [ -f "$LOG" ]; then
        echo "    ${C_D}大小 $(du -h "$LOG" 2>/dev/null | cut -f1)，更新 $(age_of "$LOG")${C_Z}"
        tail_clean "$LOG" 6 | sed 's/^/      /'
    else echo "    ${C_D}（不存在；启动训练时请把 stdout/stderr 重定向到该文件）${C_Z}"; fi

    # 5) 资源
    echo; echo "${C_B}[5] 资源${C_Z}"
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total \
                   --format=csv,noheader 2>/dev/null | sed 's/^/    GPU: /' || echo "    GPU: 查询失败"
    else echo "    GPU: 无 nvidia-smi"; fi
    local dq=""; [ -d /root/autodl-tmp ] && dq="/root/autodl-tmp" || dq="$ROOT"
    df -h "$dq" 2>/dev/null | awk 'NR==1{next}{printf "    磁盘 %s  已用 %s / %s (%s)\n", $6,$3,$2,$5}'
    echo "${C_B}===========================================================${C_Z}"
}

if [ "$WATCH" = "1" ]; then
    trap 'echo; echo "已退出监控（训练不受影响）"; exit 0' INT TERM
    while true; do
        command -v clear >/dev/null 2>&1 && clear 2>/dev/null
        panel
        echo "${C_D}每 ${INTERVAL}s 刷新，Ctrl-C 退出${C_Z}"
        sleep "$INTERVAL"
    done
else panel; fi