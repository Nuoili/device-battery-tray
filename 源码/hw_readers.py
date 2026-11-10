# -*- coding: utf-8 -*-
"""
硬件读取层：枚举 Windows 外接设备的电池电量。

支持的通道：
  1. 蓝牙 / 蓝牙 LE 设备 —— 读设备属性 DEVPKEY_Bluetooth_Battery（cfgmgr32）
  2. 罗技 2.4G 设备（Unifying / Lightspeed 接收器）—— HID++ 2.0 协议
     · 0x1004 UNIFIED_BATTERY （新固件）
     · 0x1000 BATTERY_LEVEL_STATUS （旧固件）
  其它厂商的 2.4G 私有协议（狼蛛、七彩虹等）Windows 不对外暴露，无法通用读取。
"""
import ctypes
import ctypes.wintypes as wt
import time
import uuid

# ---------------------------------------------------------------- cfgmgr32
_cm = ctypes.WinDLL('cfgmgr32', use_last_error=True)
_cm.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(wt.ULONG), wt.LPCWSTR, wt.ULONG]
_cm.CM_Get_Device_ID_ListW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.ULONG, wt.ULONG]
_cm.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wt.ULONG), wt.LPCWSTR, wt.ULONG]
_cm.CM_Get_DevNode_PropertyW.argtypes = [wt.ULONG, ctypes.c_void_p, ctypes.POINTER(wt.ULONG),
                                         ctypes.c_void_p, ctypes.POINTER(wt.ULONG), wt.ULONG]

CM_GETIDLIST_FILTER_PRESENT = 0x00000100

import re

_BT_SUFFIX = re.compile(
    r'\s*(Hands-Free\s*(AG)?|Avrcp\s*传输|音频|Audio|Sink|Headset|'
    r'Microsoft\s*Bluetooth\s*A2DP|A2DP\s*SNK|\bAG\b)\s*$', re.I)


def clean_name(name):
    """去掉系统附加的服务后缀，让名字短一些。"""
    prev = None
    out = name.strip()
    while prev != out:
        prev = out
        out = _BT_SUFFIX.sub('', out).strip(' -–—|')
    return out or name.strip()


# 蓝牙那一路常常把协议版本挂在名字尾巴上（「AULA-F87 5.0」），2.4G 那一路没有。
# 合成同一台设备时要先把这个尾巴剪掉，否则永远认不出是一把键盘。
_BT_VER = re.compile(r'\s*(?:\(\s*BT\s*\)|[-–—_|\s]\s*BT|\s*[vV]?\d+\.\d+)\s*$', re.I)


def strip_bt_ver(name):
    """去掉名字尾巴上的蓝牙协议版本（「AULA-F87 5.0」→「AULA-F87」）。"""
    base = clean_name(name or '')
    prev = None
    out = base
    while prev != out:
        prev = out
        out = _BT_VER.sub('', out).strip(' -–—|()_')
    return out or base


def merge_key(name):
    """把同一台设备的多个名字归一到同一个 key（用于蓝牙 / 2.4G 合并成一行）。"""
    return strip_bt_ver(name).lower()


def bluetooth_mac(instance):
    """从实例 ID 里提取蓝牙 MAC，用于同一设备的多个服务去重。"""
    m = re.search(r'Dev_([0-9A-Fa-f]{12})', instance, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r'&([0-9A-Fa-f]{12})_C\d{8}', instance, re.I)
    if m:
        return m.group(1).lower()
    return None


class _PROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", wt.DWORD)]


def _pkey(guid_str, pid):
    return _PROPKEY((ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(guid_str).bytes_le), pid)


PKEY_BATTERY = _pkey('{104EA319-6EE2-4701-BD47-8DDBF425BBE5}', 2)   # DEVPKEY_Bluetooth_Battery
PKEY_FRIENDLY = _pkey('{A45C254E-DF1C-4EFD-8020-67D146A850E0}', 14)  # DEVPKEY_Device_FriendlyName
# 蓝牙设备当前是否真的连着。Windows 会把断开前的电量一直缓存着，
# 光看"属性里还有电量"会把已经断开的设备当成在线。
PKEY_IS_CONNECTED = _pkey('{83DA6326-97A6-4088-9453-A1923F573B29}', 15)


# ------------------------------------------------- 经典蓝牙链路状态（bthprops）
# 为什么需要这个：`DEVPKEY_Bluetooth_Battery` 是**缓存**的 —— 经典蓝牙耳机断开后，
# 属性里照样留着断电前的电量，光看属性会把「已经关机的耳机」一直判成在线
# （实测 Beats Studio Buds 断开后仍带着 100%）。bthprops 的 fConnected 是实时的。
# 注意：它只枚举得到经典蓝牙（BTHENUM\DEV_*），BLE-only 设备（BTHLE\DEV_*）看不到，
# 那些设备仍然只能用「属性是否还在」来判断。
_bth = ctypes.WinDLL('bthprops.cpl', use_last_error=True)


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [('wYear', wt.WORD), ('wMonth', wt.WORD), ('wDayOfWeek', wt.WORD),
                ('wDay', wt.WORD), ('wHour', wt.WORD), ('wMinute', wt.WORD),
                ('wSecond', wt.WORD), ('wMilliseconds', wt.WORD)]


class _BT_ADDR(ctypes.Structure):
    _fields_ = [('ullLong', ctypes.c_ulonglong)]


class _BT_DEVICE_INFO(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),
                ('Address', _BT_ADDR),
                ('ulClassofDevice', wt.ULONG),
                ('fConnected', wt.BOOL),
                ('fRemembered', wt.BOOL),
                ('fAuthenticated', wt.BOOL),
                ('stLastSeen', _SYSTEMTIME),
                ('stLastUsed', _SYSTEMTIME),
                ('szName', wt.WCHAR * 248)]


class _BT_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD),
                ('fReturnAuthenticated', wt.BOOL),
                ('fReturnRemembered', wt.BOOL),
                ('fReturnUnknown', wt.BOOL),
                ('fReturnConnected', wt.BOOL),
                ('fIssueInquiry', wt.BOOL),
                ('cTimeoutMultiplier', ctypes.c_ubyte),
                ('hRadio', wt.HANDLE)]


class _BT_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [('dwSize', wt.DWORD)]


try:
    _bth.BluetoothFindFirstRadio.restype = wt.HANDLE
    _bth.BluetoothFindFirstRadio.argtypes = [ctypes.POINTER(_BT_RADIO_PARAMS),
                                             ctypes.POINTER(wt.HANDLE)]
    _bth.BluetoothFindNextRadio.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HANDLE)]
    _bth.BluetoothFindRadioClose.argtypes = [wt.HANDLE]
    _bth.BluetoothFindFirstDevice.restype = wt.HANDLE
    _bth.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(_BT_SEARCH_PARAMS),
                                              ctypes.POINTER(_BT_DEVICE_INFO)]
    _bth.BluetoothFindNextDevice.argtypes = [wt.HANDLE, ctypes.POINTER(_BT_DEVICE_INFO)]
    _bth.BluetoothFindDeviceClose.argtypes = [wt.HANDLE]
    _BTH_OK = True
except Exception:
    _BTH_OK = False


def classic_bluetooth_links():
    """返回 {mac(小写): 是否连着}，只覆盖经典蓝牙设备。

    不发 inquiry（`fIssueInquiry=False`），只列已配对/已认证的，实测 10~30ms，
    可以放进 3 秒一次的轻量探测里。枚举失败返回空字典 —— 调用方必须把「空」当成
    「这次不知道」，不能当成「全都断开了」。
    """
    if not _BTH_OK:
        return {}
    out = {}
    find_radio = None
    try:
        rp = _BT_RADIO_PARAMS(ctypes.sizeof(_BT_RADIO_PARAMS))
        radio = wt.HANDLE()
        find_radio = _bth.BluetoothFindFirstRadio(ctypes.byref(rp), ctypes.byref(radio))
        if not find_radio:
            return {}
        while True:
            sp = _BT_SEARCH_PARAMS(ctypes.sizeof(_BT_SEARCH_PARAMS),
                                   True, True, False, True, False, 0, radio)
            info = _BT_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(_BT_DEVICE_INFO)
            find_dev = _bth.BluetoothFindFirstDevice(ctypes.byref(sp), ctypes.byref(info))
            while find_dev:
                mac = '%012x' % (info.Address.ullLong & 0xFFFFFFFFFFFF)
                out[mac] = bool(info.fConnected)
                info2 = _BT_DEVICE_INFO()
                info2.dwSize = ctypes.sizeof(_BT_DEVICE_INFO)
                if not _bth.BluetoothFindNextDevice(find_dev, ctypes.byref(info2)):
                    break
                info = info2
            if find_dev:
                _bth.BluetoothFindDeviceClose(find_dev)
            nxt = wt.HANDLE()
            if not _bth.BluetoothFindNextRadio(find_radio, ctypes.byref(nxt)):
                break
            radio = nxt
    except Exception:
        return out
    finally:
        if find_radio:
            try:
                _bth.BluetoothFindRadioClose(find_radio)
            except Exception:
                pass
    return out


def _devnode_prop(devinst, key, buflen=512):
    buf = ctypes.create_string_buffer(buflen)
    size = wt.ULONG(buflen)
    ptype = wt.ULONG()
    if _cm.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(ptype),
                                   buf, ctypes.byref(size), 0) != 0:
        return None, None
    return ptype.value, buf.raw[:size.value]


def list_present_instance_ids():
    size = wt.ULONG()
    if _cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), None, CM_GETIDLIST_FILTER_PRESENT) != 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if _cm.CM_Get_Device_ID_ListW(None, buf, size.value, CM_GETIDLIST_FILTER_PRESENT) != 0:
        return []
    return [s for s in buf[:].split('\x00') if s]


def read_bluetooth_batteries():
    """返回所有能在系统属性里读到电量的设备（蓝牙为主）。

    「在线」判定分两条路（2026-11-11 实测结论）：

    · **经典蓝牙**（`BTHENUM\\Dev_*`，A2DP/HFP 耳机音箱那类）走 bthprops 的
      `fConnected` —— 实时、可靠。为什么不能只看属性：`DEVPKEY_Bluetooth_Battery`
      是**缓存**的，耳机断开后属性里还留着断电前的电量（实测 Beats Studio Buds
      断开后仍报 100%），拿它判在线会让已经离线的设备一直挂在面板上。
    · **BLE**（`BTHLE\\Dev_*`）不在 bthprops 的枚举范围里，只能按「属性还在」算在线。
      BLE 断开后节点和属性都会随驱动卸载消失，所以这条仍然成立。

    同一 MAC 会出现在多个服务节点上（主节点 + 各 profile），按 key 聚合，
    只要有一个节点给出「断开」，就以断开为准。
    """
    links = classic_bluetooth_links()
    raw = []
    for inst in list_present_instance_ids():
        devinst = wt.ULONG()
        if _cm.CM_Locate_DevNodeW(ctypes.byref(devinst), inst, 0) != 0:
            continue
        ptype, pbuf = _devnode_prop(devinst, PKEY_BATTERY, 16)
        if pbuf is None:
            continue
        # DEVPROP_TYPE_UINT32 / UINT16 / BYTE 都可能是
        if ptype in (2, 3, 4, 5, 6, 7):     # BYTE / UINT16 / UINT32 系
            level = int.from_bytes(pbuf[:4], 'little')
        else:
            continue
        if not (0 <= level <= 100):
            continue
        _, name_raw = _devnode_prop(devinst, PKEY_FRIENDLY, 512)
        name = name_raw.decode('utf-16-le', 'ignore').split('\x00')[0] if name_raw else ''
        if not name:
            name = inst.split('\\')[1] if '\\' in inst else inst
        mac = bluetooth_mac(inst)
        # 经典链路状态：只有 bthprops 枚举得到的 MAC 才有值，BLE 是 None
        link = links.get(mac) if mac else None
        raw.append({
            'key': ('bt:' + mac) if mac else ('bt:' + inst),
            'name': clean_name(name),
            'level': level,
            'channel': 'bluetooth',
            'instance': inst,
            'status': None,
            'charging': None,      # 系统属性只给电量，给不了充电状态
            'online': True if link is None else bool(link),
            'link_connected': link,
            'ts': time.time(),
        })

    # 同一台设备的多个节点聚合成一条：电量取第一个读到的，在线状态「一断则断」
    merged = {}
    order = []
    for it in raw:
        key = it['key']
        if key not in merged:
            merged[key] = it
            order.append(key)
            continue
        cur = merged[key]
        if cur.get('link_connected') is not False and it.get('link_connected') is False:
            cur['online'] = False
            cur['link_connected'] = False
        # 名字取更像人名的那个（带空格的优先）
        if len(it['name']) > len(cur['name']):
            cur['name'] = it['name']
    return [merged[k] for k in order]



# ---------------------------------------------------------------- HID / SetupAPI
_setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
_hid = ctypes.WinDLL('hid', use_last_error=True)
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)

_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]


class _GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_byte * 8)]


def _guid(s):
    return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


class _IFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", _GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class _IFACE_DETAIL(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("DevicePath", wt.WCHAR * 1024)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT), ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT), ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT), ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT), ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


HID_GUID = _guid('{4D1E55B2-F16F-11CF-88CB-001111000030}')
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _hid_caps(path):
    """返回 (vid, pid, usage_page, out_len)，打不开返回 None。"""
    h = _k32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if not h or h == INVALID_HANDLE:
        return None
    try:
        attrs = _HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        _hid.HidD_GetAttributes(wt.HANDLE(h), ctypes.byref(attrs))
        pp = ctypes.c_void_p()
        caps = _HIDP_CAPS()
        if _hid.HidD_GetPreparsedData(wt.HANDLE(h), ctypes.byref(pp)):
            _hid.HidP_GetCaps(pp, ctypes.byref(caps))
            _hid.HidD_FreePreparsedData(pp)
        return attrs.VendorID, attrs.ProductID, caps.UsagePage, caps.OutputReportByteLength
    finally:
        _k32.CloseHandle(wt.HANDLE(h))


def list_hid_paths():
    h = _setupapi.SetupDiGetClassDevsW(ctypes.byref(HID_GUID), None, None, 0x02 | 0x10)
    if not h or h == INVALID_HANDLE:
        return []
    did = _IFACE_DATA()
    did.cbSize = ctypes.sizeof(did)
    detail = _IFACE_DETAIL()
    detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
    out, i = [], 0
    while _setupapi.SetupDiEnumDeviceInterfaces(
            ctypes.c_void_p(h), None, ctypes.byref(HID_GUID), i, ctypes.byref(did)):
        need = wt.DWORD()
        if _setupapi.SetupDiGetDeviceInterfaceDetailW(
                ctypes.c_void_p(h), ctypes.byref(did), ctypes.byref(detail),
                ctypes.sizeof(detail), ctypes.byref(need), None):
            out.append(detail.DevicePath)
        i += 1
    _setupapi.SetupDiDestroyDeviceInfoList(ctypes.c_void_p(h))
    return out


# ---------------------------------------------------------------- Logitech HID++
LOGITECH_VID = 0x046D
_SWID = 0x0A          # 非 0 才要求设备回包
_HIDPP_LONG = 0x11


class _Hidpp:
    def __init__(self, path):
        import hid
        self.dev = hid.device()
        self.dev.open_path(path.encode('utf-8'))
        self.dev.set_nonblocking(0)
        self.sent = 0
        self.recv = 0
        self.timeout = 500      # 毫秒

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    def _drain(self):
        for _ in range(8):
            if not self.dev.read(64, 6):
                break

    def req(self, dev_idx, feat, func, params=b'\x00\x00\x00', timeout=None):
        # 无线设备空闲后会休眠，首次唤醒可能要几百毫秒；超时给足并重试一次
        if timeout is None:
            timeout = self.timeout
        want = (func << 4) | _SWID
        msg = [_HIDPP_LONG, dev_idx, feat, want] + list(params) + [0] * 13
        for attempt in range(2):
            self._drain()
            try:
                self.dev.write(msg)
            except Exception:
                continue
            self.sent += 1
            deadline = time.time() + timeout / 1000.0
            while time.time() < deadline:
                r = self.dev.read(64, 50)
                if not r:
                    continue
                if r[0] == _HIDPP_LONG and r[1] == dev_idx and r[3] == want:
                    self.recv += 1
                    return r
        return None

    # -- 便捷封装
    def get_feature_index(self, dev_idx, feature_id):
        # 注意：feature ID 在报文中是高字节在前
        r = self.req(dev_idx, 0x00, 0x00, bytes([feature_id >> 8, feature_id & 0xFF, 0x00]))
        return r[4] if r else 0

    def ping(self, dev_idx, timeout=None):
        return self.req(dev_idx, 0x00, 0x01, timeout=timeout) is not None

    def device_name(self, dev_idx):
        p = self._logi_power(dev_idx)
        return p


def _hidpp_logi_power(dev_idx, conn):
    """返回 (level, status, charging) 或 None。

    两个电池特性的回报里都带充电状态：
      0x1004 UNIFIED_BATTERY  func1  [0]=电量% [1]=电量档位 [2]=充电状态 [3]=外接电源
                             充电状态 0 放电 / 1 充电 / 2 快满了 / 3 已充满 / 4 慢充
                             / 5 电池异常 / 6 过热 / 7 充电错误
      0x1000 BATTERY_LEVEL_STATUS func0 [0]=电量% [1]=下一档 [2]=充电状态
                             0 放电 / 1 充电 / 2 已充满 / 3 错误
    """
    # 只枚举一次特性表，结果缓存于 conn 上
    cache = getattr(conn, '_feat_cache', None)
    if cache is None:
        cache = conn._feat_cache = {}
    key = ('tbl', dev_idx)
    table = cache.get(key)
    if table is None:
        fset = conn.get_feature_index(dev_idx, 0x0001)   # FEATURE_SET
        table = {}
        if fset:
            r = conn.req(dev_idx, fset, 0x00)
            count = r[4] if r else 0
            for i in range(1, min(count, 64) + 1):
                rr = conn.req(dev_idx, fset, 0x01, bytes([i, 0x00, 0x00]))
                if rr:
                    table[(rr[4] << 8) | rr[5]] = i
        cache[key] = table
    # 0x1004 UNIFIED_BATTERY -> func 1：byte0 电量、byte1 档位、byte2 充电状态
    idx = table.get(0x1004)
    if idx:
        r = conn.req(dev_idx, idx, 0x01)
        if r and 0 <= r[4] <= 100:
            status = r[6]
            return r[4], status, status in (1, 2, 3, 4)
    # 0x1000 BATTERY_LEVEL_STATUS -> func 0：byte0 电量、byte1 下一档、byte2 充电状态
    idx = table.get(0x1000)
    if idx:
        r = conn.req(dev_idx, idx, 0x00)
        if r and 0 <= r[4] <= 100:
            status = r[6]
            return r[4], status, status in (1, 2)
    return None


def _logi_device_name(conn, dev_idx):
    try:
        idx = conn.get_feature_index(dev_idx, 0x0005)   # DEVICE_NAME
        if not idx:
            return None
        r = conn.req(dev_idx, idx, 0x00)
        length = r[4] if r else 0
        if not length:
            return None
        chars = []
        for pos in range(1, min(length, 32) + 1):
            rr = conn.req(dev_idx, idx, 0x01, bytes([pos, 0x00, 0x00]))
            if not rr:
                break
            chars.append(rr[4])
        name = bytes(chars).decode('utf-8', 'ignore').strip('\x00').strip()
        return name or None
    except Exception:
        return None


def read_logitech_batteries():
    """遍历罗技接收器，读出其下挂设备的电量。"""
    import hid  # 延迟导入，缺失时整体降级
    results = []
    try:
        paths = list_hid_paths()
    except Exception:
        return results
    candidates = []
    for p in paths:
        info = _hid_caps(p)
        if not info:
            continue
        vid, pid, up, out_len = info
        if vid == LOGITECH_VID and up == 0xFF00 and out_len >= 20:
            candidates.append((p, pid))
    seen = set()
    for path, pid in candidates:
        conn = None
        try:
            conn = _Hidpp(path)
        except Exception:
            continue
        try:
            # 先快速探测，全都无响应再给一次长等待（设备可能刚休眠）
            alive = [di for di in (1, 2, 3, 4, 5, 6) if conn.ping(di, timeout=150)]
            if not alive:
                for di in (1, 2, 3, 4, 5, 6):
                    if conn.ping(di, timeout=900):
                        alive.append(di)
                        break
            for dev_idx in alive:
                got = _hidpp_logi_power(dev_idx, conn)
                if not got:
                    continue
                level, status, charging = got
                key = f'logi:{pid:04X}:{dev_idx}'
                if key in seen:
                    continue
                seen.add(key)
                name = _logi_device_name(conn, dev_idx) or f'罗技无线设备 {dev_idx}'
                results.append({
                    'key': key,
                    'name': name,
                    'level': level,
                    'channel': 'logitech',
                    'instance': path,
                    'status': status,
                    'charging': charging,
                    'ts': time.time(),
                })
        finally:
            if conn:
                conn.close()
    return results


# ---------------------------------------------------------------- AULA 2.4G 私有协议
# 狼蛛（AULA）2.4G 接收器 3554:FA09 的 mi_01 col01 暴露一条 UsagePage 0xFF02
# 的 20 字节双向私有通道。报文格式（实测破译，抓包见 源码/协议探测/kbcap.pcap）：
#   [0]  = 0x13  报告 ID
#   [1]  = 0x0A 设备主动通知 / 0x05 主机查询
#   [2..3] = 01 00
#   [4]  = 0x04 电池命令
#   [5]  = 设备槽位（本机键盘固定为 5）
#   [6]  = 电量 %
#   [7]  = 状态标志（bit4=0x10 插着充电线；纯电池供电为 0x01）
#   [19] = 前 19 字节求和校验
# 主机发 [0x13,0x05,0x01,0x00,...] 即可让接收器回一帧电量通知。
AULA_VID = 0x3554
AULA_PID = 0xFA09
AULA_USAGE_PAGE = 0xFF02
_AULA_FLAG_CHARGING = 0x10


def _aula_frame(body):
    b = bytearray(20)
    b[0] = 0x13
    b[1:1 + len(body)] = body
    b[19] = sum(b[:19]) & 0xFF
    return bytes(b)


def _aula_device_name(base):
    """接收器自己只报「2.4G Wireless Receiver」这种通用名，尽量换成真实型号。"""
    generic = ('receiver', 'dongle', '2.4g', 'composite', 'wireless')
    try:
        for inst in list_present_instance_ids():
            if 'VID_3554&PID_FA09' not in inst.upper():
                continue
            devinst = wt.ULONG()
            if _cm.CM_Locate_DevNodeW(ctypes.byref(devinst), inst, 0) != 0:
                continue
            # DEVPKEY_Device_BusReportedDeviceDesc
            key = _pkey('{540B947E-8B40-45BC-A8A2-6A0B894CBDA2}', 4)
            _, raw = _devnode_prop(devinst, key, 512)
            if raw:
                nm = raw.decode('utf-16-le', 'ignore').split('\x00')[0].strip()
                if nm and not any(g in nm.lower() for g in generic):
                    return clean_name(nm)
    except Exception:
        pass
    # 兜底：同一把键盘的蓝牙名（如「AULA-F87 5.0」→「AULA-F87」）
    try:
        for it in read_bluetooth_batteries():
            nm = it['name']
            if nm.upper().startswith('AULA'):
                return re.sub(r'\s+\d+(\.\d+)?$', '', nm).strip()
    except Exception:
        pass
    return base


def read_aula_batteries():
    """查询 AULA 2.4G 接收器下挂设备的电量。"""
    import hid
    results = []
    try:
        paths = list_hid_paths()
    except Exception:
        return results
    target = None
    for p in paths:
        info = _hid_caps(p)
        if not info:
            continue
        vid, pid, up, out_len = info
        if vid == AULA_VID and pid == AULA_PID and up == AULA_USAGE_PAGE and out_len >= 20:
            target = p
            break
    if not target:
        return results

    dev = None
    try:
        dev = hid.device()
        dev.open_path(target.encode('utf-8'))
        dev.set_nonblocking(1)
        found = None
        for attempt in range(3):
            # 清干净上一次的残留
            for _ in range(8):
                if not dev.read(64):
                    break
            try:
                dev.write(_aula_frame([0x05, 0x01, 0x00]))
            except Exception:
                continue
            deadline = time.time() + 0.7
            while time.time() < deadline:
                r = dev.read(64)
                if not r:
                    time.sleep(0.01)
                    continue
                if len(r) >= 20 and r[4] == 0x04 and 0 <= r[6] <= 100:
                    found = bytes(r)
                    break
            if found:
                break
            time.sleep(0.15)
        if not found:
            return results
        level = found[6]
        flag = found[7]
        slot = found[5]
        name = _aula_device_name('AULA 2.4G 键盘')
        results.append({
            'key': f'aula:{slot:02X}',
            'name': name,
            'level': level,
            'channel': 'aula',
            'instance': target,
            'status': flag,
            'charging': bool(flag & _AULA_FLAG_CHARGING),
            'online': True,
            'ts': time.time(),
        })
    except Exception:
        return results
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
    return results


# ---------------------------------------------------------------- 汇总
def scan_all():
    """整轮扫描。返回 (items, ok_channels)。

    ok_channels 是**这次真的读成功**的通道集合 —— 某个读取器抛异常时它就不在里面，
    调用方据此跳过该通道的离线判定，免得一次读取失败把所有设备都判成离线（面板瞬间清空）。
    """
    items = []
    ok = set()
    for channel, reader in (('bluetooth', read_bluetooth_batteries),
                            ('logitech', read_logitech_batteries),
                            ('aula', read_aula_batteries)):
        try:
            items.extend(reader())
            ok.add(channel)
        except Exception:
            pass
    # 同一把键盘在 2.4G 下已经有实时电量时，蓝牙那条只剩过期缓存，隐藏掉
    aula_names = [it['name'].upper() for it in items if it['channel'] == 'aula']
    if aula_names:
        items = [it for it in items
                 if not (it['channel'] == 'bluetooth' and it['online'] is False
                         and any(it['name'].upper().startswith(n) for n in aula_names))]
    # 同名同电量的重复项合并
    uniq = {}
    for it in items:
        k = it['key']
        if k not in uniq:
            uniq[k] = it
    return list(uniq.values()), ok


if __name__ == '__main__':
    t0 = time.time()
    devs, chans = scan_all()
    for d in devs:
        print(f"[{d['level']:3d}%] {d['name']:<32} ({d['channel']})  {d['key']}")
    print(f"通道 {sorted(chans)}  耗时 {time.time() - t0:.2f}s")
