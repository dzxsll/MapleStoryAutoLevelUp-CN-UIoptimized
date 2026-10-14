# -*- coding: utf-8 -*-
"""
render_ui_preview — 不开窗口，把 GUI 渲染成 PNG，用来肉眼检查汉化/布局

用法：
    python -m tools.render_ui_preview

说明：
- 在真实 Windows 桌面会话下直接渲染，不 show() 窗口（不会弹窗打断你），
  字体走系统字体，中文正常显示。
- 不要设 QT_QPA_PLATFORM=offscreen：离屏平台没有中文字体，会渲染成方框。

输出：debug/ui_preview_*.png
"""
import os
import sys

# Local import before Qt to keep cwd on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
from src.ui.ui import MainWindow  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug")


class DummyController:
    """离线预览用：MainWindow 会调它的 enable/disable_bot_viz"""

    def enable_bot_viz(self):
        pass

    def disable_bot_viz(self):
        pass


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    app = QApplication([])
    win = MainWindow(controller=DummyController())

    # 页签名单直接问控件（2026-10-14：硬编码的名单在页签重排后就过期了，
    # 而且"名字→下标"假设一旦错位，截出来的是别的页、还照样报成功）
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    for index, name in enumerate(names):
        try:
            win.tabs.setCurrentIndex(index)
            app.processEvents()
            pix = win.grab()
            path = os.path.join(OUT_DIR, f"ui_preview_{name}.png")
            pix.save(path)
            print(f"已输出: {path}  ({pix.width()}x{pix.height()})")
        except Exception as e:
            print(f"[{name}] 渲染失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
