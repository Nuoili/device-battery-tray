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
# 未连接超过这么多天就整条不再显示（缓存电量放太久已无参考价值）。
# 设备没被删除，只是不展示；一插回来扫描到就自然重新出现。设 0 可关闭该行为。
OFFLINE_HIDE_DAYS = 5

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


def update_with_scan(state, scanned, now=None):
    """用一次真实扫描结果更新状态，返回状态字典。"""
    now = now or time.time()
    devices = state.setdefault('devices', {})
    live_keys = set()
    for item in scanned:
        key = item['key']
        live_keys.add(key)
        dev = devices.get(key) or {}
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
    # 未扫描到的标记为离线
    for key, dev in devices.items():
        if key not in live_keys:
            dev['online'] = False
    return state


def patch_levels(state, items, now=None):
    """只更新「已有记录」的电量，不碰在线状态。

    给高频的充电探测用：蓝牙电量可以在两分钟内轻量重读一次，但一次只探到部分设备，
    不能像整轮扫描那样把没探到的都判成离线。返回真正发生变化的 key 列表。
    """
    now = now or time.time()
    devices = state.setdefault('devices', {})
    changed = []
    for item in items:
        key = item.get('key')
        level = item.get('level')
        dev = devices.get(key)
        if not dev or level is None:
            continue
        if item.get('online') is False:
            dev['online'] = False          # 蓝牙断了：别再把缓存电量当实时值
            continue
        level = int(level)
        if dev.get('last_level') == level:
            dev['last_ts'] = now          # 值没变也刷新时间，避免展示值被"外推"成掉电
            continue
        dev['last_level'] = level
        dev['last_ts'] = now
        dev['last_seen'] = now
        dev['online'] = True
        hist = dev.get('history') or []
        if not hist or hist[-1][1] != level or (now - hist[-1][0]) > 300:
            hist.append([now, level])
        dev['history'] = hist[-MAX_HISTORY:]
        dev['rate'] = _calc_rate(dev['history'])
        changed.append(key)
    return changed


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


def is_stale(dev, now=None, days=None):
    """离线太久的设备是否该从面板里收起来。

    只有「未连接 + 上次连上距今超过 days 天」才算过期；在线设备永远不算，
    时间戳缺失也无从判断，按「不过期」保留。
    """
    if dev.get('online', True):
        return False
    days = OFFLINE_HIDE_DAYS if days is None else days
    if not days or days <= 0:
        return False
    now = now or time.time()
    ts = last_online_ts(dev)
    if ts is None:
        return False
    return (now - ts) > days * 86400


def describe_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return '刚刚'
    if seconds < 3600:
        return f'{seconds // 60} 分钟前'
    if seconds < 86400:
        return f'{seconds // 3600} 小时前'
    return f'{seconds // 86400} 天前'
