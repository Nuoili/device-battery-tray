# -*- coding: utf-8 -*-
"""生成多尺寸 ico，供 exe 使用。"""
import sys
from icons import render_app_icon

out = sys.argv[1] if len(sys.argv) > 1 else 'Device Battery.ico'
img = render_app_icon(256)
img.save(out, format='ICO',
         sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
print('saved', out)
