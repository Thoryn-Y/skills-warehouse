#!/usr/bin/env bash
# =============================================================================
# 上线前预检 + 训练耗时预估
# 用法：
#   bash pretest.sh                      # 完整预检（含真实规模计时试跑，需要 GPU）
#   bash pretest.sh --timing-epochs 1    # 少跑几个 epoch 更快，但更不准确
#   bash pretest.sh --no-smoke           # 只做环境/资产/语法，不试跑
#   bash pretest.sh --dry-run            # 只打印试跑命令，不真跑（无需 GPU）
#   bash pretest.sh --clean              # 先清空旧临时目录
#   bash pretest.sh --help
# 不删除、不修改任何既有文件，不碰正式 output/。
# ⚠ 上传云端时请把临时目录排除。
# ⭐ 计时口径：训练用【全量训练列表 + 正式 batch】跑 TIMING_EPOCHS 个 epoch 计时；
#    微型列表只给【测试】。原因见可配置区 FULL_LISTS 上方的说明（用微型列表
#    计时会让总预估偏小一到两个数量级，据此设定时关机会提前掐断训练）。
# =============================================================================
set -u

# ------------------------------- 可配置区 ------------------------------------
PYTHON=""                        # 留空=用 PATH 里的 python
TIMING_EPOCHS=3                  # 试跑 epoch 数
REAL_EPOCHS=150                  # 正式流程 epoch 数（用于折算）
TMPTOP_NAME=".pretest_tmp"       # 临时目录名

# ⭐⭐ 计时试跑必须用【全量训练列表 + 正式 batch】，否则预估会严重偏小：
#   微型列表（如 head -n 4 取几行）会让 1 个 epoch 只有几次迭代，而正式训练
#   常有上百到几百次（差 70~90 倍很常见）⇒ 折算出的总时长会偏小一到两个数量级，
#   拿它去设定时关机会【提前掐断训练】。
#   ⇒ FULL_LISTS 指向全量训练列表目录（相对项目根，如 "lists/BTCV"）；
#     留空 = 不加 --list_dir，用训练脚本自身的默认列表（默认通常也是全量）。
#     ⚠ 本脚本临时生成的微型列表（$LISTS）【只能给测试】，绝不能传给训练。
FULL_LISTS="<相对路径，如 lists/<数据集>>"
# 微型列表只取几行；测试用（测试每个 case 通常只要几十秒）
MICRO_TRAIN=4; MICRO_VAL=2; MICRO_TEST=1
# ⚠ 按项目改：要跑语法检查的代码目录（相对项目根）。
#   注意占位符必须放在引号里，否则尖括号会被 shell 当成重定向、脚本直接解析失败。
CODE_DIRS=(
    "<代码目录1>"
    # "<代码目录2>"
)
# 试跑要遍历的训练任务（与 run_all.sh 的 TASKS 保持一致）
TASKS=(
    "<标签1>"
    # "<标签2>"
)
# 每个模型的工作目录 + 试跑命令模板（可在下方 run_train / run_stage 里按项目展开）
# -----------------------------------------------------------------------------

NO_SMOKE=0; CLEAN=0; DRY=0
show_help(){ awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "${BASH_SOURCE[0]}"; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-smoke) NO_SMOKE=1 ;;
        --dry-run)  DRY=1 ;;
        --clean)    CLEAN=1 ;;
        --timing-epochs)  shift; TIMING_EPOCHS="${1:-3}" ;;
        --timing-epochs=*) TIMING_EPOCHS="${1#*=}" ;;
        --help|-h)  show_help; exit 0 ;;
        *) echo "未知参数：$1"; exit 2 ;;
    esac; shift
done
case "$TIMING_EPOCHS" in ''|*[!0-9]*|0) echo "错误：--timing-epochs 需正整数"; exit 2;; esac
[ -z "$PYTHON" ] && PYTHON="python"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
TMPTOP="$ROOT/$TMPTOP_NAME"
WORK="$TMPTOP/run_$STAMP"
LISTS="$WORK/lists"; OUT="$WORK/out"; LOGS="$WORK/logs"
TIMING_TSV="$WORK/train_timing.tsv"

[ "$CLEAN" = "1" ] && [ -d "$TMPTOP" ] && { echo "[清理] 删除 $TMPTOP"; rm -rf "$TMPTOP"; }

if [ -t 1 ]; then C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_WR=$'\033[33m'; C_BD=$'\033[1m'; C_Z=$'\033[0m';
else C_OK=""; C_NO=""; C_WR=""; C_BD=""; C_Z=""; fi
hr(){ printf '%s\n' "======================================================================"; }
title(){ echo; hr; printf '%s%s%s\n' "$C_BD" "$*" "$C_Z"; hr; }
pass(){ PASS=$((PASS+1)); printf '  %s[ 通过 ]%s %s\n' "$C_OK" "$C_Z" "$*"; }
fail(){ FAIL=$((FAIL+1)); printf '  %s[ 失败 ]%s %s\n' "$C_NO" "$C_Z" "$*"; }
skip(){ SKIP=$((SKIP+1)); printf '  %s[ 跳过 ]%s %s\n' "$C_WR" "$C_Z" "$*"; }
warn(){ printf '  %s[ 注意 ]%s %s\n' "$C_WR" "$C_Z" "$*"; }
info(){ printf '  [信息] %s\n' "$*"; }
PASS=0; FAIL=0; SKIP=0; DRYCMD=0

run_stage(){   # run_stage <环节名> <命令...>
    local name="$1"; shift
    if [ "$DRY" = "1" ]; then
        printf '  [ 计划 ] %s\n           (cd %s &&\n             %s)\n' "$name" "$PWD" "$*"
        DRYCMD=$((DRYCMD+1)); return 0
    fi
    local log="$LOGS/$name.log"
    printf '  [....] %s\n' "$name"
    if "$@" >"$log" 2>&1; then pass "$name"
    else local rc=$?; fail "$name（退出码 $rc）—— 日志末尾 25 行："
         tail -n 25 "$log" | sed 's/^/          | /'
         echo "          | （完整日志：$log）"; fi
}
expect_file(){ if [ "$DRY" = "1" ]; then printf '  [ 计划 ] 应生成 %s\n' "$2"; return 0; fi
    if [ -f "$1" ]; then pass "$2"; else fail "$2（未生成：$1）"; fi; }
expect_files(){ if [ "$DRY" = "1" ]; then printf '  [ 计划 ] 应生成 %s %s\n' "$2" "$3"; return 0; fi
    local n; n=$(ls -1 "$1"/$2 2>/dev/null | wc -l)
    if [ "${n:-0}" -gt 0 ]; then pass "$2 $3（$n 个）"; else fail "$2 $3（为空或不存在：$1）"; fi; }

_human(){ local s="$1"
    if   [ "$s" -ge 3600 ]; then printf '%dh%02dm' $((s/3600)) $(((s%3600)/60))
    elif [ "$s" -ge 60 ];   then printf '%dm%02ds' $((s/60))   $((s%60))
    else                         printf '%ds' "$s"; fi; }

# 从训练脚本自己写出的 log.txt 里读每个 epoch 的真实耗时（正则按项目日志格式改）
record_timing(){
    local task="$1" outdir="$2" caplog="$3" wall="$4" status="$5"
    local line
    line=$("$PYTHON" - "$outdir/log.txt" "$caplog" "$wall" "$TIMING_EPOCHS" "$REAL_EPOCHS" <<'PY'
import re, sys, os
primary, fallback = sys.argv[1], sys.argv[2]
wall, n_ep, real_ep = float(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
# ⚠ 按你的训练脚本日志格式改这里的正则：
PER = re.compile(r'\[(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\]\s*<日志里每 epoch 开始的行特征>')
def stamps(path):
    if not path or not os.path.isfile(path): return []
    try: txt = open(path, errors='ignore').read()
    except Exception: return []
    return [int(h)*3600+int(m)*60+int(s) for h,m,s in PER.findall(txt)]
ts = stamps(primary) or stamps(fallback)
src = "log.txt" if stamps(primary) else ("捕获日志" if ts else "墙钟兜底")
if len(ts) >= 2: per = (ts[-1]-ts[0])/float(len(ts)-1)
elif n_ep > 0:   per = wall/n_ep
else:            per = wall
if per <= 0:     per = wall/max(n_ep,1)
est = per*real_ep
print("%s\t%.3f\t%.3f\t%.3f" % (src, wall, per, est))
PY
)
    printf '%s\t%s\t%s\n' "$task" "$status" "$line" >> "$TIMING_TSV"
}

# 跑一个"训练"环节：真实规模 + 计时
run_train(){
    local name="$1" task="$2" outdir="$3"; shift 3
    if [ "$DRY" = "1" ]; then
        printf '  [ 计划 ] %s （训练 %s 个 epoch）\n           (cd %s &&\n             %s)\n' \
            "$name" "$TIMING_EPOCHS" "$PWD" "$*"
        DRYCMD=$((DRYCMD+1)); return 0
    fi
    local log="$LOGS/$name.log"
    printf '  [....] %s（计时：%s 个 epoch）\n' "$name" "$TIMING_EPOCHS"
    local t0 t1 wall rc; t0=$(date +%s)
    if "$@" >"$log" 2>&1; then rc=0; else rc=$?; fi
    t1=$(date +%s); wall=$((t1-t0))
    if [ "$rc" -eq 0 ]; then pass "$name（实测 $(_human "$wall")）"
    else fail "$name（退出码 $rc）—— 日志末尾 25 行："
         tail -n 25 "$log" | sed 's/^/          | /'; fi
    if [ "$rc" -eq 0 ]; then record_timing "$task" "$outdir" "$log" "$wall" OK
    else                     record_timing "$task" "$outdir" "$log" "$wall" FAIL; fi
}

# =============================================================================
hr
echo "上线前预检"; echo "项目根  : $ROOT"; echo "解释器  : $PYTHON"
echo "预检时间: $(date '+%Y-%m-%d %H:%M:%S')"; echo "产物目录: $WORK"; hr

# =============================================================================
title "[1/4] 环境检查（解释器 / torch / CUDA / 依赖）"
# ⚠ 按项目改 import 列表
"$PYTHON" - <<'PY'
import importlib, sys
fails=[]
def chk(c,m):
    print(("  [ OK ] " if c else "  [FAIL] ")+m)
    if not c: fails.append(m)
print("  解释器 :", sys.executable); print("  版本   :", sys.version.split()[0])
try:
    import torch
    chk(True, "torch %s" % torch.__version__)
    if torch.cuda.is_available():
        chk(True, "CUDA 可用（cuda=%s，GPU 数=%d）" % (torch.version.cuda, torch.cuda.device_count()))
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            print("             gpu%d: %s  %.1f GB" % (i, p.name, p.total_memory/1024**3))
    else: chk(False, "CUDA 不可用（请检查镜像是否 GPU 版）")
except Exception as e: chk(False, "torch 导入失败：%s" % e)
try:
    import torchvision; chk(True, "torchvision %s" % torchvision.__version__)
except Exception as e: chk(False, "torchvision 导入失败：%s" % e)
# ⚠ 按项目改：
for imp, pipn in [("numpy","numpy"),("scipy","scipy")]:
    try: m=importlib.import_module(imp); chk(True,"%-14s %s"%(imp,getattr(m,"__version__","")))
    except Exception as e: chk(False,"%s 缺失（pip 名 %s）：%s"%(imp,pipn,e))
sys.exit(0 if not fails else 1)
PY
[ $? -eq 0 ] && pass "环境检查通过" || fail "环境检查存在问题（见上）；可执行 bash setup_env.sh 补齐"
echo "  磁盘剩余："; df -h "$ROOT" 2>/dev/null | sed -n '1p;2p' | sed 's/^/          /'
HAS_CUDA=$("$PYTHON" -c "import torch;print(1 if torch.cuda.is_available() else 0)" 2>/dev/null || echo 0)

# =============================================================================
title "[2/4] 数据 / 列表 / 配置 / 预训练权重 检查"
# ⚠ 按项目改：目录、列表文件、关键资产路径
cd "$ROOT" || exit 1
"$PYTHON" - <<'PY'
import os, sys, glob
fails=[]
def chk(c,m):
    print(("  [ OK ] " if c else "  [FAIL] ")+m)
    if not c: fails.append(m)
# ⚠ 按项目改：
TR="data/train_npz"; TE="data/test_vol_h5"; LS="lists"
chk(os.path.isdir(TR), "%s 存在" % TR)
chk(os.path.isdir(TE), "%s 存在" % TE)
n_tr=len(glob.glob(os.path.join(TR,"*.npz"))); n_te=len(glob.glob(os.path.join(TE,"*.npy.h5")))
print("            训练切片 : %d 个"%n_tr); print("            测试体数据 : %d 个"%n_te)
chk(n_tr>0,"训练切片非空"); chk(n_te>0,"测试体数据非空")
def rows(p):
    try:
        with open(p) as f: return [l.strip() for l in f if l.strip()]
    except Exception: return []
tr,va,te = rows(os.path.join(LS,"train.txt")), rows(os.path.join(LS,"val.txt")), rows(os.path.join(LS,"test.txt"))
for nm,v in [("train.txt",tr),("val.txt",va),("test.txt",te)]:
    chk(len(v)>0, "%s/%s 存在且非空（%d 行）"%(LS,nm,len(v)))
# ⚠ 按项目改：抽样核对"列表里的名字 ↔ 实际文件"、抽样真实读取
sys.exit(0 if not fails else 1)
PY
[ $? -eq 0 ] && pass "资产检查通过" || fail "资产检查存在问题（见上）"

# =============================================================================
title "[3/4] 脚本语法检查"
cd "$ROOT" || exit 1
PYFAIL=0; PYTOTAL=0
for _d in "${CODE_DIRS[@]}"; do
    if [ ! -d "$ROOT/$_d" ]; then warn "代码目录不存在，跳过：$_d"; continue; fi
    while IFS= read -r f; do
        PYTOTAL=$((PYTOTAL+1))
        if ! "$PYTHON" -m py_compile "$f" 2>/tmp/pyc_err; then
            PYFAIL=$((PYFAIL+1)); fail "语法错误：$f"; sed 's/^/          | /' /tmp/pyc_err
        fi
    done < <(find "$ROOT/$_d" -name "*.py" -not -path "*__pycache__*" | sort)
done
[ "$PYFAIL" -eq 0 ] && pass "$PYTOTAL 个 .py 全部编译通过" || fail "$PYTOTAL 个 .py 中 $PYFAIL 个语法错误"
for _s in "$ROOT"/*.sh; do
    [ -f "$_s" ] || continue
    if bash -n "$_s" 2>/tmp/sh_err; then pass "$(basename "$_s") 语法通过"
    else fail "$(basename "$_s") 语法错误："; sed 's/^/          | /' /tmp/sh_err; fi
done

# =============================================================================
title "[4/4] 试跑 + 耗时预估（${TIMING_EPOCHS} epoch 全量 + 测试 1 个 case）"
if [ "$NO_SMOKE" = "1" ]; then skip "已指定 --no-smoke"; 
elif [ "$DRY" != "1" ] && [ "$HAS_CUDA" != "1" ]; then skip "无可用 CUDA，跳过试跑";
else
    if [ "$DRY" != "1" ]; then
        mkdir -p "$LISTS" "$OUT" "$LOGS"
        # ⚠ 按项目改列表路径。这里的微型列表【仅供测试】跑通链路用——
        #   训练绝对不要传 --list_dir "$LISTS"（理由见文件头，会让预估偏小一到两个数量级）。
        head -n "$MICRO_TRAIN" "$ROOT/lists/train.txt" > "$LISTS/train.txt"
        head -n "$MICRO_VAL"   "$ROOT/lists/val.txt"   > "$LISTS/val.txt"
        head -n "$MICRO_TEST"  "$ROOT/lists/test.txt"  > "$LISTS/test.txt"
    fi
    info "训练：全量列表（${FULL_LISTS:-脚本默认}） + 正式 batch，共 ${TIMING_EPOCHS} 个 epoch（真实规模计时）"
    info "测试：微型列表（$LISTS，${MICRO_TEST} 个 case），仅验证链路"
    if [ -n "$FULL_LISTS" ] && [ -f "$ROOT/$FULL_LISTS/train.txt" ]; then
        info "全量训练列表行数：$(wc -l < "$ROOT/$FULL_LISTS/train.txt")（可据此核对正式 1 个 epoch 的迭代次数 ≈ 行数 / batch_size，验证确实是全量规模）"
    fi
    info "输出全部落在：$OUT（不动正式 output/）"
    [ "$DRY" = "1" ] && warn "--dry-run：只打印命令，不真跑"

    # ⚠ 下面按项目展开：对每个模型/方案调用 run_train + run_stage
    #   · run_train（训练计时）传【全量】列表：--list_dir "$ROOT/$FULL_LISTS"
    #     （若 FULL_LISTS 留空，就删掉这一行、用训练脚本自身默认列表）
    #   · run_stage（测试）才传微型列表：--list_dir "$LISTS"
    #   · expect_files 的 glob 用"一个 case 一个文件"的模式（如 '*_pred.nii.gz'），
    #     否则一个 case 的多份产物会被数成多个 case。
    # 例：
    # for T in "${TASKS[@]}"; do
    #     run_train "${T}_train" "$T" "$WORK/out/$T/train" \
    #         "$PYTHON" "train-${T}.py" --output_dir "$WORK/out/$T/train" \
    #         --list_dir "$ROOT/$FULL_LISTS" \
    #         --max_epochs "$TIMING_EPOCHS" ...
    #     expect_file "$WORK/out/$T/train/best_model.pth" "${T} best"
    #     run_stage "${T}_test" "$PYTHON" "test-${T}.py" \
    #         --output_dir "$WORK/out/$T/test" --ckpt_dir "$WORK/out/$T/train" \
    #         --list_dir "$LISTS" ...
    #     expect_files "$WORK/out/$T/test/predictions" "*_pred.nii.gz" "${T} 预测"
    # done
fi

# =============================================================================
title "训练耗时预估（用于设置云服务器定时关机时间）"
if [ "$DRY" = "1" ]; then skip "--dry-run：未计时";
elif [ ! -s "$TIMING_TSV" ]; then skip "本次未产生计时数据";
else
    "$PYTHON" - "$TIMING_TSV" "$REAL_EPOCHS" "$WORK" "$TIMING_EPOCHS" <<'PY'
import os, sys
tsv, real_ep, work, timed_ep = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
def dur(s):
    s=int(round(s))
    if s>=3600: return "%dh%02dm"%(s//3600,(s%3600)//60)
    if s>=60:   return "%dm%02ds"%(s//60,s%60)
    return "%ds"%s
rows=[]
for line in open(tsv, encoding="utf-8", errors="ignore"):
    p=line.rstrip("\n").split("\t")
    if len(p)<6: continue
    rows.append({"task":p[0],"status":p[1],"src":p[2],
                 "wall":float(p[3]),"per":float(p[4]),"est":float(p[5])})

# 排序：默认按 task 标签字母序，或保持 TSV 写入顺序（写 TASKS 的顺序）。
# 如需自定义顺序，按项目加一个 order = {"<标签1>":0, "<标签2>":1} 再排。
rows.sort(key=lambda r: r["task"])

out = []
def P(s=""):
    out.append(s); print(s)

P("  实测口径：每个训练环节跑 %d 个 epoch（全量数据 + 正式 batch）；" % timed_ep)
P("            每 epoch 耗时取自训练脚本自己写出的 log.txt 相邻 epoch 时间戳；")
P("            预估训练时长 = 每 epoch 耗时 × %d（正式 epoch 数）。" % real_ep)
P("")
P("  %-18s %-12s %-10s %-12s %s"%("任务","实测(N ep)","每epoch","预估训练","计时数据来源"))
P("  "+"-"*76)
total=0.0; sub={}
for r in rows:
    tag="" if r["status"]=="OK" else "   (该环节未成功，估值不完整)"
    if r["status"]=="OK":
        total+=r["est"]; sub[r["task"]]=sub.get(r["task"],0.0)+r["est"]
    P("  %-18s %-12s %-10s %-12s %s%s"%(r["task"],dur(r["wall"]),dur(r["per"]),dur(r["est"]),r["src"],tag))
P("  "+"-"*76)
for t,v in sub.items(): P("  %-18s 小计：%s"%(t,dur(v)))
P("")
if total>0: P("  ★ 总预估训练时间（不含测试）：%s（约 %.1f 小时）"%(dur(total),total/3600.0))
else:       P("  ★ 没有成功的训练环节，无法给出总预估。")
P("")
P("  提示：")
P("    · 测试阶段未计入。")
P("    · 该预估基于【全量数据 + 正式 batch】的实测每-epoch 耗时；")
P("      若某环节实际是用微型列表跑的，其估值会偏小一到两个数量级。")
P("    · 设定时关机请在总预估上再留 10~20% 缓冲。")
P("    · 短程外推，epoch 越多越准。")
path=os.path.join(work,"train_time_estimate.txt")
try:
    open(path,"w",encoding="utf-8").write("\n".join(out)+"\n"); print(); print("  已写入：%s"%path)
except Exception as e: print("  (写入 %s 失败：%s)"%(path,e))
PY
fi

# =============================================================================
title "预检汇总"
echo "  通过 $PASS 项 | 失败 $FAIL 项 | 跳过 $SKIP 项"
[ "$DRY" = "1" ] && echo "  （--dry-run：共列出 $DRYCMD 条命令，未真正执行）"
echo "  预检产物：$WORK"; [ -d "$LOGS" ] && echo "  分环节日志：$LOGS"; echo
if [ "$DRY" = "1" ]; then
    printf '%s--dry-run 结束：以上命令即正式流程的等价试跑版。%s\n' "$C_OK" "$C_Z"; exit 0
fi
if [ "$FAIL" -eq 0 ]; then
    printf '%s预检全部通过 —— 可以开始正式训练：bash run_all.sh%s\n' "$C_OK" "$C_Z"
    echo "说明：临时目录 $TMPTOP_NAME 确认无误后可整体删除（上传云端时请排除）。"; exit 0
else
    printf '%s预检存在 %d 项失败 —— 请先修复再开训。%s\n' "$C_NO" "$FAIL" "$C_Z"
    [ -d "$LOGS" ] && echo "提示：失败环节完整日志在 $LOGS/<环节名>.log"; exit 1
fi