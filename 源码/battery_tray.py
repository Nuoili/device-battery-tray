# -*- coding: utf-8 -*-
"""
设备电量托盘 —— 常驻系统托盘，显示外接无线设备的电量。

工作方式：
  · 启动即采样一次，之后每 60 分钟读一次真实电量
  · 两次采样之间，用历史掉电速率线性外推，图标上的数字是估算值
  · 只保留一个托盘图标 + 一个定时线程，无窗口、无后台轮询

命令行：
  设备电量.exe            正常启动（有窗口即托盘）
  设备电量.exe --tray     静默启动（供开机自启使用）
  设备电量.exe --once     只读一次并打印，不驻留（排查用）
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

APP_TITLE = '设备电量'
APP_VERSION = '1.0.2'
APP_REG_NAME = 'DeviceBatteryTray'
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
# 新设备「插上就出现在面板」靠的就是它 —— 20 秒内必然入库，用户感知接近实时。
# 整轮扫描（含罗技 HID++、AULA 私有协议）仍按「检测间隔」走，别把这个调太短。
PROBE_SECONDS = 20
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


def is_autostart_enabled():
    try:
        import winreg
        hive, path = _run_key()
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, APP_REG_NAME)
            return bool(val)
    except Exception:
        return False


def set_autostart(enabled):
    try:
        import winreg
        hive, path = _run_key()
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_REG_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        log.warning('设置开机自启失败: %s', e)
        return False


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


def _merge_same_device(rows):
    """把「AULA-F87」和「AULA-F87 5.0」这种同一台设备的两条链路合成一行。

    名字只留一个（去掉蓝牙版本尾巴），电量取更有说服力的那条：
    在线的优先；都掉线就取最近连过的那条；同链路状态再比通道。
    这样面板上不会出现「一台键盘两行、一行 100% 一行未连接」。
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
        self._hev = None             # 唤起事件句柄

    # -- 采样 ---------------------------------------------------------
    def scan(self):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            items = hw_readers.scan_all()
            with self.lock:
                state_store.update_with_scan(self.state, items, time.time())
                state_store.save_state(self.state)
                self.last_scan_ts = time.time()
                self.scan_error = None
            log.info('采样完成：%d 个设备', len(items))
        except Exception as e:
            log.exception('采样失败')
            with self.lock:
                self.scan_error = str(e)
        finally:
            with self.lock:
                self.scanning = False
            self.refresh_display(force=True)

    def probe(self):
        """轻量探测（20 秒一次）：蓝牙快照 + HID 接口集合，负责「实时增删」。

        和整轮扫描的区别：
          · 蓝牙只读系统缓存属性，不往设备发请求，20 秒一次也不打扰设备休眠
          · HID 只枚举接口路径（微秒级），不读数、不跟罗技握手
        作用：
          · 新连接的设备立刻入库并刷新面板（不用等下一轮整轮扫描）
          · 蓝牙设备从快照里消失（关机 / 出配对）→ 标记离线，面板随即收起
          · HID 接口集合一有变化（插拔接收器）→ 触发整轮扫描
        返回是否有变化（有变化就该强制重画面板）。
        """
        changed = False

        # ---- 蓝牙：全量快照，没出现在里的就算离线（只影响 bluetooth 通道）----
        try:
            items = hw_readers.read_bluetooth_batteries()
        except Exception:
            log.debug('蓝牙探测失败', exc_info=True)
            items = None
        if items is not None:
            with self.lock:
                touched = state_store.apply_snapshot(
                    self.state, items, channel='bluetooth', now=time.time())
                if touched:
                    state_store.save_state(self.state)
                    changed = True
                    for key in touched:
                        dev = (self.state.get('devices') or {}).get(key) or {}
                        log.info('蓝牙更新：%s %s%% 在线=%s',
                                 dev.get('name', key), dev.get('last_level'),
                                 dev.get('online'))

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
                    changed = True
        return changed

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
        if not force and key == self._last_icon_key:
            return
        self._last_icon_key = key
        try:
            self.icon.icon = icons.render_icon(shown)
            self.icon.title = self.tooltip(rows)
            self.icon.update_menu()
        except Exception:
            log.debug('刷新托盘失败', exc_info=True)
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
            # 每 20 秒一次轻量探测：新设备入库、离线出库都靠它，所以别指望更长周期
            force = self.probe()
            interval = max(5, int(self.cfg.get('interval_minutes', 60))) * 60
            due = (self.last_scan_ts is None) or (time.time() - self.last_scan_ts >= interval)
            if due or self._scan_requested:
                self._scan_requested = False
                self.scan()
            else:
                self.refresh_display(force=force)
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
        lines = [f"[{d['level']:3d}%] {d['name']}  ({d['channel']})" for d in hw_readers.scan_all()]
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
                        '设备电量已经在运行了，但没能把它的面板叫出来。\n\n'
                        '图标在系统右下角托盘里，如果没有直接看到，'
                        '请点开托盘的「^」展开区域。',
                        '设备电量', 0x40)
                except Exception:
                    pass
        return

    setup_logging()
    log.info('启动，argv=%s', sys.argv)
    cfg = state_store.load_config()
    state = state_store.load_state()
    try:
        BatteryTray(cfg, state).run()
    except Exception:
        log.exception('主循环异常')


if __name__ == '__main__':
    main()
