# -*- coding: utf-8 -*-
"""创建 Windows 快捷方式（.lnk），纯 ctypes + COM，不依赖 pywin32。

为什么要自己写：这个程序是单文件 exe，PyInstaller 打包时不会带 pywin32，
而 .lnk 只能通过 COM 的 IShellLink 生成。这里手动取 vtable 调它，
打出来的 exe 一样能用。

用法：
    from shortcut import create_shortcut
    create_shortcut(r'C:\\Users\\me\\Desktop\\Device Battery.lnk', r'D:\\x\\Device Battery.exe')
"""
import ctypes
import os
from ctypes import wintypes

_ole32 = ctypes.WinDLL('ole32', use_last_error=True)

CLSCTX_INPROC_SERVER = 0x1

# IShellLinkW 的 {000214F9-0000-0000-C000-000000000046}
# CLSID_ShellLink {00021401-0000-0000-C000-000000000046}
# IPersistFile   {0000010B-0000-0000-C000-000000000046}


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    def __init__(self, d1, d2, d3, d4):
        super().__init__(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


CLSID_ShellLink = GUID(0x00021401, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IShellLinkW = GUID(0x000214F9, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IPersistFile = GUID(0x0000010B, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))

# 各方法在 vtable 里的槽位（IUnknown 占前 3 个）
_VT_SET_DESCRIPTION = 7
_VT_SET_WORKDIR = 9
_VT_SET_ARGS = 11
_VT_SET_ICON = 17
_VT_SET_PATH = 20
_VT_SAVE = 6            # IPersistFile::Save

_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoCreateInstance.restype = ctypes.c_long


def _vt_call(ptr, index, restype, argtypes, *args):
    """按 vtable 槽位调用 COM 方法。ptr 是接口指针。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def create_shortcut(lnk_path, target, workdir=None, args='', icon=None,
                    description=''):
    """在 lnk_path 建一个指向 target 的快捷方式，成功返回 True。"""
    if os.path.exists(lnk_path):
        try:
            os.remove(lnk_path)          # 覆盖：先删掉旧的，避免 IPersistFile 拒绝
        except OSError:
            return False
    os.makedirs(os.path.dirname(lnk_path) or '.', exist_ok=True)

    hr = _ole32.CoInitializeEx(None, 0x2)      # STA
    com_ok = hr in (0, 1)                      # S_OK / S_FALSE

    psl = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_ShellLink), None,
                                 CLSCTX_INPROC_SERVER,
                                 ctypes.byref(IID_IShellLinkW),
                                 ctypes.byref(psl))
    if hr != 0 or not psl:
        if com_ok:
            _ole32.CoUninitialize()
        return False

    try:
        _vt_call(psl, _VT_SET_PATH, ctypes.c_long, [wintypes.LPCWSTR],
                 str(target))
        if args:
            _vt_call(psl, _VT_SET_ARGS, ctypes.c_long, [wintypes.LPCWSTR], args)
        _vt_call(psl, _VT_SET_WORKDIR, ctypes.c_long, [wintypes.LPCWSTR],
                 workdir or os.path.dirname(str(target)))
        if description:
            _vt_call(psl, _VT_SET_DESCRIPTION, ctypes.c_long,
                     [wintypes.LPCWSTR], description)
        if icon:
            _vt_call(psl, _VT_SET_ICON, ctypes.c_long,
                     [wintypes.LPCWSTR, ctypes.c_int], str(icon), 0)

        pf = ctypes.c_void_p()
        hr = _vt_call(psl, 0, ctypes.c_long,          # IUnknown::QueryInterface
                      [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                      ctypes.byref(IID_IPersistFile), ctypes.byref(pf))
        if hr != 0 or not pf:
            return False
        try:
            hr = _vt_call(pf, _VT_SAVE, ctypes.c_long,
                          [wintypes.LPCWSTR, wintypes.BOOL], str(lnk_path), True)
            return hr == 0
        finally:
            _vt_call(pf, 2, ctypes.c_ulong, [])       # Release
    finally:
        _vt_call(psl, 2, ctypes.c_ulong, [])
        if com_ok:
            _ole32.CoUninitialize()
