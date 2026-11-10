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
import statistics
import time
from pathlib import Path

APP_NAME = 'DeviceBatteryTray'
DATA_DIR = Path(os.environ.get('APPDATA', '.')) / APP_NAME
STATE_FILE = DATA_DIR / 'state.json'
CONFIG_FILE = DATA_DIR / 'config.json'

DEFAULT_RATE = 0.5          # %/小时，没有历史数据时的兜底
MAX_HISTORY = 240           # 每个设备保留的样本数（约 10 天）
RATE_WINDOW = 7 * 24 * 3600  # 只用最近 7 天的样本算速率
FRESH_SECONDS = 600         # 10 分钟内的采样视为「实测」
# 未连接多久就从面板收起来（秒）。用户要求「离线即隐藏，下次连上再出现」，
# 但蓝牙属性和整轮扫描都有抖动，留 90 秒宽限，避免闪进闪出。
# 设 0 可关闭隐藏行为（离线设备继续按缓存电量展示）。
OFFLINE_HIDE_SECONDS = 90

DEFAULT_CONFIG = {
    'interval_minutes': 60,
    'autostart': False,
    'show_unknown_hint': True,
}


def ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


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


def apply_snapshot(state, scanned, channel=None, now=None):
    """用一份「某通道的完整快照」更新状态，返回发生变化的 key 列表。

    channel 给定时只在该通道内做离线判定 —— 蓝牙轻量探测只覆盖蓝牙设备，
    不能顺手把罗技 / AULA 那些这次没探到的设备判成离线。
    """
    now = now or time.time()
    devices = state.setdefault('devices', {})
    live_keys = set()
    changed = []
    for item in scanned:
        key = item['key']
        live_keys.add(key)
        dev = devices.get(key) or {}
        is_new = key not in devices
        prev_level = dev.get('last_level')
        prev_online = dev.get('online', True)
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
        if is_new or prev_level != dev['last_level'] or prev_online != connected:
            changed.append(key)
    # 未出现在快照里的设备标记为离线（只针对指定通道）
    for key, dev in devices.items():
        if key in live_keys:
            continue
        if channel is not None and dev.get('channel') != channel:
            continue
        if dev.get('online', True):
            dev['online'] = False
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
    留 OFFLINE_HIDE_SECONDS 的宽限，避免蓝牙属性抖动导致闪进闪出。
    在线设备永远不算过期；时间戳缺失按「过期」处理 —— 没有时间戳说明数据不完整，
    留着反而会长期显示一个不知道多久以前的电量。
    """
    if dev.get('online', True):
        return False
    secs = OFFLINE_HIDE_SECONDS if seconds is None else seconds
    if not secs or secs <= 0:
        return False
    now = now or time.time()
    ts = last_online_ts(dev) or dev.get('last_seen')
    if ts is None:
        return True
    return (now - ts) > secs


def describe_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return '刚刚'
    if seconds < 3600:
        return f'{seconds // 60} 分钟前'
    if seconds < 86400:
        return f'{seconds // 3600} 小时前'
    return f'{seconds // 86400} 天前'
