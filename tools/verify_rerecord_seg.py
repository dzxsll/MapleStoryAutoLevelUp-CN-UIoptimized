# -*- coding: utf-8 -*-
'''「单段重录」的自检（2026-10-12 加，配合 tools/routeRecorder.py 的 --rerecord-seg）。

## 这个功能最怕什么

**怕写错段号。** 重录第 2 段却把 route3.png 覆盖了 —— 用户完全看不出来
（路线图上无非少了一条线、多了一条线），只有挂机时角色不按预期走才暴露，
而且那时候旧路线已经没了。

所以本自检的重点全在**"只动该动的那一个文件"**：
  【1】`route_out_index()` 算出来的目标段号对不对（纯函数，直接钉）
  【2】真的调 `save_route()` 跑一遍，然后**逐字节**比对：
       · 目标段被换了；
       · **其它段一个字节都没变**；
       · 原图被备份到了 `_backup_重录前/`；
       · 连按两次 F3 → 还是覆盖同一个目标段，备份仍是**原图**（不被中间版本冲掉）。
  【3】非重录模式的老行为不许被改坏（照旧自增、照旧写 route{idx+1}）
  【4】三种非法组合必须以退出码 2 挡住，且报错文案对得上

用法：
    python -m tools.verify_rerecord_seg
'''
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import load_yaml, load_image, imwrite_unicode   # noqa: E402
from src.utils.paths import resource_path                            # noqa: E402
from tools.routeRecorder import RouteRecorder                        # noqa: E402

FAIL: list = []
TMP = "_verify_rr_tmp"
TMP_NOMAP = "_verify_rr_nomap"
TMP_ONLY1 = "_verify_rr_only1"
# ⚠️ 守卫用例要**独立目录**：TMP_ONLY1 会被用例【3】写出 route2.png，
#    到用例【4】时"缺第 2 段"的前提就不成立了（实测踩到，退出码变成 1 而不是 2）。
TMP_GUARD = "_verify_rr_guard"
TMP_SEGS = "_verify_rr_segs"
BUILT_DIRS = [TMP, TMP_NOMAP, TMP_ONLY1, TMP_GUARD, TMP_SEGS]


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def mapdir(name):
    return resource_path(os.path.join("minimaps", name))


def cleanup():
    for n in BUILT_DIRS:
        shutil.rmtree(mapdir(n), ignore_errors=True)


def solid(color, shape=(40, 60)):
    return np.full((shape[0], shape[1], 3), color, dtype=np.uint8)


def make_recorder(map_name, img_route, idx_routes=0, rerecord_seg=0, armed=True):
    '''造一个只带 `save_route` 真正会碰到的字段的录制器（不跑 GUI / 不连游戏）。'''
    cfg = load_yaml("config/config_default.yaml")
    r = RouteRecorder.__new__(RouteRecorder)
    r.cfg = cfg
    r.args = SimpleNamespace(new_map=map_name)
    r.color_code = {}
    for k, v in cfg["route"]["color_code"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    for k, v in cfg["route"]["color_code_up_down"].items():
        r.color_code[tuple(int(p) for p in k.split(","))] = v
    r.color_code_up_down = {}
    r.img_map = solid((10, 10, 10))          # 底图（_reset_route_canvas 用）
    r.img_route = img_route
    r.loc_player_global = (30, 20)
    r.loc_player_global_last = (10, 10)
    r._blob_pixels = {}
    r._route1_start = None
    r.idx_routes = idx_routes
    r._armed = armed
    r.append_home = False
    r._home_recording = False
    r.rerecord_seg = rerecord_seg
    r._rerecord_backup = None
    r._emit_state = lambda: None             # 面板协议不在本自检范围内
    return r


# ══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("单段重录 · 自检")
print("=" * 70)
cleanup()

# ══════════════════════════════════════════════════════════════════════════
print("\n【1】route_out_index()：这次该写 route{几}.png")
# ══════════════════════════════════════════════════════════════════════════
check("① 普通模式、还没存过 → route1",
      make_recorder(TMP, solid((1, 2, 3)), idx_routes=0).route_out_index(), 1)
check("② 普通模式、已存 2 段 → route3",
      make_recorder(TMP, solid((1, 2, 3)), idx_routes=2).route_out_index(), 3)
check("③ 重录模式（第 2 段，idx=1）→ 还是 route2",
      make_recorder(TMP, solid((1, 2, 3)), idx_routes=1, rerecord_seg=2)
      .route_out_index(), 2)
check("④ 重录模式（第 5 段，idx=4）→ route5",
      make_recorder(TMP, solid((1, 2, 3)), idx_routes=4, rerecord_seg=5)
      .route_out_index(), 5)
# 老自检用 __new__ 造实例时没有 rerecord_seg —— 必须能兜底，不能 AttributeError
_legacy = make_recorder(TMP, solid((1, 2, 3)), idx_routes=1)
del _legacy.rerecord_seg
check("⑤ 没有 rerecord_seg 字段（老式假实例）→ 兜底走普通模式",
      _legacy.route_out_index(), 2)

# ══════════════════════════════════════════════════════════════════════════
print("\n【2】★ save_route()：只动该动的那一个文件")
# ══════════════════════════════════════════════════════════════════════════
_d = mapdir(TMP)
shutil.rmtree(_d, ignore_errors=True)
os.makedirs(_d, exist_ok=True)
COL = {1: (255, 0, 0), 2: (0, 255, 0), 3: (0, 0, 255)}     # 三段各自一个纯色，便于分辨
for n, c in COL.items():
    imwrite_unicode(os.path.join(_d, f"route{n}.png"), solid(c))
imwrite_unicode(os.path.join(_d, "map.png"), solid((9, 9, 9)))

_before = {n: open(os.path.join(_d, f"route{n}.png"), "rb").read() for n in COL}

# ── 第一次重录第 2 段 ─────────────────────────────────────────────────────
r2 = make_recorder(TMP, solid((11, 22, 33)), idx_routes=1, rerecord_seg=2)
r2.save_route()

_after1 = {n: open(os.path.join(_d, f"route{n}.png"), "rb").read() for n in COL}
check("① 目标段 route2.png 被换掉了", _after1[2] != _before[2], True)
check_true("② 其它段 route1.png **一个字节都没变**", _after1[1] == _before[1])
check_true("③ 其它段 route3.png **一个字节都没变**", _after1[3] == _before[3])
_n2 = load_image(os.path.join(_d, "route2.png"))
check("④ 新 route2 确实是刚录的那张（角落像素 = 新纯色）",
      tuple(int(v) for v in _n2[0, 0]), (11, 22, 33))
# goal 必须压上（少了它引擎在段末死锁）
_goal_bgr = (0, 255, 255)
check_true("⑤ 新 route2 上压了 goal 点",
           int(((_n2[:, :, 0] == _goal_bgr[0]) & (_n2[:, :, 1] == _goal_bgr[1])
                & (_n2[:, :, 2] == _goal_bgr[2])).sum()) >= 9)
# 原图必须被备份
_bak = os.path.join(_d, "_backup_重录前", "route2.png")
check_true("⑥ 原 route2.png 被备份到 _backup_重录前/", os.path.exists(_bak))
check_true("⑦ 备份里就是**原图**（逐字节一致）",
           os.path.exists(_bak) and open(_bak, "rb").read() == _before[2])
check("⑧ 重录模式**不自增** idx_routes（否则下一次会吃掉 route3）",
      r2.idx_routes, 1)

# ── 再按一次 F3（重存这一版）──────────────────────────────────────────────
r2.img_route = solid((77, 88, 99))
r2.loc_player_global = (31, 21)
r2.save_route()
_after2 = {n: open(os.path.join(_d, f"route{n}.png"), "rb").read() for n in COL}
check("⑨ 第二次保存**还是覆盖 route2**（不是 route3）",
      (load_image(os.path.join(_d, "route2.png"))[0, 0].tolist(), _after2[3] == _before[3]),
      ([77, 88, 99], True))
check_true("⑩ 第二次保存后 route3 仍然没被动过", _after2[3] == _before[3])
check_true("⑪ 备份仍是**最初的原图**，没被中间那一版冲掉",
           open(_bak, "rb").read() == _before[2])

# ══════════════════════════════════════════════════════════════════════════
print("\n【3】非重录模式的老行为不许被改坏")
# ══════════════════════════════════════════════════════════════════════════
_d2 = mapdir(TMP_ONLY1)
shutil.rmtree(_d2, ignore_errors=True)
os.makedirs(_d2, exist_ok=True)
imwrite_unicode(os.path.join(_d2, "map.png"), solid((9, 9, 9)))
r3 = make_recorder(TMP_ONLY1, solid((5, 6, 7)), idx_routes=0, rerecord_seg=0)
r3.save_route()
check("① 普通模式写的是 route1.png",
      os.path.exists(os.path.join(_d2, "route1.png")), True)
check("② 普通模式照旧自增 idx_routes", r3.idx_routes, 1)
check("③ 普通模式**不**建 _backup_重录前（那目录只有重录才该出现）",
      os.path.exists(os.path.join(_d2, "_backup_重录前")), False)
r3.img_route = solid((8, 8, 8))
r3.save_route()
check("④ 再来一次写 route2.png（段号继续往前走）",
      (os.path.exists(os.path.join(_d2, "route2.png")), r3.idx_routes), (True, 2))

# ══════════════════════════════════════════════════════════════════════════
print("\n【4】三种非法组合必须被挡住（真跑一遍录制器，看退出码与文案）")
# ══════════════════════════════════════════════════════════════════════════
_emptyd = mapdir(TMP_NOMAP)
shutil.rmtree(_emptyd, ignore_errors=True)
os.makedirs(_emptyd, exist_ok=True)          # 有目录，但没有 map.png
# 守卫③专用：有 map.png 和 route1.png，但**确实没有** route2.png
_gd = mapdir(TMP_GUARD)
shutil.rmtree(_gd, ignore_errors=True)
os.makedirs(_gd, exist_ok=True)
imwrite_unicode(os.path.join(_gd, "map.png"), solid((9, 9, 9)))
imwrite_unicode(os.path.join(_gd, "route1.png"), solid((1, 1, 1)))


def run_recorder(*extra):
    p = subprocess.run(
        [sys.executable, "-m", "tools.routeRecorder", *extra],
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


rc, out = run_recorder("--new_map", TMP_NOMAP, "--rerecord-seg", "1")
check("① 没有 map.png → 退出码 2", rc, 2)
check_true("② 文案说清了是缺底图", "没有地图底图" in out)

rc, out = run_recorder("--new_map", TMP_GUARD, "--rerecord-seg", "2")
check("③ 没有第 2 段（只有 route1）→ 退出码 2", rc, 2)
check_true("④ 文案点明了缺哪一段", "没有第 2 段" in out)

rc, out = run_recorder("--new_map", TMP_GUARD, "--rerecord-seg", "1", "--append-home")
check("⑤ --rerecord-seg 与 --append-home 同用 → 退出码 2", rc, 2)
check_true("⑥ 文案说明了两者互斥", "不能同时用" in out)

# ══════════════════════════════════════════════════════════════════════════
print("\n【5】界面侧：段号枚举只数**连续**的段（决定下拉里列哪几段）")
# ══════════════════════════════════════════════════════════════════════════
# 引擎是按 route1、route2… 顺序加载直到文件不存在的 ⇒ 中间断一段，后面等于不存在。
# 界面若把不连续的段也列出来，用户选中一个"引擎其实不会加载"的段去重录，白忙一场。
from src.ui.ui import MainWindow                                    # noqa: E402

_sd = mapdir(TMP_SEGS)
shutil.rmtree(_sd, ignore_errors=True)
os.makedirs(_sd, exist_ok=True)
for _n in (1, 2, 4):                      # 故意缺 route3.png
    imwrite_unicode(os.path.join(_sd, f"route{_n}.png"), solid((1, 1, 1)))
# 这个方法不碰 self，可以传 None 直接当纯函数测（不用起 QApplication）
check("① 有 1/2/4 段（缺 3）→ 只认 1、2", MainWindow._existing_route_segs(None, TMP_SEGS), [1, 2])
check("② 一段都没有的目录 → 空列表",
      MainWindow._existing_route_segs(None, TMP_NOMAP), [])
check("③ 目录不存在也不炸 → 空列表",
      MainWindow._existing_route_segs(None, "_verify_rr_not_exist"), [])

print("\n" + "=" * 70)
cleanup()
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n_ in FAIL:
        print(f"   - {n_}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
