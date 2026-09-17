# =============================================================================
# OntoloHub 原生安装脚本（Windows / PowerShell 5.1+）
#
# 用法（在仓库根目录）：
#   .\install.ps1                  # 默认安装：venv + pip + npm + 初始化 SQLite
#   .\install.ps1 -Dev             # 同时安装开发依赖
#   .\install.ps1 -SkipDeps        # 仅做目录初始化 + DB 初始化
#   .\install.ps1 -SkipDb          # 不初始化数据库（你打算手动跑）
#   .\install.ps1 -Force           # 重建 venv / node_modules
#   .\install.ps1 -Quiet           # 减少输出
#
# 依赖：
#   - Python >= 3.11（PATH 上能找到 `python`）
#   - Node.js >= 20（PATH 上能找到 `node`）
#
# 安装完后：
#   venv\Scripts\Activate.ps1                 # 激活 Python 环境
#   npm run dev                                # 同时启动 API (8000) + Web (3000)
#   或者：
#   npm run dev:api                            # 只起 API
#   npm run dev:web                            # 只起 Web
# =============================================================================

[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$SkipDeps,
    [switch]$SkipDb,
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

function Write-Header {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor Cyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor Cyan
}

function Write-Step {
    param([string]$Text)
    if (-not $Quiet) {
        Write-Host ""
        Write-Host "▶ $Text" -ForegroundColor Green
    }
}

function Write-Ok {
    param([string]$Text)
    if (-not $Quiet) {
        Write-Host "  ✓ $Text" -ForegroundColor Green
    }
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host "  ⚠ $Text" -ForegroundColor Yellow
}

function Fail {
    param([string]$Text)
    Write-Host ""
    Write-Host "✗ $Text" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Get-ToolVersion {
    param([string]$Cmd, [string]$Arg = "--version")
    try {
        $out = & $Cmd $Arg 2>&1 | Select-Object -First 1
        return "$out".Trim()
    } catch {
        return $null
    }
}

function Compare-Version {
    param([string]$Installed, [string]$Required)
    # 返回 $true 表示 Installed >= Required。
    $ins = [version]($Installed -replace '[^\d.]', '')
    $req = [version]($Required -replace '[^\d.]', '')
    return ($ins -ge $req)
}

# -----------------------------------------------------------------------------
Write-Header "OntoloHub 原生安装"

# -----------------------------------------------------------------------------
# 1. 前置检查
Write-Step "检查前置工具"

$pyRaw = Get-ToolVersion "python"
if (-not $pyRaw) { Fail "找不到 python。请安装 Python 3.11+ 并加入 PATH。" }
$pyVer = ($pyRaw -replace 'Python ', '').Trim()
if (-not (Compare-Version $pyVer "3.11")) {
    Fail "需要 Python 3.11+，当前是 $pyVer"
}
Write-Ok "Python $pyVer"

$nodeRaw = Get-ToolVersion "node"
if (-not $nodeRaw) { Fail "找不到 node。请安装 Node.js 20+ 并加入 PATH。" }
$nodeVer = $nodeRaw.TrimStart('v').Trim()
if (-not (Compare-Version $nodeVer "20.0.0")) {
    Fail "需要 Node.js 20+，当前是 $nodeVer"
}
Write-Ok "Node.js $nodeVer"

$npmVer = (Get-ToolVersion "npm").TrimStart('v')
Write-Ok "npm $npmVer"

# -----------------------------------------------------------------------------
# 2. 创建虚拟环境
Write-Step "设置 Python 虚拟环境"

$venvDir = Join-Path $ScriptDir "venv"
if ($Force -and (Test-Path $venvDir)) {
    Write-Warn2 "Force 模式：删除现有 venv"
    Remove-Item -Recurse -Force $venvDir
}
if (-not (Test-Path $venvDir)) {
    & python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { Fail "创建 venv 失败" }
    Write-Ok "venv 已创建：$venvDir"
} else {
    Write-Ok "venv 已存在，跳过创建"
}

$py = Join-Path $venvDir "Scripts\python.exe"
& $py -m pip install --upgrade pip wheel setuptools --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip 自升级失败" }
Write-Ok "pip 已升级"

# -----------------------------------------------------------------------------
# 3. 安装 Python 依赖
if (-not $SkipDeps) {
    Write-Step "安装 Python 依赖"
    & $py -m pip install -r (Join-Path $ScriptDir "requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Fail "安装 requirements.txt 失败" }
    Write-Ok "runtime 依赖已安装"

    if ($Dev) {
        & $py -m pip install -r (Join-Path $ScriptDir "requirements-dev.txt") --quiet
        if ($LASTEXITCODE -ne 0) { Fail "安装 requirements-dev.txt 失败" }
        Write-Ok "dev 依赖已安装"
    }

    # 以可编辑模式安装三个 Python 包（monorepo 工作区）
    & $py -m pip install -e (Join-Path $ScriptDir "apps\api") --quiet
    if ($LASTEXITCODE -ne 0) { Fail "安装 apps/api 失败" }
    Write-Ok "apps/api (editable)"

    & $py -m pip install -e (Join-Path $ScriptDir "apps\cli") --quiet
    if ($LASTEXITCODE -ne 0) { Fail "安装 apps/cli 失败" }
    Write-Ok "apps/cli (editable)"

    & $py -m pip install -e (Join-Path $ScriptDir "packages\core") --quiet
    if ($LASTEXITCODE -ne 0) { Fail "安装 packages/core 失败" }
    Write-Ok "packages/core (editable)"
} else {
    Write-Ok "跳过 Python 依赖安装（-SkipDeps）"
}

# -----------------------------------------------------------------------------
# 3.5. 注册 monorepo src/ 路径到 venv site-packages
# 让 `from ontolohub_cli...`、`from rpc...` 等子包 import 不依赖 cwd。
Write-Step "注册 monorepo src/ 到 venv（editable 路径）"
$pthFile = Join-Path $venvDir "Lib\site-packages\ontolohub_monorepo.pth"
$pairs = @(
    @{ Name = "apps/cli";     Sub = "src" },
    @{ Name = "packages/core"; Sub = "src" }
)
$lines = @()
foreach ($p in $pairs) {
    $full = Join-Path $ScriptDir $p.Name
    if (-not (Test-Path $full)) { continue }
    $sub = Join-Path $full $p.Sub
    if (-not (Test-Path $sub)) { continue }
    $lines += "$sub"
}
if ($lines.Count -gt 0) {
    Set-Content -Path $pthFile -Value ($lines -join "`n") -Encoding ASCII
    Write-Ok ("已写入 " + $pthFile + "（" + $lines.Count + " 条路径）")
} else {
    Write-Ok "未找到 src/ 子包，跳过"
}

# -----------------------------------------------------------------------------
# 4. 安装 Node 依赖
if (-not $SkipDeps) {
    Write-Step "安装 Node 依赖（npm workspaces）"
    if ($Force -and (Test-Path (Join-Path $ScriptDir "node_modules"))) {
        Write-Warn2 "Force 模式：清理 node_modules"
        Remove-Item -Recurse -Force (Join-Path $ScriptDir "node_modules")
    }
    & npm install --no-audit --no-fund --loglevel=error
    if ($LASTEXITCODE -ne 0) { Fail "npm install 失败" }
    Write-Ok "node_modules 已就位"
} else {
    Write-Ok "跳过 Node 依赖安装（-SkipDeps）"
}

# -----------------------------------------------------------------------------
# 5. 创建数据目录
Write-Step "创建数据目录"
$dirs = @(
    "data",
    "data\uploads",
    "data\exports",
    "data\logs",
    "prototype-build",
    "exports"
)
foreach ($d in $dirs) {
    $full = Join-Path $ScriptDir $d
    if (-not (Test-Path $full)) {
        New-Item -ItemType Directory -Force -Path $full | Out-Null
    }
}
Write-Ok "data/ exports/ prototype-build/ 已就绪"

# -----------------------------------------------------------------------------
# 6. 复制 .env.example → .env（如缺）
$envFile = Join-Path $ScriptDir ".env"
$envExample = Join-Path $ScriptDir ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Ok "已生成 .env（从 .env.example 复制，按需修改）"
} else {
    Write-Ok ".env 已存在"
}

# -----------------------------------------------------------------------------
# 7. 初始化数据库（SQLite + Alembic）
if (-not $SkipDb) {
    Write-Step "初始化数据库（SQLite + Alembic）"
    Push-Location (Join-Path $ScriptDir "apps\api")
    try {
        # SQLite M0：env.py 读取 settings.database.url；保证 .env 已设好。
        # 用 cmd /c 包装让 PowerShell 把 stderr 与 exit code 解耦
        # （5.1 会把任何 stderr 误判为 NativeCommandError）。
        $alembicOut = cmd /c "$py -m alembic upgrade head 2>&1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host $alembicOut
            Fail "Alembic 升级失败"
        }
        Write-Ok "Alembic 已执行到 head"
    } finally {
        Pop-Location
    }
} else {
    Write-Ok "跳过数据库初始化（-SkipDb）"
}

# -----------------------------------------------------------------------------
Write-Header "✓ 安装完成"

Write-Host ""
Write-Host "  下一步：" -ForegroundColor Cyan
Write-Host ""
Write-Host "    # 1. 激活 Python 虚拟环境" -ForegroundColor Gray
Write-Host "    venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host ""
Write-Host "    # 2. 同时启动 API (8000) + Web (3000)" -ForegroundColor Gray
Write-Host "    npm run dev" -ForegroundColor White
Write-Host ""
Write-Host "    # 3. 单独启动" -ForegroundColor Gray
Write-Host "    npm run dev:api    # FastAPI on :8000" -ForegroundColor White
Write-Host "    npm run dev:web    # Vite on :3000" -ForegroundColor White
Write-Host ""
Write-Host "    # 4. CLI" -ForegroundColor Gray
Write-Host "    ontolohub --help" -ForegroundColor White
Write-Host ""
Write-Host "  健康检查：" -ForegroundColor Cyan
Write-Host "    curl http://localhost:8000/health" -ForegroundColor White
Write-Host ""