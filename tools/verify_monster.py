# -*- coding: utf-8 -*-
"""
怪物模板 —— 离线验证器（2026-09-12）

回答一个问题：「自动抠出来的模板，挂机时能不能准确识别到怪？」

做法（与引擎完全同逻辑）：
- 按引擎同款方式加载 monster/<名>/ 全部模板（含自动水平翻转）；
- 对 debug/ 里的留档帧（截模板时会自动留档原始画面）跑引擎自己的
  get_monsters_in_range()（轮廓匹配 + 阈值 + NMS，一模一样）；
- 把识别到的怪框出来存图，肉眼核对「框住的是不是真的怪」。

判读：
- 画面里有怪的帧 → 应该被框住（分数 < monster_detect.diff_thres）；
- 框住的不是怪（背景图案）→ 模板会误报，需要重截；
- 画面里有怪却没框住 → 模板识别不动（看分数分布决定重截或调阈值）。

用法：python -m tools.verify_monster            # 跑最近 5 张留档帧
      python -m tools.verify_monster --frames 10
      python -m tools.verify_monster --only 三眼章鱼   # 只验证这一种怪的模板
"""
import argparse
import glob
import os
import sys

import cv2
import numpy as np

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import src.engine.MapleStoryAutoLevelUp as eng          # noqa: E402
from src.utils.common import (load_yaml, override_cfg,   # noqa: E402
                              load_image, crop_frame_to_client,
                              imread_unicode, imwrite_unicode, flatten_alpha_to_green)

OUT_DIR = os.path.join(REPO_ROOT, "debug")


def load_cfg():
    cfg = override_cfg(load_yaml(os.path.join(REPO_ROOT, "config", "config_default.yaml")),
                       load_yaml(os.path.join(REPO_ROOT, "config", "config_custom.yaml"))
                       if os.path.exists(os.path.join(REPO_ROOT, "config", "config_custom.yaml"))
                       else {})
    return cfg


def build_monster_templates(only=None, cfg=None, from_dir=None):
    """按引擎 load_config **同款方式**加载模板（含多尺度 scales + 自动水平翻转）。

    ⚠️ 2026-10-14 修：原来这里自己写了一套 load_image + get_mask + flip。
       引擎改成「IMREAD_UNCHANGED + flatten_alpha_to_green + _mob_template_variants」
       之后，这里没跟着改 ⇒ 验证器与引擎**判据不一致**：
       透明底素材在本工具里透明区被读成**黑**（IMREAD_COLOR 丢 alpha），
       contour_only 模式下整张矩形都算轮廓 ⇒ 验证结果和挂机时的真实行为对不上。
       现在改为直接调引擎那两个方法（单一权威源）。
    """
    info = {}
    scales = eng.MapleStoryAutoBot._mob_scales((cfg or {}).get("monster_detect"))
    if from_dir:
        # 外部素材目录（例如从 maplestory.wiki 下回来的官方 PNG）：
        # 每个目录当成一种怪，名字取目录名 —— 用来跟仓库里现用的模板做 A/B。
        # 支持逗号分隔多个目录（比如「官方素材 + 仓库现用」合起来看覆盖率）。
        for one in [s.strip() for s in from_dir.split(",") if s.strip()]:
            if not os.path.isabs(one):
                one = os.path.join(REPO_ROOT, one)
            name = os.path.basename(os.path.normpath(one))
            imgs = []
            for file in sorted(glob.glob(os.path.join(one, "*.png"))):
                try:
                    raw = load_image(file, cv2.IMREAD_UNCHANGED)
                    imgs.extend(eng.MapleStoryAutoBot._mob_template_variants(
                        flatten_alpha_to_green(raw), scales))
                except Exception as e:                              # noqa: BLE001
                    print(f"  [跳过] {os.path.basename(file)}：{e!r}")
            if imgs:
                info[name] = imgs
            else:
                print(f"  [警告] 目录里没有可用 PNG：{one}")
        return info

    base = os.path.join(REPO_ROOT, "monster")
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d)
        if not os.path.isdir(p) or d.startswith('_'):
            continue
        if only and d != only:
            continue
        imgs = []
        for file in sorted(glob.glob(os.path.join(p, f"{d}*.png"))):
            try:
                raw = load_image(file, cv2.IMREAD_UNCHANGED)
                imgs.extend(eng.MapleStoryAutoBot._mob_template_variants(
                    flatten_alpha_to_green(raw), scales))
            except Exception as e:                                  # noqa: BLE001
                print(f"  [跳过] {os.path.basename(file)}：{e!r}")
        if imgs:
            info[d] = imgs
    return info


def locate_player(cfg, raw, client):
    """名字定位角色（与引擎同方法）；定位不到就回退到画面中心。"""
    try:
        from tools.verify_nametag import build_probe, run_one
        bot = build_probe(cfg)
        bot.img_frame_gray = cv2.cvtColor(client, cv2.COLOR_BGR2GRAY)
        bot.img_frame_debug = client.copy()
        bot.is_first_frame = True
        bot.loc_nametag = (0, 0)
        _, loc_player, _score, _dbg, err = run_one(bot, cfg, raw)
        if loc_player:
            return loc_player, True
    except Exception:
        pass
    ch, cw = client.shape[:2]
    return (cw // 2, ch // 2), False


def main():
    ap = argparse.ArgumentParser(description="怪物模板离线验证（与引擎同逻辑）")
    ap.add_argument("--frames", type=int, default=1,
                    help="跑最近 N 张留档帧（默认 1 = 只看最新一张；旧帧是旧模板时代的，没参考价值）")
    ap.add_argument("--only", default=None, help="只验证这一种怪的模板（默认全部）")
    ap.add_argument("--thres", type=float, default=None,
                    help="临时覆盖 monster_detect.diff_thres（默认读配置 0.01）")
    ap.add_argument("--from", dest="from_dir", default=None,
                    help="模板目录覆盖：把这个目录里的 *.png 当成一种怪来验证"
                         "（例如官方素材 D:\\Download\\Ligator），不动仓库里的模板")
    ap.add_argument("--scales", default=None,
                    help="临时覆盖 monster_detect.scales，如 1.0 或 0.7,0.85,1.0,1.15")
    a = ap.parse_args()

    cfg = load_cfg()
    if a.thres is not None:
        cfg["monster_detect"]["diff_thres"] = a.thres
    if a.scales:
        cfg["monster_detect"]["scales"] = [float(s) for s in a.scales.split(",") if s.strip()]
    thres = cfg["monster_detect"]["diff_thres"]

    templates = build_monster_templates(a.only, cfg=cfg, from_dir=a.from_dir)
    if not templates:
        print("[错误] monster/ 下没有模板。先用主界面 F3 截取。")
        return 2
    total_tpl = sum(len(v) for v in templates.values())   # 含翻转

    paths = sorted(glob.glob(os.path.join(REPO_ROOT, "debug", "capture_raw_*.png")),
                   key=os.path.getmtime)
    if a.frames:
        paths = paths[-a.frames:]
    if not paths:
        print("[错误] debug/ 里没有留档帧。截模板时/挂机时会自动留档。")
        return 2

    # 清掉上一次的验证图（标注图与旧汇总）—— 只保留本次结果，避免新旧混淆
    for old in glob.glob(os.path.join(OUT_DIR, "verify_monster_*.png")):
        try:
            os.remove(old)
        except Exception:
            pass

    print("=" * 76)
    print(" 怪物模板离线验证（与引擎同逻辑：轮廓匹配 + NMS）")
    print("=" * 76)
    print(f" 模板: " + ", ".join(f"{k}({len(v)}帧=原图×镜像×尺度)" for k, v in templates.items()))
    print(f" 模式: {cfg['monster_detect']['mode']}   "
          f"scales: {cfg['monster_detect'].get('scales', [1.0])}   "
          f"色校验: {bool(cfg['monster_detect'].get('verify_color', True))}"
          f"(tol={cfg['monster_detect'].get('verify_color_tol', 30)}, "
          f"min_ratio={cfg['monster_detect'].get('verify_min_ratio', 0.75)})")
    if a.from_dir:
        print(f" ⚠️ 模板目录覆盖自: {a.from_dir}（仓库里的 monster/ 未被使用）")
    print(f" 判定阈值 score < {thres}   留档帧 {len(paths)} 张（跑最近 {min(len(paths), a.frames or len(paths))} 张）")
    print()

    bot = object.__new__(eng.MapleStoryAutoBot)
    bot.cfg = cfg
    bot.monsters_info = templates
    ch_size = tuple(cfg["game_window"]["size"])
    ch_w, ch_h = ch_size[1], ch_size[0]

    hit_frames, miss_frames = 0, 0
    outs = []
    for p in paths[-(a.frames or len(paths)):]:
        raw = imread_unicode(p, cv2.IMREAD_COLOR)
        if raw is None:
            continue
        client, msg = crop_frame_to_client(raw, ch_size, cfg["game_window"]["title_bar_height"])
        if client is None:
            print(f"{os.path.basename(p):<42}  跳过（{msg}）")
            continue
        loc_player, located = locate_player(cfg, raw, client)
        bot.img_frame = client
        bot.loc_player = loc_player
        bot.img_frame_debug = client.copy()   # 引擎会在 debug 画布上画检测框

        monsters = bot.get_monsters_in_range((0, 0), (ch_w, ch_h))

        dbg = client.copy()
        # 画出攻击范围（实际挂机只打这个范围内的怪）
        try:
            if cfg["bot"]["attack"] == "aoe_skill":
                dx = cfg["aoe_skill"]["range_x"] // 2
                dy = cfg["aoe_skill"]["range_y"] // 2
            else:
                dx = cfg["directional_attack"]["range_x"]
                dy = cfg["directional_attack"]["range_y"]
            cv2.rectangle(dbg, (max(0, loc_player[0] - dx), max(0, loc_player[1] - dy)),
                          (min(ch_w, loc_player[0] + dx), min(ch_h, loc_player[1] + dy)),
                          (255, 200, 0), 1)
        except Exception:
            pass
        for m in monsters:
            (mx, my), (mh, mw) = m["position"], m["size"]
            cv2.rectangle(dbg, (mx, my), (mx + mw, my + mh), (0, 0, 255), 2)
            cv2.putText(dbg, f"{m['name']} {m['score']:.4f}", (mx, max(12, my - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
        cv2.circle(dbg, (int(loc_player[0]), int(loc_player[1])), 4, (0, 255, 0), -1)
        out_path = os.path.join(OUT_DIR, "verify_monster_" + os.path.basename(p))
        imwrite_unicode(out_path, dbg)
        outs.append(out_path)

        name = os.path.basename(p)
        if monsters:
            hit_frames += 1
            detail = ", ".join(f"{m['name']}@{m['position']}({m['score']:.4f})"
                               for m in monsters)
            print(f"{name:<42}  识别 {len(monsters)} 只: {detail}")
        else:
            miss_frames += 1
            print(f"{name:<42}  未识别到怪（若画面里确实有怪 → 模板需重截）")

    print("-" * 76)
    print(f" 有识别的帧: {hit_frames}   未识别: {miss_frames}")
    print(f" 标注图: debug\\verify_monster_*.png（红框=识别到的怪，绿点=角色，"
          f"蓝框=攻击范围）")

    # 拼接本轮全部标注图为一张长图并自动打开（2026-09-12 用户建议：不用自己翻）
    if outs:
        imgs = [im for im in (imread_unicode(p) for p in outs) if im is not None]
        if imgs:
            w = max(im.shape[1] for im in imgs)
            padded = []
            for im in imgs:
                if im.shape[1] < w:
                    pad = np.full((im.shape[0], w - im.shape[1], 3), 30, np.uint8)
                    im = np.hstack([im, pad])
                padded.append(im)
            if len(padded) == 1:
                summary_path = os.path.join(OUT_DIR, "verify_monster_最新.png")
                imwrite_unicode(summary_path, padded[0])
                print(f" 验证结果图已生成: {os.path.relpath(summary_path, REPO_ROOT)}")
            else:
                canvas = np.vstack(padded)
                ts = os.path.basename(outs[0]).replace("verify_monster_capture_raw_", "").replace(".png", "")
                summary_path = os.path.join(OUT_DIR, f"verify_monster_汇总_{ts}.png")
                imwrite_unicode(summary_path, canvas)
                print(f" 汇总图（{len(imgs)} 帧纵向拼接）已生成: {os.path.relpath(summary_path, REPO_ROOT)}")
            try:
                os.startfile(summary_path)   # 自动打开给用户核对
            except Exception:
                pass
    print()
    print(" 请逐张打开标注图核对：红框框住的是不是真的怪？")
    print(" · 框对了 → 模板可用")
    print(" · 框住了背景 → 模板误报，需重截更紧的框")
    print(" · 画面有怪却没框 → 用 --thres 0.02 试试放宽阈值（仍不行就重截模板）")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    try:
        input("\n[结束] 按回车关闭窗口 ")
    except Exception:
        pass
    sys.exit(code)
