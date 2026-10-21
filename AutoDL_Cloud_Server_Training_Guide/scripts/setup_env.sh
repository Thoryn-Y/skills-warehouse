#!/usr/bin/env bash
# =============================================================================
# 环境准备脚本（幂等：只补缺失，不重装核心包）
# 用法：
#   bash setup_env.sh            # 检查 + 安装缺失
#   bash setup_env.sh --check    # 只检查不安装
#   bash setup_env.sh --help
# ⚠ 本脚本不会安装/卸载/升级 torch、torchvision —— 云端必须用镜像自带的。
# =============================================================================
set -u

# ------------------------------- 可配置区 ------------------------------------
PYTHON=""                       # 留空=用 PATH 里的 python；可填绝对路径
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
# 依赖表：pip包名|import名|是否必需(1必需/0可选)
DEPS="
numpy|numpy|1
scipy|scipy|1
# <按项目继续补：如 h5py|h5py|1 等>
"
# 兼容性专项：<包名>|<探测命令>|<回退安装的版本>
# 例如某些包有 API 迁移，需要"真实导入探测→失败才回退"
COMPAT_SPECS="
# <import名>|<python -c '真实导入探测'>|<回退版本>
"
# -----------------------------------------------------------------------------

MODE="install"
show_help(){ awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "${BASH_SOURCE[0]}"; }
case "${1:-}" in
    --check|-c) MODE="check" ;;
    --help|-h)  show_help; exit 0 ;;
    "") ;;
    *) echo "未知参数：$1（可用：--check / --help）"; exit 2 ;;
esac

if [ -t 1 ]; then C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_WR=$'\033[33m'; C_Z=$'\033[0m';
else C_OK=""; C_NO=""; C_WR=""; C_Z=""; fi
ok(){   printf '  %s[ OK ]%s %s\n' "$C_OK" "$C_Z" "$*"; }
bad(){  printf '  %s[FAIL]%s %s\n' "$C_NO" "$C_Z" "$*"; }
warn(){ printf '  %s[WARN]%s %s\n' "$C_WR" "$C_Z" "$*"; }
info(){ printf '  [INFO] %s\n' "$*"; }
hr(){   printf '%s\n' "----------------------------------------------------------------------"; }

MISSING=0; INSTALLED=0
[ -z "$PYTHON" ] && PYTHON="python"
pip_install(){ if [ -n "$PIP_INDEX" ]; then "$PYTHON" -m pip install -i "$PIP_INDEX" "$@"; else "$PYTHON" -m pip install "$@"; fi; }
has(){ "$PYTHON" -c "import $1" >/dev/null 2>&1; }

hr; echo "环境准备脚本   模式：$([ "$MODE" = check ] && echo '只检查' || echo '检查+安装')"; hr

# ------------------------------ 0. 解释器 ------------------------------------
echo "[0] 解释器"
if ! "$PYTHON" -c "import sys" >/dev/null 2>&1; then
    bad "找不到可用的 Python：$PYTHON（请改脚本顶部 PYTHON 或先 activate）"; exit 2
fi
info "解释器 : $("$PYTHON" -c 'import sys;print(sys.executable)')"
info "版本   : $("$PYTHON" -c 'import sys;print(sys.version.split()[0])')"
[ -n "$PIP_INDEX" ] && info "pip 源 : $PIP_INDEX"

# --------------------- 1. torch 家族（只报到，不重装）------------------------
echo; echo "[1] torch / torchvision / CUDA（只报到，不重装）"
"$PYTHON" - <<'PY'
import sys
try:
    import torch
    print("  [ OK ] torch %s" % torch.__version__)
    print("         cuda=%s  available=%s  devices=%d" % (
        torch.version.cuda, torch.cuda.is_available(), torch.cuda.device_count()))
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            p = torch.cuda.get_device_properties(i)
            print("         gpu%d: %s  %.1f GB" % (i, p.name, p.total_memory/1024**3))
    else:
        print("  [WARN] CUDA 不可用：脚本里的 .cuda() 会失败。")
except Exception as e:
    print("  [FAIL] torch 导入失败：%s" % e); sys.exit(1)
try:
    import torchvision; print("  [ OK ] torchvision %s" % torchvision.__version__)
except Exception as e:
    print("  [FAIL] torchvision 导入失败：%s" % e); sys.exit(1)
PY
[ $? -ne 0 ] && MISSING=$((MISSING+1))

# --------------------------- 2. 其余依赖逐项处理 -----------------------------
echo; echo "[2] 训练/测试所需依赖"
while IFS='|' read -r pipn impn need; do
    [ -z "${pipn:-}" ] && continue
    case "$pipn" in \#*) continue;; esac
    if has "$impn"; then
        ok "$(printf '%-16s' "$impn") 已安装"
    elif [ "$MODE" = "check" ]; then
        if [ "$need" = "1" ]; then bad "$(printf '%-16s' "$impn") 缺失（必需）"; MISSING=$((MISSING+1));
        else warn "$(printf '%-16s' "$impn") 缺失（可选）"; fi
    else
        printf '  [....] 安装 %s ...\n' "$pipn"
        if pip_install "$pipn" >/tmp/pip_last.log 2>&1 && has "$impn"; then
            ok "$(printf '%-16s' "$impn") 安装成功"; INSTALLED=$((INSTALLED+1))
        else
            if [ "$need" = "1" ]; then bad "$(printf '%-16s' "$impn") 安装失败（必需）"; MISSING=$((MISSING+1));
            else warn "$(printf '%-16s' "$impn") 安装失败（可选，可忽略）"; fi
            tail -n 5 /tmp/pip_last.log | sed 's/^/         | /'
        fi
    fi
done <<EOF
$DEPS
EOF

# --------------------- 3. 兼容性专项（真实导入探测→回退）----------------------
echo; echo "[3] 兼容性专项"
while IFS='|' read -r name probe fallback; do
    [ -z "${name:-}" ] && continue
    case "$name" in \#*) continue;; esac
    if eval "$probe" >/dev/null 2>&1; then
        ok "$name 探测通过"
    else
        warn "$name 探测失败 → 回退安装 $fallback"
        if [ "$MODE" = "install" ]; then
            if pip_install "$fallback" >/tmp/pip_last.log 2>&1 && eval "$probe" >/dev/null 2>&1; then
                ok "$name 回退后通过"
            else
                bad "$name 回退失败"; tail -n 5 /tmp/pip_last.log | sed 's/^/         | /'; MISSING=$((MISSING+1))
            fi
        else MISSING=$((MISSING+1)); fi
    fi
done <<EOF
$COMPAT_SPECS
EOF

# ------------------------------ 4. 汇总 --------------------------------------
echo; hr
if [ "$MODE" = "check" ]; then
    [ "$MISSING" -eq 0 ] && printf '%s检查通过：依赖齐全。%s\n' "$C_OK" "$C_Z" \
                         || printf '%s仍有 %d 项必需依赖缺失。%s\n' "$C_NO" "$MISSING" "$C_Z"
else
    echo "本次新装：$INSTALLED 个；仍缺失（必需）：$MISSING 个"
    [ "$MISSING" -eq 0 ] && printf '%s环境已就绪。%s\n' "$C_OK" "$C_Z" \
                         || printf '%s仍有 %d 项必需依赖未就绪。%s\n' "$C_NO" "$MISSING" "$C_Z"
fi
hr
[ "$MISSING" -eq 0 ] || exit 1
exit 0