$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  ⚡ AGY Computer Use v2.1 — 全自动傻瓜式安装程序 (Windows)" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$InstallDir = Join-Path $HOME ".gemini\agy-computer-use"

# 1. 检测可用的真实 Python 3.10+（自动避开 Windows 应用商店假快捷方式）
function Find-RealPython {
    $candidates = @(
        "python",
        "py",
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($cmd in $candidates) {
        try {
            if ($cmd -eq "py") {
                $ver = & py -3 -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>$null
                if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -eq "1") {
                    return @("py", "-3")
                }
            } else {
                $ver = & $cmd -c "import sys; print(1 if sys.version_info >= (3,10) else 0)" 2>$null
                if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -eq "1") {
                    return @($cmd)
                }
            }
        } catch {}
    }
    return $null
}

Write-Host "[1/5] 正在检测本机 Python 环境..." -ForegroundColor Yellow
$PyCmd = Find-RealPython

if (-not $PyCmd) {
    Write-Host "  -> 本机尚未安装 Python 3.10+，正在为你全自动下载并静默安装 Python 3.12..." -ForegroundColor Magenta
    $installedViaWinget = $false
    try {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            $installedViaWinget = ($LASTEXITCODE -eq 0)
        }
    } catch {}

    $PyCmd = Find-RealPython
    if (-not $PyCmd) {
        $tmpExe = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
        Write-Host "  -> 正在从 Python 官方源下载安装包 (约 25MB，请稍候)..." -ForegroundColor Magenta
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $tmpExe -UseBasicParsing
        Start-Process -FilePath $tmpExe -ArgumentList "/passive InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
        $PyCmd = Find-RealPython
    }

    if (-not $PyCmd) {
        Write-Host "❌ 自动安装 Python 未完成，请手动安装 Python 3.12 后重试：https://www.python.org/downloads/" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }
}
Write-Host "  ✅ Python 环境就绪！" -ForegroundColor Green

# 2. 将程序部署到永久目录 ~/.gemini/agy-computer-use（安装包删掉也不影响以后使用！）
Write-Host "[2/5] 正在安装核心引擎到永久目录: $InstallDir ..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if ($SrcDir.TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    $excludeDirs = @(".venv", ".git", "__pycache__")
    Get-ChildItem -Path $SrcDir -Force | Where-Object { $excludeDirs -notcontains $_.Name } | ForEach-Object {
        $dest = Join-Path $InstallDir $_.Name
        Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force
    }
}
Write-Host "  ✅ 核心文件已就位！" -ForegroundColor Green

# 3. 创建独立虚拟环境并极速安装依赖（自动兼容国内清华镜像加速）
$VenvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
$VenvPyw = Join-Path $InstallDir ".venv\Scripts\pythonw.exe"
$ReqFile = Join-Path $InstallDir "requirements.txt"

if (-not (Test-Path $VenvPy)) {
    Write-Host "[3/5] 正在创建独立运行环境 (.venv)..." -ForegroundColor Yellow
    if ($PyCmd.Count -gt 1) {
        & $PyCmd[0] $PyCmd[1] -m venv (Join-Path $InstallDir ".venv")
    } else {
        & $PyCmd[0] -m venv (Join-Path $InstallDir ".venv")
    }
}

Write-Host "[3/5] 正在安装屏幕与静默操控依赖..." -ForegroundColor Yellow
& $VenvPy -m pip install --upgrade pip -q 2>$null
& $VenvPy -m pip install -r $ReqFile -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  -> 默认源较慢，正在自动切换国内清华极速镜像源..." -ForegroundColor Yellow
    & $VenvPy -m pip install -r $ReqFile -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 依赖安装失败，请检查网络连接后重试。" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }
}
Write-Host "  ✅ 依赖库全部安装完成！" -ForegroundColor Green

# 4. 自动写入本地 Antigravity (AGY) 全局 MCP + 原生 /computer-use 技能
Write-Host "[4/4] 正在原生绑定你的本地 Antigravity (MCP + /computer-use 技能)..." -ForegroundColor Yellow
& $VenvPy (Join-Path $InstallDir "setup_agy.py")

# 清理旧版多余桌面快捷方式
try {
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
    $OldShortcut = Join-Path $DesktopDir "AGY 静默操控台.lnk"
    if (Test-Path $OldShortcut) { Remove-Item -Force $OldShortcut }
} catch {}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  ✅ AGY Computer Use v3.0（GPT 原生无感模式）安装完成！" -ForegroundColor Green
Write-Host "  • 无多余网页后台，平时 100% 自动隐形；" -ForegroundColor White
Write-Host "  • 重启一次本地 Antigravity，直接在对话框说话即可后台操控！" -ForegroundColor White
Write-Host "================================================================" -ForegroundColor Green
Start-Sleep -Seconds 2

