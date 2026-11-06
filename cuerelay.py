#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CueRelay 启动入口。

实际代码在 cuerelay_app/ 包内：
  config.py     路径与时间工具
  winapi.py     Windows 原生封装
  imaging.py    截图数据
  vision.py     界面识别（输入框 / 发送按钮 / 变化校验）
  locator.py    识别与锚点的解析顺序
  automation.py 带校验的执行流水线
  scheduler.py  调度引擎
  store.py      任务与设置持久化
  ui/           界面
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from cuerelay_app.launch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
