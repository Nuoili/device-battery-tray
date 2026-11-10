# -*- coding: utf-8 -*-
"""
电量状态存储 + 掉电速率估算。

思路：
  · 每小时采一次真实电量，写进 history
  · 用 history 里「电量下降」的相邻样本算出 %/小时，取中位数作为该设备速率
  · 采样间隔内，按速率线性外推到当前时刻，得到展示值
"""
import json
import os
import shutil
import statistics
import time
from pathlib import Path

# 数据目录名跟产品名走：v1.0.5 起是 `%APPDATA%\Device Battery\`。
# 旧版叫 DeviceBatteryTray，启动时会整个挪过来（见 migrate_legacy_dir）。
APP_NAME = 'Device Battery'
LEGACY_APP_NAMES = ('DeviceBatteryTray',)
DATA_DIR = Path(os.environ.get('APPDATA', '.')) / APP_NAME
STATE_FILE = DATA_DIR / 'state.json'
CONFIG_FILE = DATA_DIR / 'config.json'

DEFAULT_RATE = 0.5          # %/小时，没有历史数据时的兜底
MAX_HISTORY = 240           # 每个设备保留的样本数（约 10 天）
RATE_WINDOW = 7 * 24 * 3600  # 只用最近 7 天的样本算速率
FRESH_SECONDS = 600         # 10 分钟内的采样视为「实测」
# 判定为离线的设备多久后从面板收起（秒）。用户要求「离线 3 秒内必须反映到面板」，
# 所以这里是 0：一判离线就立刻不显示，下次连上再自然回来。
# 探测本身 3 秒一轮，最坏 3 秒就能落到面板上。
# 设 None（或负数）可关闭隐藏行为，离线设备继续按缓存电量展示。
OFFLINE_HIDE_SECONDS = 0
# 兜底：标记为「在线」但这么久都没读到过真实电量的设备，也不再显示。
# 典型来源是 HID 设备休眠后读取器一直拿不到响应（在线标记还停在最后成功那次），
# 或者接收器被拔掉但没被接口集合变化捕获到。7 天足够长，不会误伤真的在用的设备。
GHOST_HIDE_DAYS = 7

DEFAULT_CONFIG = {
    'interval_minutes': 60,
    'autostart': False,
    'show_unknown_hint': True,
}


def ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_dir():
    """把旧数据目录（`%APPDATA%\\DeviceBatteryTray`）挪到新名字下。

    改名不改数据：配置、电量历史、日志全都带过来，用户升级后不用重新学习掉电速率。
    新目录已存在就直接用新的，不覆盖。返回是否真的迁移了。
    """
    base = DATA_DIR.parent
    try:
        if DATA_DIR.exists():
            return False
    except OSError:
        return False
    for name in LEGACY_APP_NAMES:
        old = base / name
        try:
            if not old.is_dir():
                continue
        except OSError:
            continue
        try:
            old.rename(DATA_DIR)          # 同卷直接改名，最快且保留时间戳
            return True
        except OSError:
            try:                          # 被占用 / 跨卷时退回复制
                shutil.copytree(old, DATA_DIR)
                return True
            except Exception:
                return False
    return False


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(default, dict) and isinstance(data, dict):
            merged = dict(default)
            merged.update(data)
            return merged
        return data
    except Exception:
        return dict(default) if isinstance(default, dict) else default


def save_json(path, data):
    ensure_dir()
    tmp = str(path) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def load_config():
    return load_json(CONFIG_FILE, DEFAULT_CONFIG)


def save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def load_state():
    st = load_json(STATE_FILE, {'devices': {}})
    if 'devices' not in st or not isinstance(st['devices'], dict):
        st = {'devices': {}}
    return st


def save_state(st):
    save_json(STATE_FILE, st)


def _calc_rate(history):
    """从历史样本估算 %/小时。只统计电量下降的相邻样本。"""
    if not history or len(history) < 2:
        return None
    now = time.time()
    rates = []
    recent = [h for h in history if isinstance(h, (list, tuple)) and len(h) == 2]
    for i in range(1, len(recent)):
        t1, l1 = recent[i - 1]
        t2, l2 = recent[i]
        dt = t2 - t1
        if dt < 600:              # 间隔太短，噪声大
            continue
        if now - t2 > RATE_WINDOW:
            continue
        dl = l1 - l2
        if dl <= 0:               # 充电或持平，不算
            continue
        rates.append(dl / (dt / 3600.0))
    if not rates:
        return None
    return statistics.median(rates)


def update_levels(state, scanned, now=None):
    """只更新电量/名字/在线标志，**不做离线判定**，返回各类变化：

        {'new': [...], 'level': [...], 'revived': [...], 'offline': [...]}

    · new     —— 首次出现（面板要加一行）
    · revived —— 之前离线、这次又出现在快照里（面板要把隐藏的行放回来）
    · level   —— 电量数字变了（只有数字要换，行结构不用动）
    · offline —— 快照里明确标了 online=False（经典蓝牙链路已断）

    和 mark_offline 分开，是为了让「电量刷新」和「设备增删」成为两条独立通道：
    电量变只重绘数值，设备增减才重建列表。
    """
    now = now or time.time()
    devices = state.setdefault('devices', {})
    out = {'new': [], 'level': [], 'revived': [], 'offline': []}
    for item in scanned:
        key = item['key']
        dev = devices.get(key) or {}
        is_new = key not in devices
        prev_level = dev.get('last_level')
        prev_online = bool(dev.get('online', False))
        dev['name'] = item['name']
        dev['channel'] = item.get('channel', '')
        dev['status'] = item.get('status')
        dev['charging'] = item.get('charging')      # True 充电中 / False 放电 / None 读不到
        dev['last_level'] = int(item['level'])
        connected = bool(item.get('online', True))
        dev['last_ts'] = now if connected else dev.get('last_ts')
        dev['last_seen'] = now
        hist = dev.get('history') or []
        # 同一个值短时间内重复记录没有意义，但保留可让速率更准；这里按 5 分钟粒度去抖
        if not hist or (now - hist[-1][0]) > 300 or hist[-1][1] != item['level']:
            hist.append([now, int(item['level'])])
        dev['history'] = hist[-MAX_HISTORY:]
        dev['rate'] = _calc_rate(dev['history'])
        dev['online'] = connected
        devices[key] = dev

        if is_new:
            out['new'].append(key)
        elif connected and not prev_online:
            out['revived'].append(key)
        elif prev_level != dev['last_level'] and connected == prev_online:
            out['level'].append(key)
        if not connected and prev_online:
            out['offline'].append(key)
    return out


def mark_offline(state, live_keys, channel=None, now=None):
    """把「没出现在本轮快照里」的设备标记离线，返回转为离线的 key 列表。

    channel 给定时只在该通道内判定 —— 蓝牙轻量探测只覆盖蓝牙设备，
    不能顺手把罗技 / AULA 那些这次没探到的设备判成离线。
    """
    devices = state.setdefault('devices', {})
    live = set(live_keys)
    out = []
    for key, dev in devices.items():
        if key in live:
            continue
        if channel is not None and dev.get('channel') != channel:
            continue
        if dev.get('online', True):
            dev['online'] = False
            out.append(key)
    return out


def apply_snapshot(state, scanned, channel=None, now=None):
    """用一份「某通道的完整快照」更新状态，返回发生变化的 key 列表。

    内部走 update_levels + mark_offline 两步，行为与拆开之前一致。
    """
    now = now or time.time()
    res = update_levels(state, scanned, now)
    off = mark_offline(state, [it['key'] for it in scanned], channel, now)
    changed = []
    for bucket in ('new', 'level', 'revived', 'offline'):
        for key in res[bucket]:
            if key not in changed:
                changed.append(key)
    for key in off:
        if key not in changed:
            changed.append(key)
    return changed


def update_with_scan(state, scanned, now=None):
    """用一次真实扫描结果更新状态，返回状态字典。"""
    apply_snapshot(state, scanned, None, now)
    return state


def estimate(dev, now=None):
    """返回 (显示电量, 是否估算值)。"""
    now = now or time.time()
    level = dev.get('last_level')
    ts = dev.get('last_ts')
    if level is None:
        return None, False
    if not dev.get('online', True):
        return level, False
    elapsed = max(0.0, now - (ts or now))
    if elapsed <= FRESH_SECONDS:
        return level, False
    rate = dev.get('rate')
    if rate is None:
        rate = DEFAULT_RATE
    est = level - rate * (elapsed / 3600.0)
    if est <= 0:
        return 0, True
    return est, True


def last_online_ts(dev):
    """上次「真正连上」的时刻。

    优先 last_ts（只有读到实时电量时才刷新），没有再退回 last_seen
    （每次扫描都会刷新，包括没连上的那几次）。两个都没有返回 None。
    """
    ts = dev.get('last_ts')
    if not ts:
        ts = dev.get('last_seen')
    try:
        return float(ts) if ts else None
    except (TypeError, ValueError):
        return None


def is_stale(dev, now=None, seconds=None):
    """离线设备是否该从面板里收起来。

    规则（2026-11-11 按用户要求改）：**一离线就收，再连上自动回来**。
    默认 OFFLINE_HIDE_SECONDS=0，即判定离线的那一次刷新就收起（探测 3 秒一轮，
    最坏 3 秒落到面板），不再留宽限期。在线设备永远不算过期。
    传负数可关闭隐藏（seconds=None 表示「用默认值」）；时间戳缺失按「过期」处理 ——
    没有时间戳说明数据不完整，留着反而会长期显示一个不知道多久以前的电量。
    """
    now = now or time.time()
    if dev.get('online', True):
        # 在线但很久没读到真实电量 → 幽灵记录，收起来
        if GHOST_HIDE_DAYS and GHOST_HIDE_DAYS > 0:
            ts = last_online_ts(dev)
            if ts is not None and (now - ts) > GHOST_HIDE_DAYS * 86400:
                return True
        return False
    secs = OFFLINE_HIDE_SECONDS if seconds is None else seconds
    if secs is None or secs < 0:
        return False
    ts = last_online_ts(dev) or dev.get('last_seen')
    if ts is None:
        return True
    return (now - ts) >= secs


def describe_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return '刚刚'
    if seconds < 3600:
        return f'{seconds // 60} 分钟前'
    if seconds < 86400:
        return f'{seconds // 3600} 小时前'
    return f'{seconds // 86400} 天前'
