# -*- coding: utf-8 -*-
'''「route*.png 排序」的自检（2026-10-13 加，配合 common.sort_route_files）。

## 它防的是什么（真机现场，**全程不报错**）

引擎加载路线时原来是：

    route_files = sorted(glob.glob(resource_path(f"minimaps/{map_name}/route*.png")))

`sorted()` 排的是**字符串**。段号一旦上两位数，顺序就变成

    route1, route10, route11, route12, route13, route14, route2, ..., route9

两个后果都不报错、只表现为"行为不对"：

  1. **巡逻顺序错乱**：引擎按这个列表顺序走（`idx_routes = (idx+1) % len`），
     route1 的终点接 route10 的起点，段与段首尾对不上 ⇒ 每段都靠"失联救援"硬拽。
  2. **体检误报"闭环闭不上"**：`route_audit.audit_map` 用的是另一处
     `sorted(os.listdir(...))`，同样按字符串 ⇒ 拿 **route9** 当"最后一段"，
     报告「闭环 78px，闭不上环」，而按段号排是 **0px**（完美闭环）。
     用户为此白重录了一趟最后一段。

实测（`minimaps/沼泽地2-1`，14 段，本次事故现场）：

    字符串序：末段 route9.png  终点 (172,36) → 首段最近像素 (109,49)  闭环 76px
    数字序  ：末段 route14.png 终点 (108,50) → 首段最近像素 (108,50)  闭环  0px

所以判据只有一条，但必须在**所有**枚举 route 文件的地方成立：
**排序键是段号数字，不是文件名字符串。** 唯一实现 = `src/utils/common.py:sort_route_files`。

用法：
    python -m tools.verify_route_sort
'''
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import (route_seg_number, route_sort_key,   # noqa: E402
                             sort_route_files)

FAIL: list = []
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


# 14 段的名字（故意用真实事故的段数：这个长度才会暴露字符串排序）
NAMES = [f"route{i}.png" for i in range(1, 15)]
NUM_ORDER = [f"route{i}.png" for i in range(1, 15)]


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fp:
        return fp.read()


print("=" * 70)
print("route*.png 排序 · 自检")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】sort_route_files：按段号数字排（不是字符串）")
# ══════════════════════════════════════════════════════════════════════════
check("① 14 段 → 数字序", sort_route_files(NAMES), NUM_ORDER)

# ★ 变异：老代码就是裸 sorted()，必须能复现事故（否则这条自检抓不到 bug）
check("② ★ 变异：裸 sorted() 的末段是 route9（老行为）",
      sorted(NAMES)[-1], "route9.png")
check("③ 修好之后末段是 route14", sort_route_files(NAMES)[-1], "route14.png")
check_true("④ 两种顺序**确实不同**（证明这个测试有意义）",
           sorted(NAMES) != sort_route_files(NAMES))

check("⑤ route_home / route_rest 无段号 → 排在最后",
      sort_route_files(["route2.png", "route_home.png", "route1.png",
                        "route_rest.png"]),
      ["route1.png", "route2.png", "route_home.png", "route_rest.png"])
check("⑥ 只有回正线时也不崩",
      sort_route_files(["route_home.png"]), ["route_home.png"])
check("⑦ 空列表 → 空列表", sort_route_files([]), [])
check("⑧ 全路径也认（取 basename 的段号）",
      sort_route_files([os.path.join("minimaps", "沼泽地1", "route10.png"),
                        os.path.join("minimaps", "沼泽地1", "route2.png")]),
      [os.path.join("minimaps", "沼泽地1", "route2.png"),
       os.path.join("minimaps", "沼泽地1", "route10.png")])
check("⑨ 段号重复时**稳定**（不丢项）",
      len(sort_route_files(["route1.png", "route1.png", "route2.png"])), 3)

# ── route_seg_number / route_sort_key 本身 ────────────────────────────────
check("⑩ route_seg_number('route7.png')", route_seg_number("route7.png"), 7)
check("⑪ route_seg_number('route_home.png') = None",
      route_seg_number("route_home.png"), None)
check("⑫ 数字文件名的 key < 无段号的 key",
      route_sort_key("route1.png") < route_sort_key("route_home.png"), True)

# ui.py 的删除确认对话框用 route_sort_key 排**混着**的文件列表：
# route 文件先按段号，其它文件按名字落在同一档里。
check("⑬ 混着 map.png / route_home.png 时 route{N} 在前（ui 对话框用法）",
      sorted(["map.png", "route_home.png", "route1.png", "route10.png",
              "route2.png"], key=lambda f: (route_sort_key(f), f)),
      ["route1.png", "route2.png", "route10.png", "map.png", "route_home.png"])

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】接线：引擎加载必须用它（否则修了也白修）")
# ══════════════════════════════════════════════════════════════════════════
_eng = _read("src/engine/MapleStoryAutoLevelUp.py")
check_true("① 引擎调了 sort_route_files(", "sort_route_files(" in _eng)
check_true("② 引擎里**不再**有 sorted(glob.glob(", "sorted(glob.glob(" not in _eng)
check_true("③ 引擎仍然排除 route_home/route_rest",
           'endswith("route_home.png")' in _eng
           and 'endswith("route_rest.png")' in _eng)

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】接线：体检必须用**同一份**（否则引擎对了、体检还误报）")
# ══════════════════════════════════════════════════════════════════════════
_aud = _read("src/utils/route_audit.py")
check_true("① 体检调了 sort_route_files(", "sort_route_files(" in _aud)
check_true("② 体检里**不再**有 sorted(...os.listdir(...))",
           "sorted(f for f in os.listdir(" not in _aud)
_src = inspect.getsource(__import__(
    "src.utils.route_audit", fromlist=["audit_map"]).audit_map)
check_true("③ audit_map 函数体里确实用了它", "sort_route_files(" in _src)

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】接线：其它枚举 route 文件的地方（显示顺序也不能乱）")
# ══════════════════════════════════════════════════════════════════════════
for rel, desc in (("tools/homeRouteDrawer.py", "手绘回正线读主线"),
                  ("tools/jump_pixel_report.py", "跳爬像素体检列表"),
                  ("src/ui/ui.py", "删除确认对话框")):
    _t = _read(rel)
    check_true(f"{desc}（{os.path.basename(rel)}）用了按段号排的键",
               "sort_route_files(" in _t or "route_sort_key(" in _t)

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】现场复现（数据目录存在才跑）：14 段地图的两种顺序")
# ══════════════════════════════════════════════════════════════════════════
_real = os.path.join(ROOT, "minimaps", "沼泽地2-1")
if not os.path.isdir(_real):
    print("  [SKIP] minimaps/沼泽地2-1 不存在 —— 略过现场复现")
else:
    _names = [f for f in os.listdir(_real)
              if f.startswith("route") and f.endswith(".png")]
    _sorted = sort_route_files(_names)
    check_true(f"① 现场 {len(_names)} 段：数字序末段是 route14.png",
               _sorted[-1] == "route14.png")
    if len(_names) >= 10:
        check_true("② ★ 现场：字符串序末段是 route9.png（这就是那条误报的来路）",
                   sorted(_names)[-1] == "route9.png")

print("\n" + "=" * 70)
if FAIL:
    print(f"自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("全部通过")
sys.exit(0)
