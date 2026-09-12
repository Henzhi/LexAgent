<#
.SYNOPSIS
    kv-cache-resume 环境自检（T-01）。

.DESCRIPTION
    核对 E0 机制验证的前置条件是否齐备：llama.cpp CUDA 运行时、实验模型、GPU 显存。
    每项检查独立，输出 ✅/❌；只要有 ❌ 退出码为 1（可用 $LASTEXITCODE 判）。

    注意：本脚本必须能被 Windows PowerShell 5.1 正确读取，故以 **UTF-8 with BOM** 保存
    （无 BOM 时 5.1 会按 ANSI 解析，打开即是乱码）。
    重新编码时务必先剥离已有 BOM —— 直接 `read utf-8` + `write utf-8-sig` 会产生双 BOM，
    PowerShell 会报「意外的属性 CmdletBinding」（本项目已踩）。

.PARAMETER LlamaBin
    llama.cpp 解压目录（仓库外）。默认 C:\Tools\llama.cpp\bin

.PARAMETER ModelPath
    实验用 GGUF 路径。默认 C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf

.PARAMETER ModelSha256
    期望的模型 sha256（复现依据，来自 ENV.md）。

.PARAMETER SkipGenerate
    跳过真实生成冒烟（该步要加载 2GB 模型到显存，约 5~20 秒）。

.PARAMETER ReportPath
    可选的报告输出路径。给了就把同样内容以 UTF-8 落一份文件，供 T-13 验收报告直接引用
    （避免靠终端抓屏留证）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\00_env_check.ps1
#>
[CmdletBinding()]
param(
    [string]$LlamaBin = 'C:\Tools\llama.cpp\bin',
    [string]$ModelPath = 'C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf',
    [string]$ModelSha256 = '5ee4f07cdb9beadbbb293e85803c569b01bd37ed059d2715faa7bb405f31caa6',
    [switch]$SkipGenerate,
    [string]$ReportPath = ''
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$script:Failures = 0
$script:Lines = New-Object System.Collections.Generic.List[string]

function Write-Line {
    param([string]$Text, [string]$Color = '')
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    $script:Lines.Add($Text)
}

function Write-Check {
    param([bool]$Ok, [string]$Label, [string]$Detail = '')
    $suffix = if ($Detail) { " — $Detail" } else { '' }
    if ($Ok) {
        Write-Line ("  ✅ {0}{1}" -f $Label, $suffix)
    }
    else {
        Write-Line ("  ❌ {0}{1}" -f $Label, $suffix)
        $script:Failures++
    }
}

function Get-FirstLines {
    param([string]$Text, [int]$Count = 1)
    ($Text -split "`r?`n" | Where-Object { $_.Trim() -ne '' } | Select-Object -First $Count) -join ' / '
}

<#
    跑一个原生 exe 并把 stdout+stderr 一起取回为**纯文本**。

    为什么要绕这一圈：PowerShell 5.1 把原生命令的 stderr 行包成 ErrorRecord，
    直接 `2>&1 | Out-String` 会在正文里插进「所在位置 ...」这类定位信息（首版踩到）。
    经临时文件重定向则由系统写出原始文本，跨 5.1/7.x 都干净。
#>
function Invoke-NativeCapture {
    param([string]$Exe, [string[]]$Arguments)
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        & $Exe @Arguments > $tmp 2>&1
        if (Test-Path $tmp) { return (Get-Content -Path $tmp -Raw -Encoding UTF8) }
        return ''
    }
    catch {
        return ''
    }
    finally {
        Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue
    }
}

Write-Line ''
Write-Line 'kv-cache-resume · 环境自检（T-01）' 'Cyan'
Write-Line ('-' * 64)

# --- 1. 工具目录 ---------------------------------------------------------
$serverExe = Join-Path $LlamaBin 'llama-server.exe'
$cliExe = Join-Path $LlamaBin 'llama-cli.exe'
$cudaDll = Join-Path $LlamaBin 'ggml-cuda.dll'

Write-Line '1. llama.cpp 运行时'
Write-Check (Test-Path $LlamaBin) '解压目录存在' $LlamaBin
Write-Check (Test-Path $serverExe) 'llama-server.exe 存在'
Write-Check (Test-Path $cliExe) 'llama-cli.exe 存在'
Write-Check (Test-Path $cudaDll) 'ggml-cuda.dll 存在（非 CPU-only build）'

$hasServer = Test-Path $serverExe
$hasCli = Test-Path $cliExe
$versionText = ''
if ($hasServer) {
    $versionText = Invoke-NativeCapture -Exe $serverExe -Arguments @('--version')
    Write-Check ($versionText -match 'build\s+\d+') 'llama-server --version 可读' (Get-FirstLines $versionText 1)
}
else {
    Write-Check $false 'llama-server --version 可读' '可执行文件缺失'
}

# --- 2. 模型 ------------------------------------------------------------
Write-Line ''
Write-Line '2. 实验模型'
$modelOk = Test-Path $ModelPath
if ($modelOk) {
    $item = Get-Item $ModelPath
    Write-Check $true '模型文件存在' ("{0:N0} bytes" -f $item.Length)

    $hash = (Get-FileHash -Path $ModelPath -Algorithm SHA256).Hash.ToLower()
    Write-Check ($hash -eq $ModelSha256.ToLower()) 'sha256 与 ENV.md 记录一致' $hash

    # 用 .NET 读魔数：`-Encoding Byte` 在 PS 7 已移除，这种写法 5.1/7.x 通吃
    $magic = ''
    try {
        $fs = [System.IO.File]::OpenRead($ModelPath)
        try {
            $buf = New-Object byte[] 4
            [void]$fs.Read($buf, 0, 4)
            $magic = [System.Text.Encoding]::ASCII.GetString($buf)
        }
        finally { $fs.Close() }
    }
    catch { $magic = '' }
    Write-Check ($magic -eq 'GGUF') '文件魔数为 GGUF' $magic
}
else {
    Write-Check $false '模型文件存在' $ModelPath
}

# --- 3. GPU -------------------------------------------------------------
Write-Line ''
Write-Line '3. GPU / 显存'
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
Write-Check ($null -ne $smi) 'nvidia-smi 可用'
if ($smi) {
    $gpuLine = Invoke-NativeCapture -Exe 'nvidia-smi' -Arguments @(
        '--query-gpu=name,memory.total,memory.used,driver_version', '--format=csv,noheader'
    )
    $gpuLine = Get-FirstLines $gpuLine 1
    Write-Check ($gpuLine -match 'NVIDIA') '能读到 GPU 型号 / 显存 / 驱动' $gpuLine
}

if ($hasServer) {
    $devices = Invoke-NativeCapture -Exe $serverExe -Arguments @('--list-devices')
    Write-Check ($devices -match 'CUDA0') 'llama.cpp 识别到 CUDA 设备' (Get-FirstLines $devices 2)
}

# --- 4. 生成冒烟（可选） -------------------------------------------------
Write-Line ''
if ($SkipGenerate) {
    Write-Line '4. 生成冒烟 — 已跳过（-SkipGenerate）' 'DarkGray'
}
else {
    Write-Line '4. 生成冒烟（加载模型到显存，约 5~20 秒）'
    # ⚠️ 括号不能省：`Test-Path $x -and $y` 会让 PS 把 -and 当成 Test-Path 的参数（首版踩到）
    if ($hasCli -and $modelOk) {
        $gen = Invoke-NativeCapture -Exe $cliExe -Arguments @(
            '-m', $ModelPath, '-p', '1+1=', '-n', '8', '-ngl', '99', '-st', '--temp', '0', '--seed', '42'
        )
        # 断言「确实产出了 token」而不是「输出恰好是某句话」——后者随采样波动会假失败（已踩）。
        # 输出片段只作为明细打印，供人眼确认模型没有胡言乱语。
        $genOk = ($gen -match 'Generation:\s*[\d.]+ t/s') -and ($gen -notmatch '(?i)error')
        $answer = ''
        $m = [regex]::Match($gen, '(?s)1\+1=\s*(.{0,60})')
        if ($m.Success) {
            $answer = (($m.Groups[1].Value -split "`r?`n") | Where-Object { $_.Trim() -ne '' } | Select-Object -First 1)
        }
        $speed = if ($gen -match 'Generation:\s*([\d.]+ t/s)') { $Matches[1] } else { 'n/a' }
        Write-Check $genOk '模型可加载并生成输出' ("Generation: {0} | 输出片段: {1}" -f $speed, $answer)
    }
    else {
        Write-Check $false '模型可加载并生成期望输出' 'llama-cli 或模型文件缺失'
    }
}

# --- 汇总 ---------------------------------------------------------------
Write-Line ''
Write-Line ('-' * 64)
if ($script:Failures -eq 0) {
    Write-Line '  ✅ 全部检查通过' 'Green'
    $exitCode = 0
}
else {
    Write-Line ("  ❌ {0} 项检查未通过" -f $script:Failures) 'Red'
    $exitCode = 1
}

if ($ReportPath) {
    $dir = Split-Path -Parent $ReportPath
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    # PS 5.1 的 -Encoding UTF8 就是带 BOM 的 UTF-8，Windows 编辑器打开不乱码
    Set-Content -Path $ReportPath -Value $script:Lines -Encoding UTF8
    Write-Host ("  报告已写入 {0}" -f $ReportPath) -ForegroundColor DarkGray
}

exit $exitCode
