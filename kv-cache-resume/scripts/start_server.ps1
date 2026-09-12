<#
.SYNOPSIS
    启动 llama-server（T-02）。参数集中在这里，保证每次实验的服务端配置可复现。

.DESCRIPTION
    SPEC §3.1：slot save/restore 依赖服务端 `--slot-save-path`；**不设该 flag 时返回 501**。
    故本脚本把 `-NoSlotSave` 做成显式开关 —— 用来复现 501 基线（REQ-E3 / AC5 的依据）。

.PARAMETER ModelPath
    实验用 GGUF。默认 C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf

.PARAMETER Port
    监听端口，默认 8080。

.PARAMETER Ctx
    上下文长度，默认 4096。

.PARAMETER NGpuLayers
    卸载到 GPU 的层数，默认 99（全量）。6GB 显存跑 3B Q4_K_M 足够。

.PARAMETER SlotSavePath
    slot KV 落盘目录。默认相对仓库的 kv-cache-resume\kv。

.PARAMETER KvQuantK
    K 侧 KV 缓存量化（REQ-O3 预留），取值如 f16 / q8_0 / q4_0。默认不指定（用引擎默认）。

.PARAMETER KvQuantV
    V 侧 KV 缓存量化（REQ-O3 预留），同上。

.PARAMETER Parallel
    并行 slot 数（-np）。默认 1。

.PARAMETER NoSlotSave
    **不传** `--slot-save-path` 起服务 —— 用于复现 501 基线。

.PARAMETER ExtraArgs
    追加的原始参数（数组）。

.EXAMPLE
    # 正常起服务（slot 落盘可用）
    powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1

.EXAMPLE
    # 501 基线：故意不配 slot-save-path
    powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1 -NoSlotSave -Port 8081
#>
[CmdletBinding()]
param(
    [string]$ModelPath = 'C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf',
    [int]$Port = 8080,
    [int]$Ctx = 4096,
    [int]$NGpuLayers = 99,
    [string]$SlotSavePath = '',
    [string]$KvQuantK = '',
    [string]$KvQuantV = '',
    [int]$Parallel = 1,
    [switch]$NoSlotSave,
    [string[]]$ExtraArgs = @()
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$llamaBin = 'C:\Tools\llama.cpp\bin'
$serverExe = Join-Path $llamaBin 'llama-server.exe'
if (-not (Test-Path $serverExe)) { throw "找不到 $serverExe —— 先跑 scripts\00_env_check.ps1" }
if (-not (Test-Path $ModelPath)) { throw "找不到模型 $ModelPath" }

if (-not $SlotSavePath) {
    # 本脚本位于 kv-cache-resume\scripts\，上一级即 spike 根目录
    $spikeRoot = Split-Path -Parent $PSScriptRoot
    $SlotSavePath = Join-Path $spikeRoot 'kv'
}

$args = @(
    '-m', $ModelPath,
    '-ngl', $NGpuLayers,
    '-c', $Ctx,
    '--port', $Port,
    '-np', $Parallel
)

if ($NoSlotSave) {
    Write-Host '[警告] -NoSlotSave：刻意不传 --slot-save-path，slot save/restore 应返回 501' -ForegroundColor Yellow
}
else {
    if (-not (Test-Path $SlotSavePath)) { New-Item -ItemType Directory -Force -Path $SlotSavePath | Out-Null }
    $args += @('--slot-save-path', $SlotSavePath)
}

if ($KvQuantK) { $args += @('--cache-type-k', $KvQuantK) }
if ($KvQuantV) { $args += @('--cache-type-v', $KvQuantV) }
if ($ExtraArgs.Count -gt 0) { $args += $ExtraArgs }

Write-Host 'llama-server 启动参数：' -ForegroundColor Cyan
Write-Host ("  {0} {1}" -f $serverExe, ($args -join ' '))
Write-Host ''

# 必须切到 bin 目录，否则同目录的 ggml-cuda.dll / cudart64_12.dll 找不到
Push-Location $llamaBin
try {
    & $serverExe @args
}
finally {
    Pop-Location
}
