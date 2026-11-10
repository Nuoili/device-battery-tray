# -*- coding: utf-8 -*-
"""
Device Battery —— 常驻系统托盘，显示外接无线设备的电量。

工作方式：
  · 启动即采样一次，之后按「检测间隔」读真实电量（默认 60 分钟）
  · 3 秒一次轻量探测：新接入 / 掉线的设备实时反映到面板
  · 两次采样之间，用历史掉电速率线性外推，图标上的数字是估算值
  · 只保留一个托盘图标 + 一个定时线程，无窗口、无后台轮询

命令行：
  Device Battery.exe            正常启动（有窗口即托盘）
  Device Battery.exe --tray     静默启动（供开机自启使用）
  Device Battery.exe --once     只读一次并打印，不驻留（排查用）
"""
import argparse
import ctypes
import logging
import os
import sys
import threading
import time
import unicodedata
from pathlib import Path

import pystray
from pystray import Menu, MenuItem as Item

import hw_readers
import icons
import popup
import state_store

APP_TITLE = 'Device Battery'          # 软件名（托盘提示、菜单、窗口标题、文件名都跟它走）
APP_VERSION = '1.0.6'
# 开机自启的注册表值名。v1.0.5 起统一成产品名，旧版留下的值会在启动时自动迁过来。
APP_REG_NAME = 'Device Battery'
LEGACY_REG_NAMES = ('DeviceBatteryTray',)
MUTEX_NAME = 'DeviceBatteryTray_SingleInstance'
# 第二次双击时用来叫醒已经在跑的那个实例：它 wait 这个命名事件，收到就把面板弹出来
ACTIVATE_EVENT = 'DeviceBatteryTray_ShowPanel'
EVENT_MODIFY_STATE = 0x0002

# 必须 use_last_error=True —— 否则 ctypes.get_last_error() 永远是 0，
# CreateMutexW 的 ERROR_ALREADY_EXISTS 判不出来，单实例形同虚设（双击会出现两份）。
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)

# 低电量提醒：一轮放电最多三条 —— <25%、<15%、掉线；每条 5 秒后自动收回
LOW_BATTERY = icons.WARN_LEVEL      # 25%
CRITICAL_BATTERY = 15               # 15%
LOW_RECOVER = 2                     # 回升到 27% 以上才清零重来，免得在阈值上反复抖
NOTIFY_GAP = 20                     # 多台设备同时低电时，气泡至少隔 20 秒
NOTIFY_SECONDS = 5                  # 气泡显示时长

# 轻量探测周期：只读系统缓存里的蓝牙电量 + 枚举 HID 接口路径，不往设备发请求。
# 实测一轮 10~20ms（蓝牙属性 13ms + 经典链路 3ms + HID 接口枚举 5ms 上下），
# 所以放到 3 秒一次 —— 用户要求「设备断开 3 秒内必须从面板消失」。
# 整轮扫描（含罗技 HID++、AULA 私有协议）仍按「检测间隔」走，别把这个调太短。
PROBE_SECONDS = 3
CHARGE_HOLD = 90 * 60               # 探到一次上涨后，闪电再亮 90 分钟（充电时电量常是台阶式上报）
CHARGE_MAX_JUMP = 20                # 一次涨超过 20% 视为读数抽风（比如耳机 10%→100%），不算充电

log = logging.getLogger('battery')
_MUTEX_HANDLE = None       # 单实例互斥体句柄，必须常驻，不能被回收


def _dw(text):
    """粗略的中日韩宽度：全角算 2，半角算 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
               for ch in text)


def pad_name(name, width=22):
    """按显示宽度补齐/截断设备名，让原生菜单里的电池块尽量对齐。"""
    w = _dw(name)
    if w <= width:
        return name + ' ' * (width - w)
    out, used = '', 0
    for ch in name:
        cw = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        if used + cw > width - 2:
            break
        out += ch
        used += cw
    return out + '…' + ' ' * max(0, width - used - 1)


# ----------------------------------------------------------------- 开机自启
def _run_key():
    import winreg
    return winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run'


def autostart_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" --tray'
    script = os.path.abspath(sys.argv[0])
    if not script.lower().endswith('.py'):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'battery_tray.py')
    return f'"{sys.executable}" "{script}" --tray'


def _read_run_values():
    """读取 HKCU Run 下与本程序相关的值：{值名: 命令行}。"""
    import winreg
    hive, path = _run_key()
    out = {}
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            for name in (APP_REG_NAME,) + LEGACY_REG_NAMES:
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    continue
                out[name] = val
    except FileNotFoundError:
        pass
    return out


def migrate_autostart():
    """把旧版本遗留的自启值改到新名字下。

    v1.0.5 把注册表值名从 `DeviceBatteryTray` 换成了 `Device Battery`。
    不迁移的话，升级后自启会失效（新名字下没值），任务管理器里还留一条
    指向旧 exe 的死项。
    """
    import winreg
    found = _read_run_values()
    legacy = [n for n in LEGACY_REG_NAMES if n in found]
    if not legacy or APP_REG_NAME in found:
        return False
    hive, path = _run_key()
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_REG_NAME, 0, winreg.REG_SZ, autostart_command())
            for name in legacy:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
        log.info('自启项已迁移：%s -> %s', '/'.join(legacy), APP_REG_NAME)
        return True
    except Exception as e:
        log.warning('迁移自启项失败: %s', e)
        return False


def is_autostart_enabled():
    try:
        return bool(_read_run_values())
    except Exception:
        return False


def set_autostart(enabled):
    try:
        import winreg
        hive, path = _run_key()
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_REG_NAME, 0, winreg.REG_SZ, autostart_command())
            for name in LEGACY_REG_NAMES:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
            if not enabled:
                try:
                    winreg.DeleteValue(k, APP_REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log.warning('设置开机自启失败: %s', e)
        return False


# ----------------------------------------------------------------- 快捷方式
def _shortcut_target():
    """快捷方式指向什么：打包后就是 exe 自己，源码运行时指 pythonw + 脚本。"""
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        return exe, '', os.path.dirname(exe)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'battery_tray.py')
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), 'pythonw.exe')
    if os.path.exists(pyw):
        py = pyw
    return py, f'"{script}" --tray', os.path.dirname(script)


def shortcut_paths():
    """桌面 + 开始菜单两张「Device Battery.lnk」的完整路径。

    桌面走系统已知文件夹：桌面被重定向到别的盘（OneDrive / 自定义位置）时，
    写 %USERPROFILE%\\Desktop 会落到一个用户根本看不见的空目录。
    """
    import shortcut
    name = APP_TITLE + '.lnk'
    desktop = shortcut.desktop_dir()
    programs = shortcut.programs_dir()
    return [os.path.join(desktop, name), os.path.join(programs, name)]


def create_shortcuts():
    """在桌面和开始菜单建快捷方式，名字统一叫 Device Battery。返回 (成功, 失败)。"""
    import shortcut
    target, args, workdir = _shortcut_target()
    made, failed = [], []
    for lnk in shortcut_paths():
        try:
            if shortcut.create_shortcut(lnk, target, workdir=workdir, args=args,
                                        icon=target, description=APP_TITLE):
                made.append(lnk)
            else:
                failed.append(lnk)
        except Exception:
            log.debug('创建快捷方式失败: %s', lnk, exc_info=True)
            failed.append(lnk)
    return made, failed


def ensure_shortcuts(cfg):
    """首次运行（或换版本后第一次运行）静默补一张桌面快捷方式。

    只补桌面，开始菜单那张交给菜单里的手动项；用户自己删掉了就删掉了，
    同一个版本内不再反复重建，换新版本时再补一次。
    返回是否有新建动作。
    """
    if cfg.get('shortcut_version') == APP_VERSION:
        return False
    try:
        import shortcut
        desktop_lnk = shortcut_paths()[0]
        if os.path.exists(desktop_lnk):
            cfg['shortcut_version'] = APP_VERSION
            state_store.save_config(cfg)
            return False
    except Exception:
        log.debug('检查桌面快捷方式失败', exc_info=True)
        return False
    made, failed = create_shortcuts()
    if made:
        log.info('已自动创建快捷方式：%s', ' | '.join(made))
    if failed:
        log.debug('自动创建快捷方式失败：%s', ' | '.join(failed))
    cfg['shortcut_version'] = APP_VERSION
    try:
        state_store.save_config(cfg)
    except Exception:
        log.debug('记录快捷方式标记失败', exc_info=True)
    return bool(made)


def _clean_hist(dev):
    """取出干净的历史样本 [[时间戳, 电量], ...]，丢掉残缺项。"""
    return [h for h in (dev.get('history') or [])
            if isinstance(h, (list, tuple)) and len(h) == 2 and h[1] is not None]


def _last_rise_ts(hist):
    """最近一次"像充电"的上涨发生在什么时候。

    只认涨幅小、间隔不长的相邻样本：涨太猛（读数抽风）、已经满格、掉电都不算。
    """
    for i in range(len(hist) - 1, 0, -1):
        t1, l1 = hist[i - 1]
        t2, l2 = hist[i]
        span = t2 - t1
        if span <= 0 or span > 6 * 3600:
            continue
        delta = l2 - l1
        if 0 < delta <= CHARGE_MAX_JUMP and l2 < 100:
            return t2
    return None


def _charging_state(dev, now=None):
    """判断是否在充电。

    1. 设备自己报了充电状态（罗技 HID++ 能报）→ 直接用；
    2. 报不了（蓝牙设备都是这种）→ 看最近 90 分钟内电量涨过没有，涨过就当充电中。
       最新一次变化是掉电时立刻熄灯，免得拔线后闪电还赖着。
    """
    val = dev.get('charging')
    if val is not None:
        return bool(val)
    now = now or time.time()
    hist = _clean_hist(dev)
    if len(hist) < 2:
        return False
    (t1, l1), (t2, l2) = hist[-2], hist[-1]
    if l2 < l1 and (now - t2) <= CHARGE_HOLD:      # 最近刚掉过电 = 在放电
        return False
    ts = _last_rise_ts(hist)
    return ts is not None and (now - ts) <= CHARGE_HOLD


# 同一台设备的多条链路（蓝牙 / 2.4G）合并成一行时，谁的电量更值得信：
# 2.4G 能顺手拿到充电状态、响应也快，蓝牙只能读系统缓存属性，所以同在线时优先 2.4G。
_CHANNEL_RANK = {'aula': 0, 'logitech': 1, 'bluetooth': 2}

# 「这条链路现在还在应答」的有效期（秒）。超过就当它当前没在用，把显示让给另一条。
#   · aula：probe 每 3 秒向接收器要一次电量（实测 26~43ms），留 2 次失败余量，
#     所以键盘拨到蓝牙后最多 8 秒，面板这行就会跟着换成蓝牙那一路；
#   · logitech：只在整轮扫描时读（默认 60 分钟一轮），窗口跟着放宽；
#   · bluetooth：属性 3 秒一读，留 3 次失败余量。
# 为什么要这么判：蓝牙的 `DEVPKEY_Bluetooth_Battery` 是**缓存**的，配对设备即使
# 早就切回 2.4G，属性里还留着上次的电量，光看「在线」永远是 True。只有
# 「接收器此刻还回不回数据」才能说明键盘正走哪条链路。
_LINK_FRESH_SECONDS = {'aula': 8, 'logitech': 4000, 'bluetooth': 10}


def _link_fresh(row):
    """这条链路最近还在真实应答吗（而不是只剩一份缓存的旧读数）。"""
    window = _LINK_FRESH_SECONDS.get(row.get('channel') or '')
    if window is None:
        return False
    return (row.get('age') or 0) <= window


def _merge_same_device(rows):
    """把「AULA-F87」和「AULA-F87 5.0」这种同一台设备的两条链路合成一行。

    双模键鼠（如 AULA F87）会同时出现在两条通道上：2.4G 接收器一路、蓝牙一路。
    面板只该显示**当前正在用的那条链路**，规则按优先级：

      1. 在线的优先于离线的；
      2. 都算在线时，取「最近还在应答」的那条 —— 键盘拨到蓝牙后，2.4G 接收器
         就再也回不出数据，这一路随即让位给蓝牙；拨回 2.4G 则反过来；
      3. 两条都在应答，才按通道可信度取 2.4G（它能顺手给出充电状态）。

    这样面板上既不会「一台键盘两行」，也不会显示一条早就没在用的链路的旧电量。
    """
    merged = {}
    order = []
    for r in rows:
        k = hw_readers.merge_key(r['name'])
        cur = merged.get(k)
        if cur is None:
            merged[k] = r
            order.append(k)
            continue
        merged[k] = _better_row(cur, r)
    out = []
    for k in order:
        r = merged[k]
        r['name'] = hw_readers.strip_bt_ver(r['name'])
        out.append(r)
    return out


def _better_row(a, b):
    if a['online'] != b['online']:
        return a if a['online'] else b
    fa = _link_fresh(a)
    fb = _link_fresh(b)
    if fa != fb:
        return a if fa else b
    ra = _CHANNEL_RANK.get(a.get('channel'), 9)
    rb = _CHANNEL_RANK.get(b.get('channel'), 9)
    if ra != rb:
        return a if ra < rb else b
    return a if (a.get('age') or 0) <= (b.get('age') or 0) else b


# ----------------------------------------------------------------- 主程序
class BatteryTray:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.icon = None
        self.panel = popup.DevicePanel()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.last_scan_ts = None
        self.scan_error = None
        self.scanning = False
        self._last_icon_key = None
        self._last_click = 0.0
        self._low_stage = {}        # 每台设备提醒到第几档：0 未提醒 / 1 报过25 / 2 报过15 / 3 报过离线
        self._notify_gen = 0        # 气泡代次，避免旧气泡把新气泡关掉
        self._last_notify = 0.0
        self._hid_paths = None      # 上次探测到的 HID 接口集合，用来感知插拔
        self._scan_requested = False  # 轻量探测发现新接口 → 请求立刻整轮扫描
        self._hid_removed = False     # 接口是「变少」触发的扫描 → 允许 HID 通道判离线
        self._hev = None             # 唤起事件句柄

    # -- 采样 ---------------------------------------------------------
    # 通道离线策略：
    #   · bluetooth —— 每轮都判。探测拿的是全量快照，链路断开/属性消失都是硬证据。
    #   · logitech / aula —— 平时**不判离线**。它们的读数是往接收器发 HID++ / 私有报文，
    #     设备休眠时应答不上来（鼠标不动几分钟就睡），这时读不到 ≠ 设备没了；
    #     真拔了接收器，HID 接口集合会少一条，probe 会发现并触发 trust_hid=True 的扫描，
    #     那种情况下「读不到」才是真的不在了。
    ALWAYS_OFFLINE_CHANNELS = ('bluetooth',)

    def scan(self, trust_hid=False):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            items, ok_channels = hw_readers.scan_all()
            now = time.time()
            with self.lock:
                state_store.update_levels(self.state, items, now)
                # 只对「这次真读成功」的通道判离线：某个读取器报错时它的设备原样保留，
                # 否则一次异常会把整套设备从面板上抹掉。
                for ch in ok_channels:
                    if ch not in self.ALWAYS_OFFLINE_CHANNELS and not trust_hid:
                        continue
                    live = [i['key'] for i in items if i.get('channel') == ch]
                    state_store.mark_offline(self.state, live, ch, now)
                state_store.save_state(self.state)
                self.last_scan_ts = now
                self.scan_error = None
            log.info('采样完成：%d 个设备（通道 %s%s）', len(items),
                     '/'.join(sorted(ok_channels)), '，含 HID 拔插' if trust_hid else '')
        except Exception as e:
            log.exception('采样失败')
            with self.lock:
                self.scan_error = str(e)
        finally:
            with self.lock:
                self.scanning = False
            self.refresh_display(force=True)

    def probe(self):
        """轻量探测（3 秒一次）：蓝牙快照 + AULA 2.4G 应答 + HID 接口集合。

        **两条结果分开返回** `(levels_changed, roster_changed)`：
          · roster_changed —— 设备增删（新接入 / 掉线 / 重新连上），面板要重建列表
          · levels_changed —— 只有电量数字变了，面板只需要改数字，别重建窗口

        和整轮扫描的区别：
          · 蓝牙只读系统缓存属性 + 经典链路状态，不往设备发请求，3 秒一次不打扰休眠
          · AULA 2.4G 只向接收器要一次电量（实测 26~43ms），不唤醒键盘
          · HID 只枚举接口路径（微秒级），不读数、不跟罗技握手
        """
        levels = False
        roster = False

        # ---- 蓝牙：全量快照，没出现在里的就算离线（只影响 bluetooth 通道）----
        try:
            items = hw_readers.read_bluetooth_batteries()
        except Exception:
            log.debug('蓝牙探测失败', exc_info=True)
            items = None
        if items is not None:
            now = time.time()
            with self.lock:
                res = state_store.update_levels(self.state, items, now)
                off = state_store.mark_offline(
                    self.state, [i['key'] for i in items], 'bluetooth', now)
                done = set(off) | set(res['offline'])
                if res['new'] or res['revived'] or done:
                    roster = True
                if res['level'] or res['revived']:
                    levels = True
                if roster or levels:
                    state_store.save_state(self.state)
                devices = self.state.get('devices') or {}
                for key in res['new']:
                    dev = devices.get(key) or {}
                    log.info('设备接入：%s %s%%', dev.get('name'), dev.get('last_level'))
                for key in res['revived']:
                    dev = devices.get(key) or {}
                    log.info('设备重新连上：%s %s%%', dev.get('name'), dev.get('last_level'))
                for key in done:
                    dev = devices.get(key) or {}
                    log.info('设备离线：%s', dev.get('name', key))
                for key in res['level']:
                    dev = devices.get(key) or {}
                    log.debug('电量变化：%s %s%%', dev.get('name'), dev.get('last_level'))

        # ---- AULA 2.4G：接收器直接应答（实测 26~43ms），和蓝牙同一个节奏读 ----
        # 目的不只是刷电量，更重要是判断**这条链路此刻还在不在用**：
        # 双模键盘拨到蓝牙后，接收器就再也回不出数据，这一路随即让位给蓝牙。
        # 读不到时**不判离线** —— 键盘休眠时接收器同样不回话，那不代表它不在了。
        try:
            aula_items = hw_readers.read_aula_batteries()
        except Exception:
            aula_items = None
            log.debug('AULA 探测失败', exc_info=True)
        if aula_items:
            with self.lock:
                res = state_store.update_levels(self.state, aula_items,
                                               time.time())
                if res['new'] or res['revived'] or res['level']:
                    roster = roster or bool(res['new'] or res['revived'])
                    levels = levels or bool(res['level'])
                    state_store.save_state(self.state)

        # ---- HID：接口集合变了就说明插拔过接收器，立刻做一次整轮扫描 ----
        try:
            paths = set(hw_readers.list_hid_paths())
        except Exception:
            paths = None
        if paths is not None:
            if self._hid_paths is None:
                self._hid_paths = paths
            elif paths != self._hid_paths:
                added = len(paths - self._hid_paths)
                removed = len(self._hid_paths - paths)
                self._hid_paths = paths
                if added or removed:
                    log.info('HID 接口变化：+%d -%d，触发整轮采样', added, removed)
                    self._scan_requested = True
                    # 接口变少才可能是拔了接收器；这时「读不到」才算真的不在了
                    self._hid_removed = self._hid_removed or removed > 0
                    roster = True
        return levels, roster

    # -- 展示 ---------------------------------------------------------
    def device_rows(self):
        """返回 [(显示名, 电量或None, 是否估算, 离线, 上次采样距今秒数, 设备dict)]，按电量升序。"""
        now = time.time()
        rows = []
        with self.lock:
            for key, dev in (self.state.get('devices') or {}).items():
                if not dev.get('name'):
                    continue
                # 离线（默认 90 秒宽限）的设备不进列表：面板/托盘提示/菜单同一套数据，
                # 所以判定只写在这里一处，收起来就全都收了。再连上会自然回来。
                if state_store.is_stale(dev, now):
                    continue
                val, est = state_store.estimate(dev, now)
                age = now - (state_store.last_online_ts(dev) or now)
                charging = _charging_state(dev, now) and bool(dev.get('online', True))
                if charging and dev.get('last_level') is not None:
                    # 充电时不要再按掉电速率外推，会把电量越算越低
                    val, est = dev.get('last_level'), False
                rows.append({
                    'key': key,
                    'name': dev.get('name', key),
                    'level': val,
                    'raw': dev.get('last_level'),
                    'estimated': est,
                    'online': bool(dev.get('online', True)),
                    'age': age,
                    'channel': dev.get('channel', ''),
                    'charging': charging,
                })
        rows = _merge_same_device(rows)
        rows.sort(key=lambda r: (not r['online'], r['level'] if r['level'] is not None else 999))
        return rows

    def headline(self, rows):
        online = [r for r in rows if r['online'] and r['level'] is not None]
        if online:
            return min(online, key=lambda r: r['level'])
        return None

    def tooltip(self, rows):
        if not rows:
            return f'{APP_TITLE}：未发现可读取电量的设备'
        parts = []
        for r in rows[:5]:
            if r['level'] is None:
                continue
            mark = '≈' if r['estimated'] else ''
            name = r['name'] if len(r['name']) <= 20 else r['name'][:19] + '…'
            tail = '' if r['online'] else '（未连接）'
            bolt = '⚡' if r.get('charging') else ''
            parts.append(f'{name} {mark}{int(round(r["level"]))}%{bolt}{tail}')
        text = ' | '.join(parts)
        if len(rows) > 5:
            text += f' 等 {len(rows)} 个'
        if self.scanning:
            text = '正在读取… ' + text
        return (text or APP_TITLE)[:120]

    def refresh_display(self, force=False):
        if not self.icon:
            return
        rows = self.device_rows()
        head = self.headline(rows)
        if head and head['level'] is not None:
            shown = int(round(head['level']))
        else:
            shown = None
        key = (shown, self.tooltip(rows))
        if force or key != self._last_icon_key:
            self._last_icon_key = key
            try:
                self.icon.icon = icons.render_icon(shown)
                self.icon.title = self.tooltip(rows)
                self.icon.update_menu()
            except Exception:
                log.debug('刷新托盘失败', exc_info=True)
        # 面板自己分流：设备增删才重建窗口，纯电量变化只重绘内容（不闪）。
        try:
            self.panel.refresh_if_visible(rows, self.updated_text())
        except Exception:
            log.debug('刷新面板失败', exc_info=True)

    # -- 低电量气泡 ---------------------------------------------------
    def check_low_battery(self, rows):
        """三段式提醒：跌破 25% 一次、跌破 15% 再一次、掉线（多半是耗尽了）最后再一次。

        每台设备各自记一个 stage，只往前走不回退；只有电量回血到 27% 以上才清零，
        这样一轮放电最多弹三条，不会再刷屏。
        """
        now = time.time()
        for r in rows:
            key = r['key']
            level = r['level']
            stage = self._low_stage.get(key, 0)

            # 正在充电：不弹任何气泡，并把流水线归零 —— 拔线再掉下来时重新走三段
            if r.get('charging'):
                if stage:
                    self._low_stage[key] = 0
                continue

            # 充上电了：整条流水线重置，下一次掉下来重新走三段
            if level is not None and r['online'] and level >= LOW_BATTERY + LOW_RECOVER:
                if stage:
                    self._low_stage[key] = 0
                continue

            if not r['online']:
                # 蓝牙设备的"离线"是日常（关机、耳机进仓、切有线），拿它当"电用完了"会天天误报
                if r.get('channel') == 'bluetooth':
                    continue
                # 先报过低电量、随后掉了线 —— 基本就是撑到没电了，最后提醒一次
                if stage in (1, 2) and now - self._last_notify >= NOTIFY_GAP:
                    self._low_stage[key] = 3
                    self._last_notify = now
                    if not self._notify(f'{r["name"]} 已离线，可能是电用完了。',
                                        '设备离线'):
                        self._low_stage[key] = stage       # 没弹出去就退回去
                continue

            if level is None:
                continue

            if level < CRITICAL_BATTERY and stage < 2:
                target = 2
                msg = f'{r["name"]} 只剩 {int(round(level))}%，快见底了。'
                title = '电量不足'
            elif level < LOW_BATTERY and stage < 1:
                target = 1
                msg = f'{r["name"]} 只剩 {int(round(level))}%，该充电了。'
                title = '电量偏低'
            else:
                continue

            if now - self._last_notify < NOTIFY_GAP:      # 几台一起低电时排队，别连炸
                continue
            self._low_stage[key] = target
            self._last_notify = now
            if not self._notify(msg, title):
                self._low_stage[key] = stage              # 没弹出去就退回去

    def _notify(self, msg, title):
        """弹一条托盘气泡，返回是否真的弹出去了。"""
        if not self.icon:
            return False
        self._notify_gen += 1
        gen = self._notify_gen
        try:
            self.icon.notify(msg, title)
        except Exception:
            log.debug('气泡失败', exc_info=True)
            return False
        threading.Thread(target=self._close_notification, args=(gen,),
                         daemon=True).start()
        return True

    def _close_notification(self, gen):
        """到点收气泡：Windows 的托盘气泡默认不会自己消失。"""
        time.sleep(NOTIFY_SECONDS)
        if gen != self._notify_gen:
            return                                  # 已经被新气泡顶替
        try:
            self.icon.remove_notification()
        except Exception:
            log.debug('收起气泡失败', exc_info=True)

    def updated_text(self):
        if not self.last_scan_ts:
            return '正在读取…'
        age = time.time() - self.last_scan_ts
        if age < 90:
            return '刚刚更新'
        return state_store.describe_age(age) + '更新'

    # -- 面板 ---------------------------------------------------------
    def on_show_panel(self, icon=None, item=None):
        """左键单击托盘图标 / 菜单第一项：开合竖列面板。"""
        now = time.time()
        if now - self._last_click < 0.3:      # 双击只算一次
            return
        self._last_click = now
        try:
            self.panel.toggle(self.device_rows(), self.updated_text(),
                              footer='点面板外可收起')
        except Exception:
            log.exception('弹出面板失败')

    # -- 被第二次启动唤起 ---------------------------------------------
    def start_activate_listener(self):
        """建一个命名事件，等"第二次双击"的进程来叫醒我们。

        为什么用事件而不是再开一个隐藏窗口收消息：不用注册窗口类、不用写 WndProc，
        一个 WaitForSingleObject 就够，出错的面小得多。
        """
        _k32.CreateEventW.restype = ctypes.c_void_p
        _k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_wchar_p]
        self._hev = _k32.CreateEventW(None, False, False, ACTIVATE_EVENT)
        if not self._hev:
            log.warning('唤起事件创建失败，重复双击只能弹提示')
            return
        threading.Thread(target=self._wait_activate, name='activate', daemon=True).start()

    def _wait_activate(self):
        _k32.WaitForSingleObject.restype = ctypes.c_ulong
        _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        while not self.stop_event.is_set():
            if _k32.WaitForSingleObject(self._hev, 1000) == 0:   # WAIT_OBJECT_0
                try:
                    self.on_activate()
                except Exception:
                    log.exception('唤起面板失败')

    def on_activate(self):
        """重复双击时：面板没开就弹出来，已经开着就刷新并重新置顶。"""
        log.info('收到唤起请求')
        self.panel.show(self.device_rows(), self.updated_text(),
                        footer='点面板外可收起')
        # 顺手补一次采样，让打开的面板不至于显示十几分钟前的旧数
        if not self.last_scan_ts or time.time() - self.last_scan_ts > 120:
            threading.Thread(target=self.scan, daemon=True).start()

    # -- 菜单 ---------------------------------------------------------
    def menu_items(self):
        rows = self.device_rows()
        items = [
            Item('显示电量面板', self.on_show_panel, default=True),
            Menu.SEPARATOR,
        ]
        if not rows:
            items.append(Item('未发现可读取的设备', None, enabled=False))
        for r in rows:
            glyph = icons.battery_glyph(r['level'])
            if r['level'] is None:
                text = f'{pad_name(r["name"])}  {glyph}      —'
            else:
                mark = '≈' if r['estimated'] else ' '
                text = f'{pad_name(r["name"])}  {glyph}  {mark}{int(round(r["level"]))}%'
                if r.get('charging'):
                    text += ' ⚡'
                if not r['online']:
                    text += '   未连接'
            items.append(Item(text, self._noop))
        items.append(Menu.SEPARATOR)
        items.append(Item('立即刷新', self.on_refresh))
        interval = max(5, int(self.cfg.get('interval_minutes', 60)))
        items.append(Item(f'检测间隔 {interval} 分钟', self.on_change_interval))
        items.append(Item('开机自启', self.on_toggle_autostart,
                          checked=lambda item: is_autostart_enabled()))
        items.append(Item('创建快捷方式（桌面 + 开始菜单）',
                          self.on_create_shortcuts))
        items.append(Menu.SEPARATOR)
        items.append(Item(f'版本 v{APP_VERSION}', self._noop))
        items.append(Item('退出', self.on_quit))
        return items

    def _noop(self, icon=None, item=None):
        pass

    def on_refresh(self, icon=None, item=None):
        threading.Thread(target=self.scan, daemon=True).start()

    def on_change_interval(self, icon=None, item=None):
        options = [15, 30, 60, 120, 180]
        cur = int(self.cfg.get('interval_minutes', 60))
        try:
            nxt = options[(options.index(cur) + 1) % len(options)] if cur in options else 60
        except ValueError:
            nxt = 60
        self.cfg['interval_minutes'] = nxt
        state_store.save_config(self.cfg)
        self.refresh_display(force=True)

    def on_toggle_autostart(self, icon=None, item=None):
        set_autostart(not is_autostart_enabled())
        self.refresh_display(force=True)

    def on_create_shortcuts(self, icon=None, item=None):
        made, failed = create_shortcuts()
        try:
            if made and not failed:
                icon.notify('已在桌面和开始菜单创建「%s」快捷方式' % APP_TITLE,
                            APP_TITLE)
            elif made:
                icon.notify('部分快捷方式创建失败，请检查权限', APP_TITLE)
            else:
                icon.notify('快捷方式创建失败，请检查权限', APP_TITLE)
        except Exception:
            log.debug('快捷方式提示失败', exc_info=True)
        log.info('创建快捷方式：成功 %d 个，失败 %d 个', len(made), len(failed))

    def on_quit(self, icon=None, item=None):
        self.stop_event.set()
        try:
            self.panel.stop()
        except Exception:
            pass
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass

    # -- 后台线程 -----------------------------------------------------
    def worker(self):
        # 启动即采样
        time.sleep(0.8)
        self.scan()
        self.check_low_battery(self.device_rows())
        while not self.stop_event.wait(PROBE_SECONDS):
            # 每 3 秒一次轻量探测：新设备入库、离线出库都靠它，别指望更长周期
            levels, roster = self.probe()
            interval = max(5, int(self.cfg.get('interval_minutes', 60))) * 60
            due = (self.last_scan_ts is None) or (time.time() - self.last_scan_ts >= interval)
            if due or self._scan_requested:
                trust = self._hid_removed
                self._scan_requested = False
                self._hid_removed = False
                self.scan(trust_hid=trust)
            else:
                self.refresh_display(force=(levels or roster))
            self.check_low_battery(self.device_rows())

    def _tray_anchor(self):
        """托盘图标的屏幕矩形，交给面板做「正下方 + 左对齐」。拿不到返回 None。"""
        try:
            hwnd = int(self.icon._hwnd)
        except Exception:
            return None
        if not hwnd:
            return None
        return popup.tray_icon_rect(hwnd)

    def run(self):
        rows = self.device_rows()
        head = self.headline(rows)
        shown = int(round(head['level'])) if head and head['level'] is not None else None
        self.icon = pystray.Icon(
            APP_REG_NAME,
            icon=icons.render_icon(shown),
            title=self.tooltip(rows),
            menu=Menu(lambda: self.menu_items()),
        )
        self.panel.anchor_provider = self._tray_anchor
        threading.Thread(target=self.worker, daemon=True).start()
        self.start_activate_listener()
        self.icon.run()


# ----------------------------------------------------------------- 入口
def setup_logging():
    state_store.ensure_dir()
    fh = logging.FileHandler(state_store.DATA_DIR / 'app.log', encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    fh.setLevel(logging.INFO)
    log.addHandler(fh)
    log.setLevel(logging.INFO)


def already_running():
    """已经有一个实例在跑就返回 True。

    互斥体句柄存模块级：句柄一被释放，别的实例就看不到这个互斥体了。
    """
    global _MUTEX_HANDLE
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    h = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return False
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        return True
    _MUTEX_HANDLE = h
    return False


def activate_existing(timeout=10.0):
    """通知已经在跑的那个实例：把面板叫出来。

    先 OpenEvent 再 SetEvent —— 不传数据、不做窗口查找，几行代码搞定跨进程触发。
    超时给到 10 秒：onefile 打包的程序冷启动要解压 4~6 秒，对方可能还没建好事件。
    """
    _k32.OpenEventW.restype = ctypes.c_void_p
    _k32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    _k32.SetEvent.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    end = time.time() + timeout
    while time.time() < end:
        h = _k32.OpenEventW(EVENT_MODIFY_STATE, False, ACTIVATE_EVENT)
        if h:
            _k32.SetEvent(h)
            _k32.CloseHandle(h)
            log.info('已唤起正在运行的实例')
            return True
        time.sleep(0.1)
    log.warning('唤起失败：等待 %ss 也没等到运行中实例的事件', timeout)
    return False


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--version', action='store_true')
    args, _ = parser.parse_known_args()

    if args.once:
        devs, _chans = hw_readers.scan_all()
        lines = [f"[{d['level']:3d}%] {d['name']}  ({d['channel']})" for d in devs]
        try:
            print('\n'.join(lines))
        except Exception:
            pass
        return

    if already_running():
        # 重复双击：把已经在跑的那个实例的面板叫出来，而不是再开一份
        if not args.tray:
            if not activate_existing():
                try:
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        'Device Battery 已经在运行了，但没能把它的面板叫出来。\n\n'
                        '图标在系统右下角托盘里，如果没有直接看到，'
                        '请点开托盘的「^」展开区域。',
                        APP_TITLE, 0x40)
                except Exception:
                    pass
        return

    # 先迁数据目录，再开日志：setup_logging 会先把新目录建出来，
    # 一旦新目录存在，migrate_legacy_dir 就认为「已经迁过了」而跳过。
    migrated = state_store.migrate_legacy_dir()
    setup_logging()
    log.info('启动，argv=%s', sys.argv)
    if migrated:
        log.info('数据目录已迁移到 %s', state_store.DATA_DIR)
    migrate_autostart()
    cfg = state_store.load_config()
    try:
        ensure_shortcuts(cfg)          # 桌面图标缺失就补一张（每版本只补一次）
    except Exception:
        log.debug('补建快捷方式失败', exc_info=True)
    state = state_store.load_state()
    try:
        BatteryTray(cfg, state).run()
    except Exception:
        log.exception('主循环异常')


if __name__ == '__main__':
    main()
