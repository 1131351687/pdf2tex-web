# 一键启动：创建虚拟环境、安装依赖、启动服务
param(
    [int]$Port = 0,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "[pdf2tex-web] 创建虚拟环境 .venv ..."
    python -m venv $venv
}

$stamp = Join-Path $venv ".deps-installed"
if ($Reinstall -or -not (Test-Path -LiteralPath $stamp)) {
    Write-Host "[pdf2tex-web] 安装依赖 ..."
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
    Set-Content -LiteralPath $stamp -Value (Get-Date -Format o)
}

foreach ($tool in @("pandoc", "xelatex")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 $tool，生成 TeX/PDF 会失败。请先安装 Pandoc 或 TeX Live/MiKTeX。"
    }
}

if ($Port -le 0) {
    $Port = 7860
    $envFile = Join-Path $PSScriptRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $line = Select-String -LiteralPath $envFile -Pattern '^PDF2TEX_PORT=(\d+)' |
            Select-Object -First 1
        if ($line) { $Port = [int]$line.Matches[0].Groups[1].Value }
    }
}

Write-Host "[pdf2tex-web] 服务地址 http://127.0.0.1:$Port"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
