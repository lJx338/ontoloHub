#!/usr/bin/env bash
# =============================================================================
# OntoloHub 原生安装脚本（macOS / Linux）
#
# 用法（在仓库根目录）：
#   ./install.sh                  # 默认安装
#   ./install.sh --dev            # 同时安装开发依赖
#   ./install.sh --skip-deps      # 跳过依赖安装
#   ./install.sh --skip-db        # 跳过数据库初始化
#   ./install.sh --force          # 重建 venv / node_modules
#
# 依赖：Python >= 3.11、Node.js >= 20
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

QUIET=0
DEV=0
SKIP_DEPS=0
SKIP_DB=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)       DEV=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --skip-db)   SKIP_DB=1; shift ;;
    --force)     FORCE=1; shift ;;
    --quiet)     QUIET=1; shift ;;
    -h|--help)
      sed -n '3,18p' "$0" | sed 's/^# //; s/^#//'
      exit 0
      ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

# ---------- helpers ---------------------------------------------------------
color() { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
header() { echo; color "1;36" "================================================================"; color "1;36" "  $1"; color "1;36" "================================================================"; }
step()   { [[ $QUIET -eq 1 ]] || { echo; color "32" "▶ $1"; } }
ok()     { [[ $QUIET -eq 1 ]] || color "32" "  ✓ $1"; }
warn()   { color "33" "  ⚠ $1"; }
fail()   { echo; color "31" "✗ $1"; echo; exit 1; }

version_ge() {
  # version_ge INSTALLED REQUIRED
  printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1 | grep -qx "$2"
}

# ---------- 1. 前置检查 -----------------------------------------------------
header "OntoloHub 原生安装"
step "检查前置工具"

if ! command -v python3 >/dev/null 2>&1; then
  fail "找不到 python3。请安装 Python 3.11+ 并加入 PATH。"
fi
PY_VER="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
if ! version_ge "$PY_VER" "3.11"; then
  fail "需要 Python 3.11+，当前是 $PY_VER"
fi
ok "Python $PY_VER"

if ! command -v node >/dev/null 2>&1; then
  fail "找不到 node。请安装 Node.js 20+ 并加入 PATH。"
fi
NODE_VER="$(node -v | tr -d 'v')"
if ! version_ge "$NODE_VER" "20.0.0"; then
  fail "需要 Node.js 20+，当前是 $NODE_VER"
fi
ok "Node.js $NODE_VER"

NPM_VER="$(npm -v)"
ok "npm $NPM_VER"

# ---------- 2. venv ----------------------------------------------------------
step "设置 Python 虚拟环境"
VENV_DIR="$SCRIPT_DIR/venv"

if [[ $FORCE -eq 1 && -d "$VENV_DIR" ]]; then
  warn "Force 模式：删除现有 venv"
  rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "venv 已创建"
else
  ok "venv 已存在"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools --quiet
ok "pip 已升级"

# ---------- 3. Python 依赖 ---------------------------------------------------
if [[ $SKIP_DEPS -eq 0 ]]; then
  step "安装 Python 依赖"
  pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
  ok "runtime 依赖已安装"

  if [[ $DEV -eq 1 ]]; then
    pip install -r "$SCRIPT_DIR/requirements-dev.txt" --quiet
    ok "dev 依赖已安装"
  fi

  pip install -e "$SCRIPT_DIR/apps/api"    --quiet; ok "apps/api (editable)"
  pip install -e "$SCRIPT_DIR/apps/cli"    --quiet; ok "apps/cli (editable)"
  pip install -e "$SCRIPT_DIR/packages/core" --quiet; ok "packages/core (editable)"
else
  ok "跳过 Python 依赖安装（--skip-deps）"
fi

# ---------- 3.5. 注册 monorepo src/ 到 venv ----------------------------------
step "注册 monorepo src/ 到 venv（editable 路径）"
PTH_FILE="$VENV_DIR/lib/python${PY_VER%.*}/site-packages/ontolohub_monorepo.pth"
SUBLIST=("$SCRIPT_DIR/apps/cli/src" "$SCRIPT_DIR/packages/core/src")
EXISTING=()
for sub in "${SUBLIST[@]}"; do
  [[ -d "$sub" ]] && EXISTING+=("$sub")
done
if [[ ${#EXISTING[@]} -gt 0 ]]; then
  printf '%s\n' "${EXISTING[@]}" > "$PTH_FILE"
  ok "已写入 $PTH_FILE（${#EXISTING[@]} 条路径）"
else
  ok "未找到 src/ 子包，跳过"
fi

# ---------- 4. Node 依赖 -----------------------------------------------------
if [[ $SKIP_DEPS -eq 0 ]]; then
  step "安装 Node 依赖（npm workspaces）"
  if [[ $FORCE -eq 1 && -d "$SCRIPT_DIR/node_modules" ]]; then
    warn "Force 模式：清理 node_modules"
    rm -rf "$SCRIPT_DIR/node_modules"
  fi
  npm install --no-audit --no-fund --loglevel=error
  ok "node_modules 已就位"
else
  ok "跳过 Node 依赖安装（--skip-deps）"
fi

# ---------- 5. 数据目录 -----------------------------------------------------
step "创建数据目录"
for d in data data/uploads data/exports data/logs prototype-build exports; do
  mkdir -p "$SCRIPT_DIR/$d"
done
ok "data/ exports/ prototype-build/ 已就绪"

# ---------- 6. .env ---------------------------------------------------------
if [[ ! -f "$SCRIPT_DIR/.env" && -f "$SCRIPT_DIR/.env.example" ]]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  ok "已生成 .env（从 .env.example 复制）"
else
  ok ".env 已存在"
fi

# ---------- 7. 数据库 --------------------------------------------------------
if [[ $SKIP_DB -eq 0 ]]; then
  step "初始化数据库（SQLite + Alembic）"
  pushd "$SCRIPT_DIR/apps/api" >/dev/null
  # alembic 的 INFO 走 stderr，2>&1 让错误码与日志解耦
  python -m alembic upgrade head 2>&1
  popd >/dev/null
  ok "Alembic 已执行到 head"
else
  ok "跳过数据库初始化（--skip-db）"
fi

# ---------- done ---------------------------------------------------------
cat <<EOF

$(color "1;36" "================================================================")
$(color "1;36" "  ✓ 安装完成")
$(color "1;36" "================================================================")

  下一步：

    # 1. 激活 Python 虚拟环境
    source venv/bin/activate

    # 2. 同时启动 API (8000) + Web (3000)
    npm run dev

    # 3. 单独启动
    npm run dev:api    # FastAPI on :8000
    npm run dev:web    # Vite on :3000

    # 4. CLI
    ontolohub --help

  健康检查：
    curl http://localhost:8000/health

EOF