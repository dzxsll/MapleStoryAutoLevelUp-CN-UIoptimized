# -*- coding: utf-8 -*-
'''自检：`tools/route_pad_canvas.py`（窄画布路线图的补边修复，2026-10-14 加）。

它钉住的是**四条安全性质**（弄错任何一条，用户的数据就被改坏了）：
  ① 只允许"右下补黑边"，**绝不做缩放、绝不平移坐标**；
  ② 允许补边的唯一依据是"剥掉路线色后的底图**正好在 (0,0)** 与 map.png 对齐"，
     偏了就判无法判定、一个字节都不写（fail-closed）；
  ③ 写盘前后**路线像素的数量与坐标必须完全不变**（这是"补边"与"搬路线"的分界）；
  ④ 写盘前整目录备份、`--restore` 能逐字节还原。

另外钉住一条**配置坑**（写这个工具时当场踩到的）：
  `config_custom.yaml` 是**差分**文件，里面**没有** `route.color_code` ——
  拿它当完整配置，色码表就是空的，会把好文件判成"底图全黑"。

用法：python -m tools.verify_route_pad_canvas        （CI 清单里的一项）
'''
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.utils.common import active_config_path, imread_unicode, imwrite_unicode, load_yaml  # noqa: E402
from src.utils.paths import resource_path                                                 # noqa: E402
from tools.route_pad_canvas import (                                                      # noqa: E402
    load_merged_cfg, codes_bgr, find_color_code, check_align, pad_to_ref, route_mask,
    plan_map, main,
)

_fail = 0
_tmp_name = "_verify_pad"
_tmp_dir = resource_path(os.path.join("minimaps", _tmp_name))


def check(name, got, want):
    global _fail
    ok = got == want
    if not ok:
        _fail += 1
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    return ok


def check_true(name, cond, note=""):
    return check(name + (f"（{note}）" if note else ""), bool(cond), True)


def make_ref(h=60, w=120):
    '''造一张"像地图"的参照底图：黑边 + 渐变 + 几个色块（保证模板匹配有唯一峰）。'''
    ref = np.zeros((h, w, 3), np.uint8)
    ref[10:h - 10, 10:w - 10, 0] = (np.arange(w - 20)[None, :] * 3) % 256
    ref[10:h - 10, 10:w - 10, 1] = (np.arange(h - 20)[:, None] * 5) % 256
    ref[10:h - 10, 10:w - 10, 2] = 90
    ref[20:30, 20:40] = (200, 30, 30)
    ref[35:45, 70:95] = (30, 200, 60)
    ref[12:18, 95:112] = (250, 250, 40)
    return ref


def read_bytes(p):
    with open(p, "rb") as fh:
        return fh.read()


def digest(p):
    '''比内容就用摘要 —— 直接把 PNG 字节塞进断言输出会把清单刷爆。'''
    return hashlib.sha1(read_bytes(p)).hexdigest()


def snapshot(d):
    return {f: digest(os.path.join(d, f)) for f in sorted(os.listdir(d))
            if os.path.isfile(os.path.join(d, f))}


def main_check():
    print("=" * 68)
    cfg = load_merged_cfg()
    codes = codes_bgr(cfg)
    route_color = codes[0]

    # ── 【1】色码表：差分配置里没有它，必须用合并后的配置 ────────────────────
    print("\n【1】色码表必须来自「默认 + 方案」合并后的配置")
    table = find_color_code(cfg)
    check("① 合并配置里找得到 color_code（12 条）", len(table), 12)
    diff_cfg = load_yaml(active_config_path())
    raised = False
    try:
        find_color_code(diff_cfg)
    except KeyError:
        raised = True
    check_true("② 光用方案差分文件（config_custom.yaml）找不到 —— 所以必须合并", raised)
    check("③ codes_bgr 把配置的 RGB 反成文件字节序（255,0,0 → 0,0,255）",
          codes[0], (0, 0, 255))

    # ── 【2】对齐判据 ────────────────────────────────────────────────────────
    print("\n【2】对齐判据：只有「底图正好在 (0,0)」才允许补边")
    ref = make_ref()
    narrow_ok = ref[:, :70].copy()
    narrow_ok[25:27, 25:27] = route_color          # 画两个"路线像素"
    v = check_align(narrow_ok, ref, codes)
    check("① 对齐的窄画布 → 可补边", v.ok, True)
    check("② 偏移量是 (0,0)", (v.dx, v.dy), (0, 0))
    check_true("③ 判据里带证据（相似度/差异率）", v.sim > 0.9 and v.diff_frac < 0.03,
               f"sim={v.sim:.3f} diff={v.diff_frac * 100:.2f}%")

    narrow_shift = ref[:, 5:75].copy()             # 内容右移 5px ⇒ 不能被"补边"糊过去
    v2 = check_align(narrow_shift, ref, codes)
    check("④ 底图偏了 5px → 拒绝补边", v2.ok, False)
    check("⑤ 偏移量报出来（dx=5）", v2.dx, 5)
    check_true("⑥ 拒绝理由说得出为什么", "偏了" in v2.reason, v2.reason)

    rng = np.random.default_rng(7)
    v3 = check_align(rng.integers(0, 255, (60, 70, 3), dtype=np.uint8), ref, codes)
    check("⑦ 底图与 map.png 完全不是一张 → 拒绝", v3.ok, False)
    v4 = check_align(np.zeros((60, 70, 3), np.uint8), ref, codes)
    check("⑧ 全黑窄画布（读不出内容）→ 拒绝", v4.ok, False)
    v5 = check_align(np.zeros((80, 130, 3), np.uint8), ref, codes)
    check("⑨ 比 map.png 还大 → 拒绝", v5.ok, False)
    v6 = check_align(ref.copy(), ref, codes)
    check("⑩ 尺寸已一致 → 无需处理", (v6.ok, v6.reason), (True, "尺寸已一致"))

    # ── 【3】补边本身：只加黑边，坐标不动 ────────────────────────────────────
    print("\n【3】pad_to_ref：右下补黑边，路线像素数量与坐标都不变")
    out = pad_to_ref(narrow_ok, ref.shape)
    m_before = route_mask(narrow_ok, codes)
    m_after = route_mask(out, codes)
    check("① 输出尺寸 == map.png", out.shape, ref.shape)
    check_true("② 左上角原内容逐像素相同",
               np.array_equal(out[:narrow_ok.shape[0], :narrow_ok.shape[1]], narrow_ok))
    check_true("③ 新增区域全黑（与录制器 expand_canvas 一致）",
               not out[:, narrow_ok.shape[1]:].any() and not out[narrow_ok.shape[0]:].any())
    check("④ 路线像素数量不变", int(m_after.sum()), int(m_before.sum()))
    check_true("⑤ 路线像素坐标不变",
               np.array_equal(m_after[:m_before.shape[0], :m_before.shape[1]], m_before))

    # ── 【4】真机数据回归（有就查，没有就说明跳过）──────────────────────────
    print("\n【4】真机数据：扫现有地图，断言「待修的都在 (0,0) 对齐、没有判不了的」")
    real = resource_path(os.path.join("minimaps", "废铁2"))
    if os.path.exists(os.path.join(real, "map.png")):
        p = plan_map("废铁2", cfg)
        check("① 真机图里没有「无法判定」的文件", len(p["refused"]), 0)
        bad = [n for _p, n, _i, v in p["jobs"] if (v.dx, v.dy) != (0, 0)]
        check("② 每个待补边文件都恰好是 (0,0) 对齐", bad, [])
        print(f"      （真机待补边 {len(p['jobs'])} 个："
              f"{[n for _p, n, _i, _v in p['jobs']]}；已一致 {len(p['ok_files'])} 个）")
    else:
        print("  [SKIP] minimaps/废铁2 不在本机 —— 真机回归跳过（CI 里正常）")

    # ── 【5】CLI：默认（体检）不写盘 ─────────────────────────────────────────
    print("\n【5】CLI 默认只体检：一个字节都不写")
    if os.path.isdir(_tmp_dir):
        shutil.rmtree(_tmp_dir)
    os.makedirs(_tmp_dir, exist_ok=True)
    try:
        ref_syn = make_ref()
        narrow = ref_syn[:, :70].copy()
        narrow[25:27, 25:27] = route_color
        imwrite_unicode(os.path.join(_tmp_dir, "map.png"), ref_syn)
        imwrite_unicode(os.path.join(_tmp_dir, "route1.png"), narrow)
        before = snapshot(_tmp_dir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--map", _tmp_name])
        check("① 退出码 0（有待修项不算失败）", rc, 0)
        check_true("② 报告里说得出「待补边」", "待补边" in buf.getvalue())
        check("③ 文件字节未变、也没建备份目录", snapshot(_tmp_dir), before)

        # ── 【6】CLI --apply：补边 + 备份 + 路线像素守恒 ─────────────────────
        print("\n【6】CLI --apply：补边写盘 + 先备份 + 路线像素守恒")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--map", _tmp_name, "--apply"])
        check("① 退出码 0", rc, 0)
        back = imread_unicode(os.path.join(_tmp_dir, "route1.png"))
        check("② 尺寸补到 map.png", back.shape, ref_syn.shape)
        baks = [d for d in os.listdir(_tmp_dir) if d.startswith("_pad_backup_")]
        check("③ 建了备份目录", len(baks), 1)
        bak = os.path.join(_tmp_dir, baks[0])
        check("④ 备份里是**原文件**逐字节副本",
              digest(os.path.join(bak, "route1.png")), before["route1.png"])
        m_narrow = route_mask(narrow, codes)
        m_back = route_mask(back, codes)
        check("⑤ 路线像素数量守恒", int(m_back.sum()), int(m_narrow.sum()))
        check_true("⑥ 路线像素坐标未动",
                   np.array_equal(m_back[:m_narrow.shape[0], :m_narrow.shape[1]], m_narrow))
        check_true("⑦ 复检：尺寸判据不再报不一致",
                   back.shape[:2] == ref_syn.shape[:2])

        # ── 【7】CLI --restore：逐字节还原 ───────────────────────────────────
        print("\n【7】CLI --restore：从备份逐字节还原")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--map", _tmp_name, "--restore"])
        check("① 退出码 0", rc, 0)
        check("② route1.png 还原成原来的窄画布（字节相同）",
              digest(os.path.join(_tmp_dir, "route1.png")), before["route1.png"])

        # ── 【8】fail-closed：错位的窄文件不许修 ─────────────────────────────
        print("\n【8】fail-closed：底图偏了的窄文件 → 拒绝并退出码 1")
        shutil.rmtree(_tmp_dir)
        os.makedirs(_tmp_dir, exist_ok=True)
        imwrite_unicode(os.path.join(_tmp_dir, "map.png"), ref_syn)
        imwrite_unicode(os.path.join(_tmp_dir, "route2.png"), ref_syn[:, 5:75].copy())
        before8 = snapshot(_tmp_dir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--map", _tmp_name, "--apply"])
        check("① 退出码 1（有无法判定的项）", rc, 1)
        check("② 文件没被改动", snapshot(_tmp_dir), before8)
        check("③ 也没留下备份目录", [d for d in os.listdir(_tmp_dir)
                                      if d.startswith("_pad_backup_")], [])
        check_true("④ 报告里点名了偏移量", "dx=5" in buf.getvalue())

        # ── 【9】env fail-closed：点名的图没有 map.png ───────────────────────
        print("\n【9】没有 map.png 的图 → 退出码 1（判不了就说判不了）")
        os.remove(os.path.join(_tmp_dir, "map.png"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--map", _tmp_name])
        check("① 退出码 1", rc, 1)
        check_true("② 报告说清了原因", "没有 map.png" in buf.getvalue())
    finally:
        if os.path.isdir(_tmp_dir):
            shutil.rmtree(_tmp_dir, ignore_errors=True)

    print("\n" + "=" * 68)
    if _fail:
        print(f"❌ 失败 {_fail} 项")
    else:
        print("✅ 全部通过")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main_check())
