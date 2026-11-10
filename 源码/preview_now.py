# -*- coding: utf-8 -*-
"""用实机扫描结果渲染一张面板预览图，方便肉眼核对。

走的是和托盘完全一样的数据管道（扫描 → state_store 更新 → device_rows 合并/过滤），
所以图上看到的行数、名字、电量就是面板里真正会出现的。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
import battery_tray
import hw_readers
import popup
import state_store

scanned, _chans = hw_readers.scan_all()
for r in scanned:
    print('扫描 %3d%% %-28s %s online=%s charging=%s'
          % (r['level'], r['name'], r['channel'], r.get('online'), r.get('charging')))

# 用磁盘上的历史状态 + 这次扫描结果，得到与托盘一致的行（含蓝牙/2.4G 合并、5 天过滤）
state = state_store.load_state()
state_store.update_with_scan(state, scanned)


class _TrayStub:
    def __init__(self, st):
        self.state = st
        self.lock = threading.RLock()


rows = battery_tray.BatteryTray.device_rows(_TrayStub(state))
print('面板行：')
for r in rows:
    print('  %3s%% %-28s online=%s channel=%s'
          % ('?' if r['level'] is None else int(round(r['level'])),
             r['name'], r['online'], r['channel']))

content = popup.render_panel(rows, scale=2.0, width_dip=340,
                             updated_text=time.strftime('%H:%M'),
                             footer='单击查看详情 · 右键菜单')
W, H = content.size

# 浅色桌面背景，模拟磨砂效果
bg = Image.new('RGB', (W + 80, H + 80), (232, 236, 242))
d = ImageDraw.Draw(bg)
for i in range(0, bg.width, 40):
    d.line([(i, 0), (i - 60, bg.height)], fill=(222, 228, 236), width=2)

panel = popup.compose_panel(content, bg.resize((W, H)), scale=2.0)
out = Image.new('RGBA', (bg.width, bg.height), (0, 0, 0, 0))
out.paste(panel, ((out.width - panel.width) // 2, (out.height - panel.height) // 2), panel)
path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '效果预览.png')
out.convert('RGB').save(path)
print('saved', path)
