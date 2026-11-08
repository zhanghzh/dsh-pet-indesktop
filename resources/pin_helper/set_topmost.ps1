# 图钉置顶桥：由桌宠（pythonw）在原生 SetWindowPos 被安全软件拒绝时调用。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File set_topmost.ps1 -Hwnd <n> -After (-1|-2)
# 用字节数组 Load 而非 LoadFrom：后者对本机路径也会抛 0x80131515（不支持的操作）。
param([long]$Hwnd, [int]$After = -1)
$dll = Join-Path $PSScriptRoot 'pin_top.dll'
[Reflection.Assembly]::Load([IO.File]::ReadAllBytes($dll)) | Out-Null
Write-Output "DBG hwnd=$Hwnd after=$After"
[PinTop]::SetWindowPos([IntPtr]$Hwnd, [IntPtr]$After, 0, 0, 0, 0, 0x13) | Out-Null
