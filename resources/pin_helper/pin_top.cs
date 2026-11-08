// 图钉置顶辅助库：SetWindowPos 的最小封装。
// 为什么存在：本机安全软件按进程二进制拦截 Python 的 TOPMOST-ification
// （SetWindowPos err=1400 / SetWindowLong 位被即时剥离；移动与普通
// z-order 提升不受影响），PowerShell 进程不受拦。桌宠在原生路径被拒时
// 通过 PowerShell + 本库完成置顶。源码随库分发（resources/pin_helper/）。
using System;
using System.Runtime.InteropServices;

public class PinTop
{
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after,
        int x, int y, int cx, int cy, uint flags);
}
