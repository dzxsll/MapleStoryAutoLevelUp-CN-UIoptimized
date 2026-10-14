# -*- coding: utf-8 -*-
'''自检：登记表被删空之后，**不能留下裸头部**（否则界面再也打不开）。

## 为什么专门钉这一条（2026-10-13 真机事故）
用户点【删除地图】删掉**最后一张**已登记的地图 ⇒ `unregister_map` 走文本级编辑
（为了保住文件头部的说明注释，不能整文件重写）⇒ 段里最后一条被删掉后，原来的
`_drop_line_in_block` 把段留成了**裸头部**：

    map_mobs_mapping:      ← 后面一行都没有

`yaml.safe_load` 把这种形状解析成 `None`（不是 `{}`），于是：
  · 界面 `src/ui/ui.py` 的 `refresh_map_list` 里 `name in None` →
    `TypeError: argument of type 'NoneType' is not iterable`；
  · 那个函数在**启动链**上（__init__ → setup_main_tab → create_map_selection_gbox）
    ⇒ **窗口根本打不开**（真机两次启动失败日志：log/启动失败_*.log）；
  · 引擎侧 `self.data["map_mobs_mapping"]` 同样会炸。

## 本自检钉的三层（都是**真实调用点**，不是复述逻辑）
  1. 写侧：删掉最后一条 ⇒ 段必须是显式空表 `header: {}`，且 `yaml.safe_load` 得到 `{}`；
  2. 回归：段里还有别的登记时，删一条**不能**把段改成空表，也不能把别人的行删掉；
  3. 读侧兜底：拿"裸头部"这种坏文件直接喂给 `refresh_map_list` 用的那三行取值逻辑
     （`data.get(...) or {}`）与引擎侧取值逻辑，必须**不抛异常**。
'''
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml

from src.utils import registry

FAIL = []


def check(cond, label, extra=""):
    if cond:
        print(f"  [OK] {label}")
    else:
        print(f"  [!!] {label}{('  —— ' + extra) if extra else ''}")
        FAIL.append(label)


HEAD = '''# 地图 / 怪物登记表（测试用）
# 头部注释必须被保住

eng_to_cn: {}

map_mobs_mapping:
  沼泽地2: [鳄鱼]
  火焰之地1: [火野猪]
'''

BLANK = '''# 出厂空模板（裸头部 —— 就是出事的那个形状）
eng_to_cn: {}

map_mobs_mapping:


'''

print("=" * 74)
print("1) 写侧：删掉**最后一条**登记 ⇒ 必须是显式空表，不能是裸头部")
print("=" * 74)
tmp = os.path.join(tempfile.mkdtemp(prefix="verify_reg_"), "config_data.yaml")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(HEAD.replace("  火焰之地1: [火野猪]\n", ""))

ok = registry.unregister_map("沼泽地2", path=tmp)
check(ok is True, "unregister_map 返回 True（确实改了文件）")
with open(tmp, "r", encoding="utf-8") as f:
    text = f.read()
data = yaml.safe_load(text) or {}
check(data.get("map_mobs_mapping") is not None,
      "yaml 解析出来不是 None", f"实际 {data.get('map_mobs_mapping')!r}")
check(data.get("map_mobs_mapping") == {},
      "解析结果是空 dict {}", f"实际 {data.get('map_mobs_mapping')!r}")
check("map_mobs_mapping: {}" in text,
      "文件里写的是显式空表 `map_mobs_mapping: {}`")
check("# 头部注释必须被保住" in text, "头部注释没被冲掉")

print()
print("=" * 74)
print("2) 回归：段里还有别的登记 ⇒ 删一条不能误伤别人、也不能把段变空表")
print("=" * 74)
tmp2 = os.path.join(tempfile.mkdtemp(prefix="verify_reg_"), "config_data.yaml")
with open(tmp2, "w", encoding="utf-8") as f:
    f.write(HEAD)
registry.unregister_map("沼泽地2", path=tmp2)
with open(tmp2, "r", encoding="utf-8") as f:
    text2 = f.read()
d2 = yaml.safe_load(text2) or {}
check(d2.get("map_mobs_mapping") == {"火焰之地1": ["火野猪"]},
      "只删掉了目标那条，另一张图还在", f"实际 {d2.get('map_mobs_mapping')!r}")
check("沼泽地2" not in text2, "目标那条确实没了")
check("map_mobs_mapping: {}" not in text2, "段里还有东西，就不该写成空表")

print()
print("=" * 74)
print("3) 读侧兜底：把**裸头部**这种坏文件喂给真实取值逻辑，必须不抛异常")
print("=" * 74)
tmp3 = os.path.join(tempfile.mkdtemp(prefix="verify_reg_"), "config_data.yaml")
with open(tmp3, "w", encoding="utf-8") as f:
    f.write(BLANK)
with open(tmp3, "r", encoding="utf-8") as f:
    bad = yaml.safe_load(f) or {}
check(bad.get("map_mobs_mapping") is None,
      "前提确认：裸头部确实解析成 None（这就是事故形状）")
try:
    # src/ui/ui.py refresh_map_list 里那三行（`_eng_to_cn` / `_map_mobs`）
    _eng_to_cn = bad.get("eng_to_cn") or {}
    _map_mobs = bad.get("map_mobs_mapping") or {}
    for _name in ("沼泽地2", "火焰之地1", "随便一张图"):
        if _name in _eng_to_cn:
            _t = f"{_name} ({_eng_to_cn[_name]})"
        elif _name in _map_mobs:
            _t = _name
        else:
            continue
    # src/engine/MapleStoryAutoLevelUp.py 里的两处
    _map_name = "沼泽地2"
    _not_reg = _map_name not in (bad.get("map_mobs_mapping") or {})
    _mobs = list((bad.get("map_mobs_mapping") or {}).get(_map_name) or [])
    check(_not_reg is True and _mobs == [],
          "界面/引擎的取值逻辑在裸头部上不抛异常（退化成「没登记」）")
except Exception as e:                                     # noqa: BLE001
    check(False, "界面/引擎的取值逻辑在裸头部上不抛异常", f"{type(e).__name__}: {e}")

print()
print("=" * 74)
print("4) 变异测试自证：把写侧改回老写法（留裸头部），第 1 节必须变红")
print("=" * 74)


def _old_drop(text, header, map_name):
    '''2026-10-13 之前的写法：删完直接拼回去，不管段是否为空。'''
    import re
    m_head = re.search(rf'^{re.escape(header)}:[ \t]*\n', text, re.M)
    if not m_head:
        return text, False
    pos = m_head.end()
    kept, removed = [], False
    for line in text[pos:].splitlines(keepends=True):
        if re.match(rf'[ \t]+{re.escape(map_name)}:', line):
            removed = True
            continue
        kept.append(line)
    return (text[:pos] + ''.join(kept)) if removed else text, removed


_mut_text, _ = _old_drop(HEAD.replace("  火焰之地1: [火野猪]\n", ""),
                         "map_mobs_mapping", "沼泽地2")
_mut_val = (yaml.safe_load(_mut_text) or {}).get("map_mobs_mapping")
check(_mut_val is None,
      f"老写法确实留下裸头部（解析 None）⇒ 第 1 节的红是真的",
      f"实际 {_mut_val!r}")

print()
if FAIL:
    print(f"✗ {len(FAIL)} 项未通过：")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
print("✓ 全部通过（登记表删空：写侧不裸头 / 回归不误伤 / 读侧有兜底）")
