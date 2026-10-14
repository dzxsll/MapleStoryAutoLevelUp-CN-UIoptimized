'''
Utility functions
'''
# Standard Import
import cv2
import datetime
import os
import time
from collections import defaultdict

# Libarary Import
import numpy as np
import yaml
from ruamel.yaml import YAML
# 中文绘制：cv2.putText 用的 Hershey 字体**画不了中文**（会变成 ???），
# 所以凡是要在画面上写中文的地方都走 PIL（见下面的 put_text_cn）。
from PIL import Image, ImageDraw, ImageFont

# Windows 平台专有依赖（本项目只跑国服 Windows 客户端，已移除 macOS 分支）
import win32gui
import win32con
import win32api
import win32process

# Local import
from src.utils.logger import logger
from src.utils.paths import (APP_ROOT, IS_FROZEN, resource_path,
                             ensure_dir, ensure_parent)
from src.input.InterceptionController import click_at

def active_config_path():
    """当前活动的用户配置方案路径（2026-09-12 配置方案改造后，用户配置不再是
    固定的 config_custom.yaml，而是主界面「配置方案」选中的那一份）。
    读主界面保存的 ui_state；不存在/文件缺失时回退 config_custom.yaml，再回退 None。

    ⚠️ 打包运行（IS_FROZEN）时**只认自己目录下**的配置（2026-09-17）：
    ui_state 里存的是**绝对路径**（开发机上就是源码目录里的方案文件）。
    直接照用会出现「exe 跑到源码目录去读写配置」—— 打包产物本该自给自足，
    而且会**污染开发机的实际配置**（实测：启动一次就改写了源码目录的方案文件）。
    所以路径不在 APP_ROOT 下时一律忽略，退回本目录的 config_custom.yaml。
    """
    import json
    state_p = os.path.join(os.path.expanduser("~"), ".maplebot_ui_state.json")
    try:
        with open(state_p, encoding="utf-8") as f:
            p = json.load(f).get("last_config_path")
        if p and os.path.exists(p):
            if IS_FROZEN:
                # 只允许 APP_ROOT 内部的路径，保证整个目录拷走就能用
                try:
                    inside = os.path.commonpath(
                        [os.path.abspath(p), os.path.abspath(APP_ROOT)]
                    ) == os.path.abspath(APP_ROOT)
                except ValueError:      # 不同盘符，commonpath 会抛
                    inside = False
                if not inside:
                    # 外部路径 → 先看**本程序目录**里有没有同名方案（开发版把
                    # 个人方案一起打进去了，这时就该用它），没有才算真没有。
                    cand = os.path.join(APP_ROOT, "config", os.path.basename(p))
                    if os.path.exists(cand):
                        logger.info(
                            f"[配置] ui_state 指向外部路径，改用本程序目录下的同名方案：{cand}")
                        return cand
                    logger.info(f"[配置] 忽略 ui_state 里的外部配置路径（不在本程序目录下）：{p}")
                    p = None
            if p:
                return p
    except Exception:
        pass
    fallback = os.path.join("config", "config_custom.yaml")
    return fallback if os.path.exists(fallback) else None


def load_yaml(path):
    path = resource_path(path)
    with open(path, 'r', encoding='utf-8') as f:
        logger.info(f"Load yaml: {path}")
        data = yaml.safe_load(f) or {}
        return convert_lists_to_tuples(data)

def load_yaml_with_comments(path):
    path = resource_path(path)
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, 'r', encoding='utf-8') as f:
        data = yaml.load(f)

    field_comments = defaultdict(dict)
    section_comments = {}

    for title, sub in data.items():
        # Extract section comment (before key)
        if sub.ca.comment and sub.ca.comment[1]:
            section_comment_lines = [line.value.strip('#').strip() for line in sub.ca.comment[1]]
            section_comments[title] = "\n".join(section_comment_lines)

        # Extract field-level comments
        if hasattr(sub, 'ca'):
            for key in sub:
                comment = sub.ca.items.get(key)
                if comment and comment[2]:
                    field_comments[title][key] = comment[2].value.strip('#').strip()

    return data, dict(field_comments), section_comments

def save_yaml(data, path):
    data = convert_tuples_to_lists(data)
    path = ensure_parent(path)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False)
    logger.info(f"Save yaml: {path}")

def get_cfg_diff(base, current):
    """
    Recursively compute the diff between base and current configs.
    Return only the values from current that are different.
    """
    diff = {}
    for key in current:
        if key not in base:
            diff[key] = current[key]
        elif isinstance(current[key], dict) and isinstance(base.get(key), dict):
            sub_diff = get_cfg_diff(base[key], current[key])  # recursive call
            if sub_diff:
                diff[key] = sub_diff
        else:
            norm_current = normalize(current[key])
            norm_base = normalize(base.get(key))
            if norm_current != norm_base:
                diff[key] = current[key]
    return diff

def normalize(value):
    """
    Normalize value for comparison:
    - Convert tuples to lists
    - Recursively normalize lists and dicts
    """
    if isinstance(value, tuple):
        return [normalize(v) for v in value]
    elif isinstance(value, list):
        return [normalize(v) for v in value]
    elif isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    else:
        return value

def convert_tuples_to_lists(obj):
    if isinstance(obj, dict):
        return {k: convert_tuples_to_lists(v) for k, v in obj.items()}
    elif isinstance(obj, tuple):
        return list(obj)
    elif isinstance(obj, list):
        return [convert_tuples_to_lists(i) for i in obj]
    else:
        return obj

def override_cfg(base, override):
    '''
    override_cfg (in-place)
    Modifies `base` directly by overriding keys from `override`.
    '''
    for k, v in override.items():
        if (
            k in base and isinstance(base[k], dict)
            and isinstance(v, dict)
        ):
            override_cfg(base[k], v)  # recursive override
        else:
            base[k] = v  # direct override or new key
    return base

def convert_lists_to_tuples(obj):
    if isinstance(obj, list):
        return tuple(convert_lists_to_tuples(x) for x in obj)
    elif isinstance(obj, dict):
        return {k: convert_lists_to_tuples(v) for k, v in obj.items()}
    else:
        return obj

def imread_unicode(path, mode=cv2.IMREAD_COLOR):
    '''
    读图片（对中文路径安全）。

    为什么不能直接用 cv2.imread：它在 Windows 上走 ANSI 文件 API，
    本机实测（OpenCV 5.0.0）路径里只要含中文就直接返回 None。而本项目很可能被
    放在含中文的目录下（如「D:\游戏工具\冒险岛自动练级」）—— 不处理的话
    「模板一张都读不出来」，且症状是具有迷惑性的「文件明明在，却读不到」。
    改用「按字节读文件 + imdecode」，彻底绕开路径编码。
    '''
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError as e:
        logger.debug(f"[imread_unicode] 读文件失败: {path} ({e})")
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, mode)

def imwrite_unicode(path, img, params=None):
    '''
    写图片（对中文路径安全），返回是否成功。

    同理 cv2.imwrite 在中文路径下会返回 False 且根本不生成文件，
    而且不抛异常 —— 这是最坑的一点（写过就以为成功了）。
    改用「imencode 编码到内存 + 按字节写文件」。
    '''
    ext = (os.path.splitext(path)[1] or ".png").lower()
    try:
        ok, buf = cv2.imencode(ext, img, params or [])
    except cv2.error as e:
        logger.error(f"[imwrite_unicode] 编码失败: {path} ({e})")
        return False
    if not ok:
        logger.error(f"[imwrite_unicode] 编码失败: {path}")
        return False
    try:
        buf.tofile(path)
    except OSError as e:
        logger.error(f"[imwrite_unicode] 写文件失败: {path} ({e})")
        return False
    return True

def load_image(path, mode=cv2.IMREAD_COLOR):
    '''
    Load image from disk and verify existence.
    路径相对「应用根目录」解析（见 src/utils/paths.py），因此打包后也能找到模板。
    '''
    path = resource_path(path)
    if not os.path.exists(path):
        logger.error(f"Image not found: {path}")
        logger.error(f"（应用根目录 {APP_ROOT}；模板需放在该目录下，如 "
                     f"{os.path.join(APP_ROOT, 'nametag', 'xx.png')}）")
        raise FileNotFoundError(f"Image not found: {path}")

    # Load image（走中文路径安全版）
    img = imread_unicode(path, mode)
    if img is None:
        logger.error(f"Failed to load image file: {path}")
        raise ValueError(f"Failed to load image: {path}")

    logger.info(f"Loaded image: {path}")

    return img

def nms(monsters, iou_threshold=0.3):
    '''
    Apply Non-Maximum Suppression (NMS) to remove overlapping detections.

    Parameters:
    - monsters: List of dictionaries, each representing a detected monster with:
        - "position": (x, y) top-left corner
        - "size": (width, height)
        - "score": template matching score; **越小越好**（TM_SQDIFF_NORMED，0 = 完全一致）
    - iou_threshold: Float, intersection-over-union threshold to suppress overlapping boxes

    Returns:
    - List of filtered monster dictionaries after applying NMS
    '''
    boxes = []
    for m in monsters:
        x, y = m["position"]
        w, h = m["size"]
        # [x1, y1, x2, y2, score, original_data]
        boxes.append([x, y, x + w, y + h, m["score"], m])

    # ★ 2026-10-14 改：按 score **升序**（越小越好）。
    #   `score` 是 `TM_SQDIFF_NORMED` 的返回值（0 = 完全一致）⇒ 最优是**最小**的。
    #   原来写的是 reverse=True（降序），于是同一簇重叠框里被留下的是**最差**那个；
    #   更糟的是 `with_enemy_hp_bar` 分支给血条框的 score 固定 `1.0`（最差），
    #   降序下血条框会压过真正匹配上的模板框。参考项目
    #   zhoufanglu/maplestory-auto-ds 的 `nms` 也是升序。
    #   ⚠️ 改动只影响"同簇里哪个框/哪个分数存活"，不影响框的数量级。
    #   用户拍板 2026-10-14：「升序」。
    boxes.sort(key=lambda x: x[4])

    keep = []
    while boxes:
        best = boxes.pop(0)
        keep.append(best[5])  # original monster_info

        boxes = [b for b in boxes if get_iou(best, b) < iou_threshold]

    return keep

def get_iou(box1, box2):
    '''
    Calculate the Intersection over Union (IoU) between two bounding boxes.

    Each box is expected to be a tuple or list with at least 4 values:
    (x1, y1, x2, y2), where:
        - (x1, y1) is the top-left corner
        - (x2, y2) is the bottom-right corner

    Returns:
        A float representing the IoU value (0.0 ~ 1.0).
        If there is no overlap, returns 0.0.
    '''
    x1, y1, x2, y2 = box1[:4]
    x1_p, y1_p, x2_p, y2_p = box2[:4]

    inter_x1 = max(x1, x1_p)
    inter_y1 = max(y1, y1_p)
    inter_x2 = min(x2, x2_p)
    inter_y2 = min(y2, y2_p)

    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0

    inter_area = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area1 = (x2 - x1) * (y2 - y1)
    area2 = (x2_p - x1_p) * (y2_p - y1_p)
    union = area1 + area2 - inter_area

    return inter_area / union

def screenshot(img, suffix="screenshot"):
    '''
    Save the given image as a screenshot file.

    Parameters:
    - img: numpy array (image to save).

    Behavior:
    - Saves the image to the "screenshot/" directory（相对应用根目录）with the current timestamp as filename.
    '''

    if img is None:
        return

    # ensure directory exists
    shot_dir = ensure_dir("screenshot")

    # Generate timestamp string
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = os.path.join(shot_dir, f"{timestamp}_{suffix}.png")
    imwrite_unicode(filename, img)
    logger.info(f"[screenshot] save to {filename}")

# ── 中文绘制（2026-09-12 加）────────────────────────────────────────────
# 背景：`cv2.putText` 只支持 Hershey 矢量字体，**画不了中文** ——
# 直接传中文会渲染成一串 "???"。项目要在调试图上标中文（如「攻击范围」
# 「怪物检测区」），所以这里用 PIL 画中文，再写回 numpy。
_CN_FONT_CACHE = {}


def _get_cn_font(size):
    """按字号取中文字体（优先微软雅黑），带缓存。找不到时退回 PIL 默认字体。"""
    size = int(size)
    if size in _CN_FONT_CACHE:
        return _CN_FONT_CACHE[size]
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",    # 微软雅黑（最常用）
        r"C:\Windows\Fonts\msyhbd.ttc",  # 微软雅黑 Bold
        r"C:\Windows\Fonts\simhei.ttf",  # 黑体
        r"C:\Windows\Fonts\simsun.ttc",  # 宋体
    ]
    font = None
    for path in candidates:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        logger.warning("[put_text_cn] 没找到系统中文字体，退回 PIL 默认字体（中文可能显示为方块）")
        font = ImageFont.load_default()
    _CN_FONT_CACHE[size] = font
    return font


def put_text_cn(img, text, org, color, font_size=20, thickness=1):
    """
    在 BGR 图上写**中文**文字（cv2.putText 画不了中文，会变 ???）。

    做法：把**文字所在的那一小块 ROI** 交给 PIL 画字，再**原地写回** numpy
    （用 `roi[:, :, :] = ...` 而不是返回新数组，保证调用方持有的引用不失效）。

    ⚠️ 千万不要改回「整图转换」：实测 1366x768 整图 BGR→RGB→BGR 约 **8ms/次**，
       而 viz 打开时每帧要画 2~3 个标签（攻击范围 / 索敌范围 / 路线图的小地图
       匹配度），合计约 **21ms** —— 30fps 每帧只有 33.3ms 预算，光画字就吃掉
       63%，FPS 会从 30 掉到 12 左右。改 ROI 局部转换后实测约 **0.1ms/次**。

    参数：
    - img:       BGR numpy 数组，会被原地修改
    - text:      要写的文字（中文/英文都行）
    - org:       (x, y) = 文字**左上角**（PIL 口径；cv2.putText 用的是左下角基线）
    - color:     (B, G, R)
    - font_size: 像素字号
    - thickness: 与 cv2 的 thickness 同义，这里换算成描边宽度（1 = 不描边）
    """
    # 防御性兜底（2026-09-12 实测踩过）：调用方偶尔会传 None，比如 viz 关闭时
    # 引擎层漏判 is_show_debug_window，导致 img_route_debug 是 None 传进来。
    # 调试可视化本来就允许没图，悄悄退出即可，不要把主循环带崩。
    if img is None:
        return
    font = _get_cn_font(font_size)
    x, y = int(org[0]), int(org[1])
    stroke = max(0, int(thickness) - 1)
    rgb = (int(color[2]), int(color[1]), int(color[0]))

    # 先量出文字实际占的像素范围（含描边），好确定要裁多小的 ROI
    try:
        bbox = font.getbbox(text, stroke_width=stroke)   # (x0, y0, x1, y1)
    except TypeError:                                    # 老版 Pillow 无此参数
        bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    if tw <= 0 or th <= 0:
        return

    # 文字在 img 里的真实落点 = org + bbox 的原点偏移
    tx = x + bbox[0]
    ty = y + bbox[1]
    pad = stroke + 1
    x0 = max(0, tx - pad)
    y0 = max(0, ty - pad)
    x1 = min(img.shape[1], tx + tw + pad)
    y1 = min(img.shape[0], ty + th + pad)
    if x1 <= x0 or y1 <= y0:
        return   # 文字整体落在图外，画不了

    # roi 是 img 的视图，下面原地写回即等于改 img 本身
    roi = img[y0:y1, x0:x1]
    roi_pil = Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(roi_pil)
    # ⚠️ 锚点必须是「原始 org 相对 ROI 左上角」的 (x-x0, y-y0)，
    #    不能写成 (tx-x0, ty-y0)：draw.text 内部会自己再叠一次 bbox 偏移，
    #    那样等于偏移了两次，字会跑偏（实测与整图版最大像素差 215）。
    #    bbox 只用来算 ROI 范围，不参与锚点。
    draw.text((x - x0, y - y0), text, font=font, fill=rgb,
              stroke_width=stroke, stroke_fill=rgb)
    roi[:, :, :] = cv2.cvtColor(np.array(roi_pil), cv2.COLOR_RGB2BGR)


def draw_rectangle(img, top_left, size, color, text,
                   thickness=2, text_height=0.7):
    '''
    Draws a rectangle with an text label.

    Parameters:
    - img: The image on which to draw (numpy array).
    - top_left: Tuple (x, y), the top-left corner of the rectangle.
    - size: Tuple (height, width) of the rectangle.
    - color: Tuple (B, G, R), color of the rectangle and text.
    - text: String to display above the rectangle.
            **含中文时自动改走 PIL**（cv2.putText 画不了中文，见 put_text_cn）。
    '''
    # 防御性兜底（同 put_text_cn）：调用方偶尔传 None（viz 关闭但调用层漏判），
    # 调试绘制本来就可以没图，悄悄退出即可，不要把主循环带崩。
    if img is None:
        return
    bottom_right = (top_left[0] + size[1],
                    top_left[1] + size[0])
    cv2.rectangle(img, top_left, bottom_right, color, thickness)
    if not text:
        return
    if text.isascii():
        cv2.putText(img, text, (top_left[0], top_left[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, text_height, color, thickness)
    else:
        # 中文：走 PIL。cv2 的 text_height 是缩放系数（1.0 ≈ 30px 高），
        # 这里换算成像素字号；PIL 的 org 是左上角，所以要多减一个字号高度。
        font_size = max(12, int(round(text_height * 30)))
        put_text_cn(img, text,
                    (top_left[0], max(0, top_left[1] - 18 - font_size)),
                    color, font_size=font_size, thickness=thickness)

def pad_to_size(img, size, pad_value=0):
    '''
    pad_to_size
    '''
    h_img, w_img = img.shape[:2]
    h_target, w_target = size

    pad_h = max(0, h_target - h_img)
    pad_w = max(0, w_target - w_img)

    if pad_h > 0 or pad_w > 0:
        img = cv2.copyMakeBorder(
            img,
            top   = pad_h // 2,
            bottom= pad_h - pad_h // 2,
            left  = pad_w // 2,
            right = pad_w - pad_w // 2,
            borderType=cv2.BORDER_CONSTANT,
            value=pad_value
        )

    return img

def find_pattern_sqdiff(
        img, img_pattern,
        last_result=None,
        mask=None,
        local_search_radius=50,
        global_threshold=0.4,
        allow_global_fallback=True
    ):
    '''
    Perform masked template matching using SQDIFF_NORMED method.

    The function searches for the best matching location of img_pattern inside img.
    It automatically converts the pattern to grayscale and generates a mask to ignore
    pure white (or near-white) pixels in the template, treating them as transparent background.

    Parameters:
    - img: Target search image (numpy array), can be grayscale or BGR.
    - img_pattern: Template image to search for (numpy array, BGR).
    - allow_global_fallback: 局部搜索不达标时，是否允许**退回全图搜索**。
      ⚠️ 默认 True = 保持原有行为不变（所有既有调用方不受影响）。
      设为 False 时：局部搜索不达标就直接返回局部结果，**不做全图搜索**。
      为什么要有这个开关（2026-09-27 实测）：
        全图搜索约 20ms，局部只要 0.4ms（差 50 倍）。而名字定位一次会跑
        2~4 次匹配（主匹配 + 兜底切块），**每次失败都各自回退全图** ⇒
        失败时单帧要 80ms（用户实测 95ms/帧 ≈ 10fps）。
        但"失败"往往只是那一块名牌被别的玩家盖住，并**不代表位置变了** ——
        让同一次定位里只回退一次全图就够，其余走局部即可。
      ⚠️ 只传 False、不配合"限流"会导致一次都不做全图，定位能力下降；
      调用方应自行保证"至少让第一次允许回退"。

    Returns:
    - min_loc: The top-left coordinate (x, y) of the best match position.
    - min_val: The matching score (lower = better for SQDIFF_NORMED).
    - bool: local search success or not
    '''
    # Padding if img is smaller than pattern
    img = pad_to_size(img, img_pattern.shape[:2])

    # search last result location first to speedup
    h, w = img_pattern.shape[:2]
    _local_done = False
    if last_result is not None and global_threshold > 0.0:
        lx, ly = last_result
        x0 = max(0, lx - local_search_radius)
        y0 = max(0, ly - local_search_radius)
        x1 = min(img.shape[1], lx + local_search_radius + w)
        y1 = min(img.shape[0], ly + local_search_radius + h)

        img_roi = img[y0:y1, x0:x1]
        if img_roi.shape[0] >= h and img_roi.shape[1] >= w:
            res = cv2.matchTemplate(
                    img_roi,
                    img_pattern,
                    cv2.TM_SQDIFF_NORMED,
                    mask=mask
            )
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            if min_val < global_threshold:
                return (x0 + min_loc[0], y0 + min_loc[1]), min_val, True
            # 局部搜过了但分数不达标：记住它，供禁用全图回退时返回
            _local_done = True
            _local_result = ((x0 + min_loc[0], y0 + min_loc[1]), min_val)
        else:
            _local_result = None
    else:
        _local_result = None

    # ★ 2026-09-27 加：调用方可以禁止回退全图（见参数说明）。
    #   此时返回**局部搜到的结果**（哪怕分数不达标），语义是"就地给个最像的"。
    #   ⚠️ 若连局部都没跑成（_local_result 为 None），仍走全图 ——
    #      否则就没有任何结果可返回了。
    if not allow_global_fallback and _local_result is not None:
        return _local_result[0], _local_result[1], False

    # Global fallback
    res = cv2.matchTemplate(
            img,
            img_pattern,
            cv2.TM_SQDIFF_NORMED,
            mask=mask
    )

    # Replace -inf/+inf/nan to 1.0 to avoid numerical error
    res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)

    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

    return min_loc, min_val, False

def get_mask(img, ignore_pixel_color):
    '''
    get_mask
    '''
    mask = np.all(img == ignore_pixel_color, axis=2).astype(np.uint8) * 255
    mask = cv2.bitwise_not(mask)
    return mask

def flatten_alpha_to_green(img, bg_color=(0, 255, 0), alpha_thres=128):
    '''把带 alpha 通道的图（官方素材常见的 RGBA 透明底 PNG）压成 3 通道 BGR，
    透明像素填成**绿幕** `bg_color`。

    ⚠️ 为什么填绿、不填黑（2026-10-13 从参考项目 maplestory-auto-ds 借 alpha 支持时定的）：
       参考项目 `MonsterDetector._load_monster_template` 把 alpha 外的像素置成
       `[0,0,0]`（黑）。本引擎照抄会**踩坑**：默认模式 `contour_only` 的"轮廓"就是
       黑像素，模板背景一旦是黑，整块背景立刻变成轮廓 ⇒ 等于给每张模板加了一个巨大
       的假轮廓（误报立刻变多）。填绿则与本引擎「绿幕 = 背景」的既有约定
       （`get_mask(img, (0, 255, 0))`）一致，contour_only 只看怪自己身上的黑线。

    返回：3 通道 BGR `np.ndarray`（原图是 3 通道时**原样返回**，不做拷贝）。
    形状不认识时抛 ValueError —— 由调用方决定"跳过这一张 + 记 ERROR"，不静默。
    '''
    if img is None:
        raise ValueError("flatten_alpha_to_green got None")
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.ndim == 3 and img.shape[2] == 3:
        return img
    if img.ndim == 3 and img.shape[2] == 4:
        bgr = np.ascontiguousarray(img[:, :, :3])
        bgr[img[:, :, 3] < alpha_thres] = bg_color
        return bgr
    raise ValueError(f"unsupported image shape: {img.shape}")

def to_opencv_hsv(color_hsv):
    """
    Convert HSV from standard scale:
    - Hue: 0–360
    - Saturation: 0–100
    - Value: 0–100
    to OpenCV HSV format:
    - Hue: 0–179
    - Saturation/Value: 0–255

    Args:
        color_hsv (tuple/list/np.ndarray): HSV in standard scale (H, S, V)

    Returns:
        np.ndarray: HSV in OpenCV scale
    """
    h, s, v = color_hsv
    h_opencv = round(h / 360 * 179)
    s_opencv = round(s / 100 * 255)
    v_opencv = round(v / 100 * 255)
    return np.array([h_opencv, s_opencv, v_opencv], dtype=np.uint8)

def to_standard_hsv(color_hsv):
    """
    Convert HSV from OpenCV scale to standard HSV scale.
    """
    h, s, v = color_hsv
    h_std = h / 179 * 360
    s_std = s / 255 * 100
    v_std = v / 255 * 100
    return (h_std, s_std, v_std)

def get_minimap_loc_size(img_frame):
    '''
    Detects the location and size of the minimap within the game frame.

    The function works by:
    - Thresholding the image get pure white(255,255,255) pixels.
    - Using connected components to find white-bordered regions.
    - Filtering candidates based on expected minimap size and margin rules:
        - Top, bottom, left, right margins must be 1px white lines.

    Returns:
        (x, y, w, h): Top-left coordinate and width/height of the minimap.
                    Returns None if not found.
    '''
    white = np.array([255, 255, 255])

    # Mask for pure white
    mask_white = cv2.inRange(img_frame, white, white)

    # Connected components with stats
    num_labels, labels, stats, centroids = \
        cv2.connectedComponentsWithStats(mask_white, connectivity=8)

    # Loop over components (skip label 0, which is background)
    for i in range(1, num_labels):
        x0, y0, rw, rh, area = stats[i]

        # 挡掉真正的小白块（噪声）。
        # ⚠️ 原实现卡的是 `rw < 100 or rh < 100` —— 那是照台服那套小地图尺寸定的，**国服不成立**：
        #    小地图尺寸**随地图大小变**（同一客户端下实测）：
        #        地图A（农场图）：白框 239x79，内部地形 233x76
        #        地图B（城镇）  ：白框 217x115，内部地形 211x112
        #    于是"矮"的那种（76px 高）被 100px 的线直接拒掉 —— 整条「走路」主路径
        #    （路线录制 + 走位）在这个地图版式上直接失效，而且不报错。
        #    现在改成只挡噪声级的小块，**尺寸交给下面的"四边纯白"结构判据去验** ——
        #    那条判据在两种版式上都通过了，比尺寸阈值可靠得多。
        #    ⚠️ 顺带记下：两种版式的小地图**左上角都是 (6,72)**，只有尺寸不同。
        if rw < 40 or rh < 30:
            continue

        # 小地图是固定在**画面左上角**的 HUD 元素（config 里 minimap 段的注释也这么写），
        # 用它排除画面中部的白色方框 —— 实测教训：把尺寸阈值放宽后，**登录界面**那个
        # 白边书本面板（300x454 @(563,228)）会通过"四边纯白"检查被误当成小地图。
        # 实测两种版式的白框左上角都是 **(3, 71)**；这里给足余量、只要求落在左上角一带。
        if x0 > 60 or y0 > 200:
            continue

        x1 = x0 + rw - 1
        y1 = y0 + rh - 1

        # Check 1px white top and bottom margins
        if not (np.all(img_frame[y0, x0:x0+rw] == white) and \
                np.all(img_frame[y1, x0:x0+rw] == white)):
            continue

        # Check 1px white left and right margins
        # Ensures the candidate region is framed by white borders like the minimap
        if not (np.all(img_frame[y0:y0+rh, x0] == white) and \
                np.all(img_frame[y0:y0+rh, x1] == white)):
            continue

        # Create a mask of non-white pixels
        mask_minimap = np.any(img_frame[y0:y0+rh, x0:x0+rw] != white, axis=2).astype(np.uint8)

        # Find bounding box of mask_minimap
        coords = cv2.findNonZero(mask_minimap)
        if coords is None:
            continue  # skip empty block
        x_minimap, y_minimap, w_minimap, h_minimap = cv2.boundingRect(coords)

        # Offset by original x0, y0 to get coords in original image
        x_minimap += x0
        y_minimap += y0

        # 结构校验：小地图是「细白边 + 内部整块地形底」——
        # 所以**内部非白区域的左上角应该紧贴外框**（实测两种版式都只差 3px / 1px）。
        # 实测教训：把尺寸阈值放宽后，登录界面那个大块白边面板也能通过"四边纯白"检查，
        # 但它的内部非白区域离外框很远（跑到 (563,228) 去了）—— 用这条才能挡掉。
        # （只加"外框在左上角"的位置守卫挡不住：那个面板的外框左上角也在左上角区域。）
        if (x_minimap - x0) > 10 or (y_minimap - y0) > 10:
            continue

        return x_minimap, y_minimap, w_minimap, h_minimap

    # logger.warning("Minimap not found in the game frame.")
    return None  # minimap not found

def get_player_location_on_minimap(img_minimap, minimap_player_color=(136, 255, 255),
                                   tolerance=30):
    """
    Detects the player's position on the minimap.

    The function works by:
    - Creating a binary mask of all pixels in the minimap within `tolerance` of the
    configured player color.
    - Verifying that at least 4 matching pixels are found (to avoid false positives).
    - Computing the average of these pixel coordinates to determine the center of
    the player icon on the minimap.

    【为什么需要 tolerance】（2026-09-12 实测踩坑）
      原来用 cv2.inRange 做**精确**匹配（lower == upper == 配置色），要求至少
      4 个像素**完全相等**。但小地图上的玩家黄点经过缩放 / 抗锯齿 / 游戏滤镜后，
      像素值几乎不可能和配置的 (136,255,255) 一模一样 —— 结果一个都匹配不上，
      函数返回 None，调用方 `if loc_player_minimap:` 不成立，loc_player_minimap
      就一直停在初始值 (0, 0)。
      表现：用户"一直在平台上走来走去"，但录制器画出的线全是 0 长度、
      起点永远是 (pad, pad)，看起来就是"始终没录到起点"。
      给一个容差（默认每通道 ±30）即可覆盖抗锯齿带来的偏移。

    Args:
        img_minimap: 小地图图像（BGR）
        minimap_player_color: 玩家点的 BGR 颜色
        tolerance: 每通道容差；设 0 等价于原来的精确匹配

    Returns:
        (x, y): The player's location in minimap coordinates as a tuple.
                Returns None if not enough matching pixels are found.
    """
    if tolerance:
        lower = tuple(int(max(0, c - tolerance)) for c in minimap_player_color)
        upper = tuple(int(min(255, c + tolerance)) for c in minimap_player_color)
    else:
        lower = upper = tuple(int(c) for c in minimap_player_color)
    mask = cv2.inRange(img_minimap, lower, upper)
    coords = cv2.findNonZero(mask)
    if coords is None or len(coords) < 4:
        # logger.warning(f"Fail to locate player location on minimap.")
        return None

    # Calculate the average location of the matching pixels
    # 【注意】 不能写成 coords.mean(axis=0)[0]：cv2.findNonZero 在 OpenCV 4 返回 (N,1,2)，
    #    但在 OpenCV 5（本机 5.0.0）返回 (N,2) —— 那样 [0] 会取成标量，后面 avg[0] 直接
    #    IndexError: invalid index to scalar variable（2026-09-10 实测崩过）。
    #    reshape(-1, 2) 对两种形状都成立。
    avg = coords.reshape(-1, 2).mean(axis=0)
    loc_player_minimap = (int(round(float(avg[0]))), int(round(float(avg[1]))))

    return loc_player_minimap

# 系统外壳 / 桌面类窗口 —— 标题里可能恰好含关键词，但绝不可能是游戏窗口
_SHELL_WINDOW_CLASSES = {
    "cabinetwclass",            # 资源管理器（文件夹窗口）
    "explorewclass",            # 老式资源管理器
    "progman",                  # 桌面
    "workerw",                  # 桌面（壁纸层）
    "shell_traywnd",            # 任务栏
    "shell_secondarytraywnd",   # 副屏任务栏
    "notifyiconwndclass",       # 托盘
    "tasklistthumbnailwnd",     # 任务栏缩略图
}


def list_windows_like_game(token, min_size=100):
    '''
    列出「标题含关键词、可见、且不像系统外壳」的候选窗口，按面积从大到小排序。

    返回 [{'hwnd', 'title', 'class', 'size': (w, h), 'area'}, ...]
    与标题强相关的两个函数（find_game_window_hwnd / get_game_window_title_by_token）
    都走这里，保证「找窗口」的口径一致。

    ⚠️ 必须排除**本进程自己的窗口**（2026-09-12 实测踩坑）：
       游戏窗口标题 = '冒险岛怀旧服'
       本项目主界面标题 = '冒险岛怀旧服 - 自动练级'   ← 含游戏标题这个子串
       于是主界面自己也成了"像游戏的窗口"，一旦它面积比游戏大（或游戏标题带了动态
       后缀走包含匹配），就会把**界面**当成游戏：抓它的画面、把坐标算在它身上、
       把按键发给它 —— 全程不报错。按 PID 排除本进程是最稳的判据（不依赖标题）。
    '''
    token_l = (token or "").lower()
    if not token_l:
        return []

    me = os.getpid()
    found = []

    def callback(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        title = win32gui.GetWindowText(h)
        if not title or token_l not in title.lower():
            return
        try:
            cls = win32gui.GetClassName(h)
        except Exception:
            cls = ""
        if cls.lower() in _SHELL_WINDOW_CLASSES:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(h)
        except Exception:
            pid = -1
        if pid == me:
            return          # 本进程自己的窗口（界面/面板）永远不是游戏
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
        except Exception:
            return
        w, hh = r - l, b - t
        if w < min_size or hh < min_size:
            return
        found.append({"hwnd": h, "title": title, "class": cls,
                      "size": (w, hh), "area": w * hh})

    win32gui.EnumWindows(callback, None)
    found.sort(key=lambda d: d["area"], reverse=True)
    return found


def find_game_window_hwnd(window_title):
    r'''
    按标题查找游戏窗口句柄。找不到返回 0。

    匹配顺序：
      1. 精确同名（FindWindow）—— 但要先过「不是系统外壳窗口」这一关
      2. 退化为「包含匹配」—— 国服窗口标题可能带动态后缀（地图名、角色名），精确匹配会失效；
         此时排除系统外壳窗口，并在多个候选里取面积最大的那个

    【注意】 修正说明（2026-09-10 实测发现）：
      原先「包含匹配」取的是 EnumWindows 枚举到的**第一个**命中窗口，而系统外壳窗口
      （最典型的是资源管理器）标题里极可能恰好含关键词 —— 例如项目目录名里带
      「冒险岛怀旧服」，用资源管理器打开该目录时，窗口标题就是
      `冒险岛怀旧服国服自动练级 - 文件资源管理器`，正好含配置关键词「冒险岛怀旧服」。
      一旦命中就会去抓资源管理器的画面、把坐标算到资源管理器上，且**全程不报错**。
      现在改为：排除系统外壳窗口类 + 多候选取面积最大者。
    '''
    hwnd = win32gui.FindWindow(None, window_title)
    if hwnd:
        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            cls = ""
        if cls.lower() not in _SHELL_WINDOW_CLASSES:
            return hwnd

    candidates = list_windows_like_game(window_title)
    if not candidates:
        return 0

    for c in candidates:
        if c["title"] == window_title:
            return c["hwnd"]

    best = candidates[0]
    logger.warning(
        "[find_game_window_hwnd] 精确匹配失败，改用包含匹配，"
        f"在 {len(candidates)} 个候选里取最大的：{best['title']!r} "
        f"({best['class']} {best['size'][0]}x{best['size'][1]})")
    if len(candidates) > 1:
        logger.warning("[find_game_window_hwnd] 其它候选：" +
                       "；".join(f"{c['title']!r}({c['class']})" for c in candidates[1:4]))
    return best["hwnd"]

def get_game_window_client_origin(window_title):
    '''
    返回游戏窗口「客户区」左上角在屏幕上的绝对坐标 (x, y)；找不到窗口返回 None。

    为什么用客户区而不是窗口外框：ui_coords 里的坐标是相对游戏画面（即客户区）的，
    而 GetWindowRect 取到的是含标题栏与边框的外框，直接相加会让点击整体上移
    一个标题栏的高度。
    '''
    hwnd = find_game_window_hwnd(window_title)
    if not hwnd:
        return None
    return win32gui.ClientToScreen(hwnd, (0, 0))

def get_window_client_size(window_title):
    '''
    返回游戏窗口「客户区」尺寸 (w, h)（物理像素，不含标题栏与边框）；找不到返回 None。

    用途：和抓到的帧尺寸交叉核对，第一时间发现
    「config 的 game_window.size 与真实窗口不一致」这种会让所有坐标失准的问题。
    '''
    hwnd = find_game_window_hwnd(window_title)
    if not hwnd:
        return None
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    return (right - left, bottom - top)


def get_window_metrics(window_title):
    '''
    一次性返回窗口的关键几何量，供诊断工具使用。找不到窗口返回 None。

    返回 dict：
      win_rect         外框 (l, t, r, b)（屏幕坐标，含标题栏与边框）
      win_size         外框尺寸 (w, h)
      client_size      客户区尺寸 (w, h)   ← 这才是「真实游戏画面」尺寸
      client_origin    客户区左上角在屏幕上的坐标 (x, y)
      title_bar_height 真实标题栏高度 = 客户区顶 - 外框顶
      border_left      左边框宽度 = 客户区左 - 外框左
      maximized        是否处于最大化状态
      title / class_   窗口标题 / 窗口类名
    '''
    hwnd = find_game_window_hwnd(window_title)
    if not hwnd:
        return None
    win_rect = win32gui.GetWindowRect(hwnd)
    cl = win32gui.GetClientRect(hwnd)
    origin = win32gui.ClientToScreen(hwnd, (0, 0))
    place = win32gui.GetWindowPlacement(hwnd)
    return {
        "hwnd": hwnd,
        "title": win32gui.GetWindowText(hwnd),
        "class_": win32gui.GetClassName(hwnd),
        "win_rect": win_rect,
        "win_size": (win_rect[2] - win_rect[0], win_rect[3] - win_rect[1]),
        "client_size": (cl[2] - cl[0], cl[3] - cl[1]),
        "client_origin": origin,
        "title_bar_height": origin[1] - win_rect[1],
        "border_left": origin[0] - win_rect[0],
        "maximized": place[1] == win32con.SW_SHOWMAXIMIZED,
    }


# 标题栏高度自适应的容差（像素）。见 resolve_title_bar_height()。
TITLE_BAR_TOLERANCE = 1

# 已打过「自适应接管」日志的 (配置值, 实测值) 组合。
# ⚠️ resolve_title_bar_height 在**每帧**都被调用（30fps），这条日志不去重就会刷爆日志
#    —— 与 issue #2 的「每帧一条 ERROR」是同类错误。见该函数内的说明。
_TITLE_BAR_LOGGED = set()


def resolve_title_bar_height(cfg, window_title=None, tag="标题栏"):
    r'''
    取「本次该切掉多少行标题栏」，配置值与**真机实测值**不符时按实测值走。

    ── 为什么需要它（issue #14）────────────────────────────────────────────
    配置里的 game_window.title_bar_height 是**开发机实测的常数**（31）。
    但标题栏高度是 **Windows 按 主题 + DPI + build 算出来的系统值**，每台机器都可能不同：
    实测某用户机器为 27，比配置少 4px。

    切多了会怎样：帧高 = 客户区高 + 标题栏 + 1，切掉 31 就只剩 765 行，
    不够 768 → crop_frame_to_client 判定"比目标小"并返回 None →
    引擎拿不到帧（表现为「画面大小对不上」，且录制/试读都看不到画面）。

    切多但没跨过阈值时更隐蔽：不报错，**所有坐标整体上移几像素** ——
    血蓝条 ROI 距底边本就只有约 4px，会连带读数不准。

    ── 取值规则（三道闸，确保不改变正常用户的行为）────────────────────────
      1. 量不到窗口（最小化/句柄失效/异常）→ 一律返回配置值，维持现状；
      2. 实测值与配置值相差 ≤ TITLE_BAR_TOLERANCE(1px) → 返回配置值，行为零变化；
      3. 只有相差 > 1px 时才用实测值接管，并打日志留证。

    ── 口径说明（为什么不用 get_window_metrics）──────────────────────────
    实测标题栏高度 = 客户区顶 - 外框顶（get_window_metrics 的 title_bar_height，
    common.py 中 win32gui.ClientToScreen / GetWindowRect 那条路径）。
    该路径依赖窗口未被最小化，否则 win_rect 会返回 (-32000, -32000) 之类的哨兵值
    → 量出来的"标题栏"高达上万像素。因此这里**额外校验**：实测值必须落在
    (0, 200) 的合理区间内，否则视为量测失败，退回配置值。

    参数
      cfg            完整配置（读 cfg["game_window"]["title_bar_height"] 当基准）
      window_title   游戏窗口标题关键词；不给就用 cfg["game_window"]["title"]
      tag            日志前缀

    返回 int：本次应切掉的行数（配置值 或 实测值）
    '''
    try:
        gw = (cfg or {}).get("game_window", {}) or {}
        configured = int(gw.get("title_bar_height", 31))
    except Exception:                                          # noqa: BLE001
        return 31

    title = window_title or gw.get("title")
    if not title:
        return configured

    measured = None
    try:
        m = get_window_metrics(title)
        if m:
            measured = int(m.get("title_bar_height"))
    except Exception as e:                                     # noqa: BLE001
        logger.debug(f"[{tag}] 量测失败，沿用配置值 {configured}：{e}")
        return configured

    # 闸 1：量不到 / 量出离谱的值（窗口最小化时外框是 -32000 哨兵）→ 维持现状
    if measured is None or not (0 < measured < 200):
        logger.debug(f"[{tag}] 实测值 {measured} 不合理（窗口未还原？），"
                     f"沿用配置值 {configured}。")
        return configured

    # 闸 2：差异在容差内 → 走配置值，正常用户行为零变化
    if abs(measured - configured) <= TITLE_BAR_TOLERANCE:
        return configured

    # 闸 3：确实不符 → 按实测值接管，留全证据（实测值/配置值/是否接管）
    #
    # ⚠️ 这条日志**必须只打一次**：resolve_title_bar_height 是在**每帧**的裁剪路径上
    #    被调用的（30fps），无条件打会瞬间刷爆日志 —— 与 issue #2 踩过的
    #    「每帧一条 ERROR」完全同类。用模块级集合按 (配置值, 实测值) 去重：
    #    值没变就不再打；真变了（换窗口/改配置/系统缩放变了）会重新打一次。
    _key = (configured, measured)
    if _key not in _TITLE_BAR_LOGGED:
        _TITLE_BAR_LOGGED.add(_key)
        logger.info(f"[{tag}] 【自适应】配置 title_bar_height={configured} 与实测 "
                    f"{measured} 相差 {abs(measured - configured)}px（>容差 "
                    f"{TITLE_BAR_TOLERANCE}px）—— 本次按**实测值 {measured}** 裁剪。\n"
                    f"        原因：标题栏高度是系统按主题/DPI/build 算的，每台机器可能不同；\n"
                    f"        配置值只是开发机实测的常数，本机对不上就会切多或切少。\n"
                    f"        想永久修好：运行 `python -m tools.measure_window --write-config`，\n"
                    f"        把本机实测值写进配置（之后这条日志就不再出现）。")
    return measured


def crop_frame_to_client(frame, target_size, title_bar_height, tag="帧"):
    r'''
    把「抓窗口」拿到的原始帧裁成**游戏客户区**，使帧的像素坐标 == 客户区坐标。

    这是全部坐标（模板、ui_coords、路线图、点击）能对上的唯一前提，
    所以引擎和路线录制器共用这一份实现，不各写一套。

    ── 抓帧口径（2026-09-10 实测确认，见 tools/measure_window.py）────────────
    `windows_capture` 抓**窗口**时，帧里含标题栏和 1px 窗口边框，不是纯客户区：
      帧宽 = 客户区宽 + 2        （左右各 1px）
      帧高 = 客户区高 + 标题栏高 + 1（底部 1px）
    实测对照：800x600 客户区的窗口 → 帧 802x632，标题栏 31px，内容从 (1, 31) 开始。

    ── 裁剪规则 ──────────────────────────────────────────────────────────
      1. 切掉顶部 title_bar_height 行 → 内容左上角 == 客户区左上角
      2. 水平按「左右边框对称」取左偏移 (帧宽 - 目标宽) // 2，锚定左上角裁到目标尺寸

    为什么锚左上角而不是原来的「居中裁」：居中裁在多余像素不对称时会整体偏移
    （底部恰好多 1px 时 y 偏移为 0 是巧合，多 2px 就偏 1px）。

    参数
      frame             原始帧 (h, w, c)
      target_size       config 的 game_window.size，语义是 [高, 宽]
      title_bar_height  帧顶部要切掉的行数
      tag               日志前缀，便于区分是哪个模块在裁

    返回 (裁剪后的帧, 说明)；尺寸不足返回 (None, 原因说明)
    '''
    target_h, target_w = target_size
    raw_h, raw_w = frame.shape[:2]

    if title_bar_height > 0:
        if raw_h <= title_bar_height:
            return None, (f"帧高 {raw_h} 不足以切掉标题栏 {title_bar_height} 行"
                          f"（窗口没开？或 title_bar_height 填错？）")
        cut = frame[title_bar_height:, :]
    else:
        cut = frame

    h, w = cut.shape[:2]
    x0 = max(0, (w - target_w) // 2)   # 左右边框对称 → 左边框宽度

    if w >= target_w and h >= target_h:
        out = cut[0:target_h, x0:x0 + target_w]
        msg = (f"原始帧 {raw_w}x{raw_h} - 标题栏 {title_bar_height} → {w}x{h}"
               f" → 裁到 {target_w}x{target_h}（丢弃左边框 {x0}px、"
               f"右边框 {w - x0 - target_w}px、底部 {h - target_h}px）")
        return out, msg

    # 帧比目标小：不裁剪（贴黑边只会把坐标带得更偏），把问题暴露给调用方
    #
    # ★ 2026-09-29（issue #14）：这条文案原来只说"窗口是不是被改小了"，
    #   把矛头**全指向用户**，但实测最常见的真因是**配置切多了** ——
    #   title_bar_height 是系统值（受主题/DPI/build 影响），配置里的 31 只是
    #   开发机实测常数；本机真实值更小时就会切多，剩下不够目标高。
    #   两种成因的**症状完全一样**（都表现为"抓到的画面比配置小"），
    #   所以这里把两种可能都列出来，并给出各自的分辨办法。
    short = target_h - h
    return None, (
        f"抓到的画面 {w}x{h} 比配置的 {target_w}x{target_h} 小 {short}px"
        f"（标题栏已切 {title_bar_height} 行）。\n"
        f"        两种可能，按顺序查：\n"
        f"          ① **配置切多了**（更常见）：本机真实标题栏比配置的 "
        f"{title_bar_height}px 小，导致切掉了本该保留的画面行。\n"
        f"             分辨办法：运行 `python -m tools.measure_window`，\n"
        f"             看它量出的「标题栏高度」是否为 {title_bar_height}；\n"
        f"             不是就运行 `python -m tools.measure_window --write-config` 改写配置。\n"
        f"             程序已内置自适应：相差 >1px 时会自动按实测值裁剪。\n"
        f"          ② **游戏窗口确实变小了**：分辨率没设成 "
        f"{target_w}x{target_h}、开了全屏/最大化、或还开着旧窗口。")


def click_in_game_window(window_title, coord, button="left", clicks=1):
    '''
    Mouse click on a game window coordinate

    - coord 相对游戏窗口客户区左上角
    - 鼠标事件经 Interception 内核驱动发出，与键盘输入链路保持一致
      （不再使用用户态 pyautogui，避免注入痕迹与键盘链路不一致）
    '''
    origin = get_game_window_client_origin(window_title)
    if origin is None:
        text = f"Cannot find window: {window_title}"
        logger.error(text)
        raise RuntimeError(text)

    loc_click = (origin[0] + int(coord[0]), origin[1] + int(coord[1]))
    ok = click_at(loc_click[0], loc_click[1], button=button, clicks=clicks)
    if ok:
        logger.info(f"[click_in_game_window] 客户区 {tuple(coord)} -> 屏幕 {loc_click} "
                    f"(按钮={button}, 次数={clicks})")
    else:
        logger.error(f"[click_in_game_window] 点击未执行（光标未到位）: "
                     f"客户区 {tuple(coord)} -> 屏幕 {loc_click}")
    return ok

def route_size_mismatch(size, ref_size) -> bool:
    """
    这张路线图（或回正线图）与**参考图**（minimaps/<地图>/map.png）尺寸是否不一致。

    ⚠️ 2026-10-14 真机事故后加的（手绘回正线面板打不开）：
        IndexError: boolean index did not match indexed array along axis 1;
        size of axis is 344 but size of corresponding boolean axis is 268
    真因：minimaps/废弃地铁2 里混着**两次不同小地图尺寸**录出来的文件 ——
    map.png 137x344（当次）、route3..route7 也是 137x344，
    但 route1 137x268、route2 137x319 是另一次录制的遗留。

    为什么尺寸不同就**不能用**：map.png 是整张小地图全景，录制器拿它当坐标系
    （`get_player_location_on_global_map()` = `find_pattern_sqdiff(self.img_map,
    self.img_minimap)` + 小地图上的角色点 + offset），路线图上的像素就是这套坐标。
    尺寸不一样 ⇒ **刻度不一样**，不是"图糊了"：拿它当主线参考（手绘面板）会错位，
    让引擎按它走会**走到错的地方**，而且都不报错。所以判据只有一条：尺寸必须一致；
    不一致的**跳过 + 明确报出来**（fail-closed，见引擎 `load_config` 与
    `tools/homeRouteDrawer.py` 的两处调用点）。

    只比 shape[:2]（高、宽），不比对内容 —— 内容有差异是正常的（不同次的录制）。

    Args:
        size: 待检查图的 shape（或 (h, w) 元组）
        ref_size: 参考图的 shape（或 (h, w) 元组）

    Returns:
        bool: True = 尺寸不一致（这张图不能用）
    """
    return tuple(size[:2]) != tuple(ref_size[:2])


def mask_route_colors(img_map, img_route, color_code):
    """
    Masks all pixels in img_route where img_map contains any route color.
    Pixels at those positions in img_route are set to black (0,0,0).
    """
    # Parse color_code keys to list of RGB tuples
    target_colors = [tuple(map(int, color_str.split(','))) for color_str in color_code.keys()]

    # Ensure dimensions match
    if img_map.shape[:2] != img_route.shape[:2]:
        logger.warning("[mask_route_colors] Resizing img_map from "
                       f"{img_map.shape} to {img_route.shape}")
        img_map = cv2.resize(img_map, (img_route.shape[1], img_route.shape[0]))

    # Build mask for each color
    mask = np.zeros(img_map.shape[:2], dtype=bool)
    for color in target_colors:
        matches = np.all(img_map == color, axis=-1)
        mask |= matches

    # Apply mask to img_route (set those pixels to black)
    img_route[mask] = (0, 0, 0)

    return img_route

def _wait_foreground(hwnd, timeout=0.35):
    '''
    等前台窗口变成 hwnd（轮询）。返回 True/False。

    ⚠️ 为什么不能只查一次（2026-09-12 实测）：窗口切换的**瞬间**
    GetForegroundWindow() 会短暂返回 0 —— 实测采样序列是
        [+0.00s] hwnd=0（无前台）  →  [+0.02s] hwnd=游戏窗口
    所以切换后立刻查一次，会把「其实已经切成功」误判成「切失败」，
    日志里刷出假的「✗ 无法把游戏窗口切到前台」告警，把人往错误方向带。
    '''
    t0 = time.time()
    while time.time() - t0 < timeout:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return win32gui.GetForegroundWindow() == hwnd

def activate_game_window(window_title):
    '''
    activate_game_window
    This function only support Windows OS

    返回：
        True  —— 调用返回时游戏窗口**确实**在前台（已用 GetForegroundWindow 核对）
        False —— 试过常规与强制两条路都没能切过去（调用方应据此提示用户）

    【为什么必须核对返回值、还要有强制路径】（2026-09-12 实测踩坑）
      Windows 只允许「当前前台进程」或「刚收到用户输入事件的进程」调
      SetForegroundWindow；不满足时它**静默失败**（返回 False，不抛异常）。
      本项目场景正好踩中：用户在**界面**上点「开始」→ 界面是前台进程 →
      这时想把前台交给游戏，常规调用会被拒绝。原实现只看有没有抛异常，
      于是"切前台失败"和"切成功"都会打印同一句 `Set game window to foreground`
      —— 用户以为已经切过去了，实际游戏一直没拿到前台，**按键全打在界面上**，
      症状就是「点开始角色不动」，且日志里一条异常都没有。
      强制路径：AttachThreadInput 把当前前台线程的输入队列挂到本线程上，
      绕开上面那条限制（业界标准做法）。
    '''
    hwnd = find_game_window_hwnd(window_title)
    if hwnd == 0:
        raise Exception(f"Cannot find window with title: {window_title}")

    if win32gui.GetForegroundWindow() == hwnd:
        return True          # 已经在前台，不用折腾

    # ---- 常规路径 ----
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetActiveWindow(hwnd)
        except Exception:
            pass

    if _wait_foreground(hwnd):
        logger.info("[activate_game_window] Set game window to foreground")
        return True

    # ---- 强制路径 ----
    # ⚠️ 三个防御点（2026-09-12 实测：无前台权限的进程上跑会踩到）：
    #   ① GetForegroundWindow() 可能返回 0（前台正在切换 / 前台在别的桌面）→
    #      此时 GetWindowThreadProcessId(0) 得到 tid=0，AttachThreadInput(0, ...)
    #      会抛 "(87, 'AttachThreadInput', '参数错误')"。必须判空再挂。
    #   ② AttachThreadInput 失败**不能**中断整个流程 —— 后面的 BringWindowToTop
    #      本身在部分场景就能成，挂不上就跳过。
    #   ③ 挂上了必须在 finally 里摘掉，否则本线程的输入队列会一直粘着别人的线程。
    fg = win32gui.GetForegroundWindow()
    tid_fg = tid_me = None
    attached = False
    try:
        tid_fg = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        tid_me = win32api.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            win32process.AttachThreadInput(tid_fg, tid_me, True)
            attached = True
    except Exception as e:
        logger.debug(f"[activate_game_window] AttachThreadInput 未成功（不影响后续尝试）: {e}")
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetActiveWindow(hwnd)
        # SwitchToThisWindow 是 user32 的非文档化函数，在前两条都被系统拒绝时
        # 往往还能成（实测 2026-09-12）。失败无所谓，不要中断流程。
        try:
            win32gui.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if attached:
            try:
                win32process.AttachThreadInput(tid_fg, tid_me, False)
            except Exception:
                pass

    if _wait_foreground(hwnd):
        logger.info("[activate_game_window] Set game window to foreground"
                    "（常规调用被系统拒绝，已用 AttachThreadInput 强制切换）")
        return True

    # 最后再宽容等一次（部分机器上前台移交有明显延迟）
    if _wait_foreground(hwnd, 0.6):
        logger.info("[activate_game_window] Set game window to foreground"
                    "（延迟生效）")
        return True

    logger.warning(
        "[activate_game_window] ✗ 无法把游戏窗口切到前台，当前前台是 "
        f"{win32gui.GetWindowText(win32gui.GetForegroundWindow())!r}。"
        "按键会全部落在这个窗口上，游戏收不到 —— 请手动点一下游戏窗口。")
    return False

def get_game_window_title_by_token(token):
    '''
    按关键词找游戏窗口标题（供 windows_capture 用）。找不到返回 None。

    口径与 find_game_window_hwnd 一致：排除系统外壳窗口，多候选取面积最大者。
    '''
    candidates = list_windows_like_game(token)
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning("[get_game_window_title_by_token] 命中多个候选，取最大的："
                       + "；".join(f"{c['title']!r}({c['class']})" for c in candidates[:4]))
    return candidates[0]["title"]

def normalize_pixel_coordinate(coord, base_size, target_size=None):
    r'''
    把「按 base_size 录制的坐标」换算到「target_size 的画面」上。

    参数
      coord        待换算坐标 (x, y)，含义是「在 base_size 画面上的像素位置」
      base_size    录制这段坐标时画面的尺寸 (h, w) —— 即 config 的 game_window.coord_base_size
      target_size  当前画面的尺寸 (h, w)；不给就取 base_size（即不换算，恒等）

    返回换算后的 (x, y)，与配置同一坐标系。

    ## 为什么改成这样（2026-09-10）

    原实现把坐标归一到写死的「标准尺寸 693x1282」，而画面在
    MapleStoryAutoLevelUp.get_img_frame() 里被裁成 config 的 game_window.size
    （上游默认 1366x768）—— 两个尺寸宽高比都不同（1.779 vs 1.850），
    调用方又直接用换算后的坐标去切实际画面 → 坐标整体偏移。

    现在统一为一条规则：

      **画面帧的像素坐标系 == 录制坐标时用的坐标系 == config 的 game_window.size**

    即：`coord_base_size` 默认就等于 `game_window.size`（配置里用 `~` 引用），
    此时本函数是恒等变换。只有当「历史 ui_coords 是在另一个尺寸下录的」
    才需要把 coord_base_size 单独填成那个尺寸，由本函数负责换算，
    换算方向是 base → target（原实现的方向是反的，这也是坐标错的根因之一）。
    '''
    h_base, w_base = base_size
    if target_size is None:
        target_size = base_size
    h_tgt, w_tgt = target_size

    if (h_base, w_base) == (h_tgt, w_tgt):
        return coord

    if h_base <= 0 or w_base <= 0:
        logger.warning(f"[normalize_pixel_coordinate] base_size 非法 {base_size}，原样返回")
        return coord

    scale_y = h_tgt / h_base
    scale_x = w_tgt / w_base

    x, y = coord
    new_x = round(x * scale_x)
    new_y = round(y * scale_y)

    logger.info("[normalize_pixel_coordinate] "
                f"coord{tuple(coord)} 由 {w_base}x{h_base} 换算到 {w_tgt}x{h_tgt} "
                f"→ {(new_x, new_y)}")

    return (new_x, new_y)


def edge_guard_step_cap(step, span) -> int:
    '''围栏"往前看多少像素"的**上限**（纯函数 → 可离线单测）。

    治的是什么（2026-09-14 真机实测）：
        平台只有 **10px** 宽（94~104），而围栏的补偿量算出来是 **6px**
        （单帧位移 2px × 反应延迟 3 帧）。判定式是
            can_left  = (px - step) > xmin
            can_right = (px + step) < xmax
        于是角色站到中间 x=99 时：往左会到 93（出界）、往右会到 105（出界）
        → **两边都不许走** → 被按死在原地 → 卡住 10s → 触发脱困随机指令
        （`right down jump`）→ 一脚把角色踹下平台。

        ⚠️ 判据：**step >= 半宽 就必然出现死区**，所以上限必须 **< 半宽**。
        另外它顺带压住了异常值：被怪撞飞那一下实测量到 14px，
        ×3 之后是 42px —— 比整个平台宽 4 倍，接下来几帧会彻底锁死。

    副作用（要知情）：窄平台的"防冲出"余量会变小。但过度保护会把角色按死，
    实测下来比偶尔冲出去更糟（按死 → 卡住 → 随机脱困 → 掉坑）。

    Args:
        step: 原本算出的补偿量（会被夹到 >= 1）
        span: 平台可用宽度 `xmax - xmin`（像素）

    Returns:
        int: 封顶后的补偿量，恒 >= 1。
             span <= 2（窄到站不住的台子）→ 不封顶，原样返回（交给调用方处理）。
    '''
    try:
        s = int(step)
    except (TypeError, ValueError):
        return 1
    s = max(1, s)
    try:
        w = int(span)
    except (TypeError, ValueError):
        return s
    if w <= 2:
        return s
    return min(s, max(1, w // 2 - 1))


def route_too_narrow_for_patrol(goal_gap, min_gap) -> bool:
    '''这条路"窄到不值得来回巡逻"吗？（纯函数 → 可离线单测）

    治的是什么（2026-09-15 真机实测）：
        本图巡逻线只有 **10px** 长（全局 x 94~104），两段终点只差 **4px**。
        切段判定的容差 `reach` 被 `(gap-2)//2` 压到 1（见 _near_seg_goal），
        于是段0 在 `x >= 100` 触发、段1 在 `x <= 98` 触发
        → **角色只能在 98~100 之间来回蹭 2px**，实际等于没巡逻。
        代码里本来就把这个当"已知代价"接受了（注释原话：「宁可蹭 3px」），
        但它每 10 秒会触发一次看门狗的"卡死" → 随机脱困指令 → 把角色踹到别处
        （实测：随机到 jump，角色直接跳到上一层平台去了）。

    ⇒ 与其让它在 2px 里抖（从行为特征看比站着不动**更像脚本**），
       不如**干脆不巡逻**：站着面向怪、正常出招即可。这就是"窄平台降级"。

    Args:
        goal_gap: 相邻两段终点的间距（像素）。单段路线 / 取不到 → 0。
        min_gap:  阈值，小于它就算"窄"。

    Returns:
        bool: True = 应该降级（不来回蹭、不判卡死）。

    ⚠️ 单段路线（`goal_gap <= 0`）**永远不降级** —— 它靠 `_apply_pingpong`
       走完全程，本来就不存在"蹭"的问题。
    ⚠️ 阈值非法 / <= 0 → 一律 False（**不降级**，最保守：宁可保持老行为，
       也不要因为配置写错就让全世界的图都不巡逻了）。
    '''
    try:
        g = float(goal_gap)
        m = float(min_gap)
    except (TypeError, ValueError):
        return False
    if g <= 0 or m <= 0:
        return False
    return g < m


def nametag_relax_decision(score, thres, ratio, max_jump, cand, anchor):
    '''名字定位「抗闪」判据（2026-10-14 加）—— **只有这一处**，引擎与自检共用。

    病根（用户真机日志 2026-10-14 11:42，`log/MSBot_2026-10-14_11-42-16.log`）：
    名字匹配的分数是**双峰**的 ——
      · 认得出的帧 0.0000~0.0057（配置里 2026-09-10 的标定值；本次真机"近段最低 0.0009"）
      · 认不出的帧 0.0220~0.0240，正好卡在 `diff_thres: 0.01` **之上**
      · 而配置注释里记录的"完全失配 / 已知假阳性"是 0.033~0.047
    ⇒ 0.022~0.024 这一档既不是"完全没匹配上"，也不够干净，多半是名牌被挡了一部分
    （切块兜底 `split_width_fallback: 30` 也没救回来）。

    真正的危害不是"位置错"，而是 `nametag_miss_streak` 一路涨过
    `stale_pos_full_frame_after`（当时是 10；2026-10-14 当天先后试过 25 与 10，
    最终按用户拍板设为 **0 = 关掉整帧放大**）
    ⇒ **索敌框放大到整帧** ⇒ 怪数虚高（真机 怪8~怪10，
    正常 怪0~怪2）、去追画面外 550px 的目标（感知框 ±330 根本看不到）。

    判据（**两条同时满足**才采信这一帧）：
      ① 分数 < `thres * ratio`（`ratio <= 1` 直接关掉本功能）；
      ② 候选角色位置离**上一次严格命中**的锚点曼哈顿距离 <= `max_jump`。
    ②是安全阀，理由沿用配置注释里的口径："认错 = 位置错（危害大），不认 = 沿用上一帧
    位置（几乎无害）" ⇒ 放宽必须**有界**：最坏后果只是"锚点附近 max_jump 像素内的偏移"，
    而不是"框到别处去"（那才会让索敌框/攻击框整体错位）。
    ⚠️ 锚点必须始终是**严格命中**的位置，**不能**用上次放宽采信的位置 ——
       否则每帧都能挪 max_jump，连成一串就把位置链式带跑了。

    Args:
        score: 本次匹配分（SQDIFF_NORMED，0=完全一样，越小越像）
        thres: `nametag.diff_thres`（严格阈值）
        ratio: `nametag.relax_ratio`（<=1 表示关闭本功能）
        max_jump: `nametag.relax_max_jump`（像素）
        cand: 本次算出的**角色位置** (x, y)（已过画面外假命中护栏）
        anchor: 上次**严格命中**的角色位置 (x, y)；没有就传 None

    Returns:
        (bool, str, int | None): (是否采信, 人话原因, 与锚点的距离或 None)
    '''
    try:
        _score = float(score)
        _thres = float(thres)
        _ratio = float(ratio)
    except (TypeError, ValueError):
        return False, "分数/阈值/放宽倍数不可用", None
    if _ratio <= 1.0:
        return False, "放宽已关闭（relax_ratio <= 1）", None
    _cap = _thres * _ratio
    if not _score < _cap:
        return False, f"分数 {_score:.4f} 连放宽上限 {_cap:.4f} 都没过", None
    if cand is None or anchor is None:
        return False, "还没有过严格命中，没有可比对的锚点", None
    try:
        _d = abs(int(cand[0]) - int(anchor[0])) + abs(int(cand[1]) - int(anchor[1]))
        _jump = int(max_jump)
    except (TypeError, ValueError, IndexError):
        return False, "位置/距离不可用", None
    if _d > _jump:
        return False, (f"分数 {_score:.4f} 在放宽上限 {_cap:.4f} 内，"
                       f"但位置离上次严格命中 {_d}px > {_jump}px"), _d
    return True, (f"分数 {_score:.4f} 超过严格阈值 {_thres:g}"
                  f"（放宽 ×{_ratio:g}，上限 {_cap:.4f}），"
                  f"但位置离上次严格命中只差 {_d}px（≤{_jump}）"), _d


def mob_no_attack_reason(kind, engage_n=0, far_n=0, half_x=0, half_y=0,
                         approach=True, stop_to_attack=True, front=None,
                         cooldown_left=0.0,
                         gap_area=None, gap_need=None, gap_dx=0, gap_dy=0,
                         center_in=0):
    '''「画面里有怪、这一帧却没出招」的**归因文案**（2026-10-14 加；纯函数 → 可离线单测）。

    治的是什么（用户真机反馈 m05740「有检测框、也在攻击范围内，但是没有立马攻击」）：
        画出来的框是**索敌框**（`get_search_box()` = 攻击框 + `search_box_margin`
        之后再乘 `detect_range_scale`：默认 2.0、用户侧 1.5 ⇒ 横向 ±330px / 纵向
        ±132px；位置连续丢失 `stale_pos_full_frame_after` 帧时直接放大到**整帧**），
        而出招只看**攻击框**（`get_attack_range()` = `aoe_skill.range_x/y ÷ 2` ⇒
        ±170×±70），并且要求与怪框的交叠面积 `>= _min_mob_area()`。
        两个框差着一大截 ⇒ **「框画出来了」只证明"看见了"，不证明"够得着"** ——
        真机日志 `[靠近] …最近的在left侧 176px` 正是这一类（机器人在**走过去**打，
        不是站着打）。
    为什么需要这一行：散在 6 个分支里的 `return` 各自都合理，但用户从日志里读不出
        "这一帧为什么没出招"。只有把**本帧的事实**（框内几个 / 框外几个 / 两个开关 /
        前台 / 冷却）汇总成一行，才能一眼分清「引擎认为够不着」「在爬绳/让路」
        「窗口没在前台」「冷却没到」这四种完全不同的处境。

    ⚠️ 纯观测：本函数的返回值**只进日志**，不参与任何判定（改它不会改行为）。
    ⚠️ 三态：`front=None` = **查不到**（键盘控制器还没起 / 查询失败），必须与
       `front=False`（确认不在前台）分开说 —— 别把"不知道"写成"正常"
       （`03 §6` 教训 3：区分"没发生"和"没观测"）。

    Args:
        kind (str): 'front' | 'no_engage' | 'approach_pending' | 'approach_off'
            | 'cooldown' | 'direction'。未知取值 ⇒ 归一成 'unknown'（**不抛异常**：
            日志文案不该把主循环带崩）。
        engage_n (int): 攻击框内的目标数（"够得着"的那批）。
        far_n (int): 索敌框内、攻击框外的目标数（"看得见够不着"的那批）。
        half_x, half_y (int): 攻击框半宽/半高（像素），只进文案。
        approach (bool): `monster_detect.approach_mob`（要不要"走过去打"）。
        stop_to_attack (bool): `monster_detect.stop_to_attack`（要不要"停下来打"）。
        front (bool | None): 游戏窗口是否在前台；None = 未知。
        cooldown_left (float): 攻击冷却剩余秒数（kind='cooldown' 时用）。
        gap_area (int | None): 离"算够得着"**最近的那一只**与攻击框的交叠面积（px²）。
            None = 这一帧没算（就不打这段）。★ 2026-10-14 加：用户 m06668
            「红色攻击框里有绿色的检测框，但是就是不攻击」—— 只说"框外有几个"
            分不清「差 1px」（判据太严，该放宽）和「差 300px」（画出来的是索敌框，
            本来就不该打）。给出数字才分得清。
        gap_need (int | None): 那只怪"算够得着"需要的交叠面积（= `_min_mob_area()`，
            本图**最小**那张模板的面积）—— 对小怪意味着"必须几乎完全落在攻击框里"。
        gap_dx, gap_dy (int): 那只怪的中心离攻击框的水平/垂直距离（已在框内 = 0）。
        center_in (int): 怪框**中心**已经落在攻击框内的目标数（判据与直觉是否一致的
            唯一证据：中心都在框里却仍不算够得着 ⇒ 判据偏严）。

    Returns:
        (str, str): (归一化后的 code, 整行正文)。正文**不含** `[没出招·归因] ` 前缀。
    '''
    _k = str(kind)
    if _k not in ("front", "no_engage", "approach_pending", "approach_off",
                  "cooldown", "direction", "home_route"):
        _k = "unknown"

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    _eng, _far = _i(engage_n), _i(far_n)
    _tot = _eng + _far
    _hx, _hy = _i(half_x), _i(half_y)
    _sw = (f"stop_to_attack={'开' if stop_to_attack else '关'}"
           f" / approach_mob={'开' if approach else '关'}")
    _fr = ("前台=**未知**" if front is None
           else ("前台=在前台" if front else "前台=**不在前台**"))
    _facts = f"（攻击框 ±{_hx}×±{_hy}px，画面 {_tot} 个目标；{_sw}；{_fr}）"

    if _k == "front":
        _body = ("游戏窗口不在前台 ⇒ 引擎照旧检测、照旧画框，但键盘层**一个键都不发**"
                 "（把游戏窗口点到最前；本项目界面自己在前台时也算「不在前台」，"
                 "因为它标题里含游戏标题）")
    elif _k == "no_engage":
        _tail = ("；正在走过去打，还没进框" if approach
                 else "；approach_mob 关着 ⇒ 框外的一律不管")
        _body = (f"攻击框里一个都没有：{_far} 个目标全在**攻击框外**"
                 f"（画出来的框是索敌框，比攻击框大一圈）{_tail}")
    elif _k == "approach_pending":
        _body = (f"攻击框外有 {_far} 个目标，但本帧没追过去"
                 f"（还没凑齐「连续两次都看见」的确认，或它们都在别的层 —— "
                 f"|高差| > approach_dy_tol）⇒ 本帧不出招")
    elif _k == "approach_off":
        _body = (f"attack 框外有 {_far} 个目标，但 approach_mob 关着 ⇒ "
                 f"只打框内的，框外的一律不管（这不是故障，是配置）")
    elif _k == "cooldown":
        if _eng > 0:
            _body = (f"攻击框里有 {_eng} 个目标，但攻击冷却还剩 {float(cooldown_left):.2f}s "
                     f"⇒ 冷却一到就出招")
        else:
            _body = (f"锁定中的目标暂时不在攻击框里（正在锁定补招），"
                     f"攻击冷却还剩 {float(cooldown_left):.2f}s")
    elif _k == "home_route":
        _body = ("正在走回正线（从平台掉下去之后先回到主线上）⇒ 回正期间怪逻辑"
                 "**完全不碰**走位与动作，看见怪也不打 —— 回到主线后立刻恢复")
    elif _k == "direction":
        _body = (f"攻击框里有 {_eng} 个目标，但 directional 判不出攻击方向"
                 f"（怪在正上/正下，或左右两侧一样近）⇒ 本帧不出招")
    else:
        _body = (f"原因未知（kind={kind!r}）：这一帧没出招，但没匹配到已知归因 —— "
                 f"请把本行连同前后的日志一起发给开发者")
    # ── 离「算够得着」最近的那一只差多少（2026-10-14 加，用户 m06668）────────
    #    为什么要数字：归因只说"框外有几个"，而"够不着"有三种完全不同的处境 ——
    #      ① 差一点点 ⇒ 交叠判据太严（最小模板的**整个**面积才算 ⇒ 体型最小的怪
    #         必须完全落在攻击框里），该放宽；
    #      ② **中心已经在攻击框里**却仍不算 ⇒ 判据与人的直觉不一致（最该看的一档）；
    #      ③ 差得远 ⇒ 画出来的是索敌框，本来就不该打（预期行为）。
    #    ⚠️ 纯观测：算不出来/参数非法 ⇒ 一个字都不加，绝不影响正文。
    _gap = ""
    try:
        if gap_need:
            _ga, _gn = _i(gap_area), _i(gap_need)
            _gdx, _gdy, _gci = _i(gap_dx), _i(gap_dy), _i(center_in)
            _dxy = (f"，中心离攻击框 {_gdx}px 水平 / {_gdy}px 垂直"
                    if (_gdx or _gdy) else "，中心已在攻击框内")
            _gap = (f"；离「算够得着」最近的一只：交叠 {_ga}/{_gn} px²"
                    f"（还差 {max(0, _gn - _ga)}{_dxy}；"
                    f"中心已在攻击框内的有 {_gci} 个）")
    except (TypeError, ValueError):
        _gap = ""
    return _k, _body + _gap + _facts


def monster_box_detail(pos, size, box):
    '''一只怪相对攻击框的一句话明细（纯函数 → 可离线单测）。

    格式：`(cx,cy) WxH 交叠A 中心∈框` / `… 中心外 40px×0px↓`
    （中心在框内时不写距离与箭头；`A` = 与该框的交叠面积 px²，
    门槛由整行的表头统一给出，不逐只重复。）
    算不出来 ⇒ 返回 `(?)`，**绝不抛异常**（这是观测行，不能带崩主循环）。
    '''
    try:
        _x, _y = int(pos[0]), int(pos[1])
        _w, _h = int(size[0]), int(size[1])
        _cx, _cy = _x + _w // 2, _y + _h // 2
        _bx0, _by0, _bx1, _by1 = (int(box[0]), int(box[1]),
                                  int(box[2]), int(box[3]))
        _iw = max(0, min(_bx1, _x + _w) - max(_bx0, _x))
        _ih = max(0, min(_by1, _y + _h) - max(_by0, _y))
        _dx = int(max(_bx0 - _cx, 0, _cx - _bx1))
        _dy = int(max(_by0 - _cy, 0, _cy - _by1))
        if _dx == 0 and _dy == 0:
            _where = "中心∈框"
        else:
            _arrow = ""
            if _dy:
                _arrow += "↑" if _cy < _by0 else "↓"
            if _dx:
                _arrow += "←" if _cx < _bx0 else "→"
            _where = f"中心外 {_dx}px×{_dy}px{_arrow}"
        return f"({_cx},{_cy}) {_w}×{_h} 交叠{_iw * _ih} {_where}"
    except (TypeError, ValueError, IndexError):
        return "(?)"


def monster_detect_snapshot(engage, far, box=None, mode="center", need=0,
                            detail_n=4, player=None, margin=0):
    '''把**这一帧的怪检测结果**压成一行（纯函数 → 可离线单测）。

    治的是什么（用户 m06852「记录下怪物的检测信息，现在的攻击还是不够灵敏」）：
        `[运行状态]` 只写「怪N」，看不出**它在哪、为什么不算够得着**；
        `[没出招·归因]` 只在"没出招"的帧打，出招的帧什么都看不到。
        于是"够不够灵敏"没法证 —— 这一行补上：攻击框的**绝对坐标**、
        引擎认为的**角色位置**、以及每只怪的中心/尺寸/交叠/中心离框多远。

    Args:
        engage (list[dict]): **攻击框内**的怪（调用方用 `_mobs_in_attack_box()`
            分好类 —— 本函数**不重新判定**，避免出现第二套判据）。
        far (list[dict]): 检出了、但不在攻击框内的怪。
        box (tuple | None): 攻击框 (x0, y0, x1, y1)；None ⇒ 不写坐标段。
        mode (str): 当前判据名（`center` / `min_area`）。
        need (int): `_min_mob_area()`（"算够得着"需要的交叠面积）。
        detail_n (int): 明细最多列几只（先列框内的，再列框外交叠最大的）。
        player (tuple | None): 引擎认为的角色位置 (x, y)。
        margin (int): 判定用的「宽判定」余量（`monster_detect.mob_engage_margin`，
            px）。`box` 是**已经加过余量**的框；这里只把它写进文案，免得日志说
            "框在这儿"、读者却拿它与技能真实范围对不上号。0/畸形 ⇒ 不写这一段。

    Returns:
        str: 整行正文（**不含** `[怪检测] ` 前缀）。
    '''
    _eng = list(engage or [])
    _far = list(far or [])
    _box = None
    if box is not None:
        try:
            _box = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        except (TypeError, ValueError, IndexError):
            _box = None
    _head = (f"检出 {len(_eng) + len(_far)} 只 | 攻击框内 {len(_eng)} / 框外 "
             f"{len(_far)} | 判据 {mode}")
    if _box is not None:
        _head += (f"（门槛 {int(need)}px²）| 攻击框 x[{_box[0]},{_box[2]}] "
                  f"y[{_box[1]},{_box[3]}]"
                  f"（±{(_box[2] - _box[0]) // 2}×±{(_box[3] - _box[1]) // 2}）")
        try:
            _mg = int(margin or 0)
        except (TypeError, ValueError):
            _mg = 0
        if _mg:
            _head += (f"＋宽判定余量 {_mg}px（上面这个框已向外各扩 {_mg}px；"
                      f"屏幕上画的仍是技能真实框）")
    if player is not None:
        try:
            _head += f" | 角色({int(player[0])},{int(player[1])})"
        except (TypeError, ValueError, IndexError):
            pass
    # 明细：框内的在前（那是"正在打"的），框外按交叠从大到小（那是"最近够得着"的）
    _items = [(_m, True) for _m in _eng] + [(_m, False) for _m in _far]
    _parts = []
    for _n, (_m, _in) in enumerate(_items[:max(0, int(detail_n))]):
        try:
            _d = monster_box_detail(_m["position"], _m["size"], _box or (0, 0, 0, 0))
        except (TypeError, KeyError):
            _d = "(?)"
        _parts.append(f"{'★' if _in else '·'}{_n + 1}{_d}")
    _detail = ("；明细 " + " ".join(_parts)) if _parts else ""
    _more = ""
    if len(_items) > max(0, int(detail_n)):
        _more = f"（还有 {len(_items) - max(0, int(detail_n))} 只没列）"
    return _head + _detail + _more


def monster_detect_interval(cfg) -> int:
    '''怪检测**隔几帧**才真的跑一次（纯函数 → 可离线单测）。

    治的是什么（2026-09-14 真机实测）：
        一帧 552ms 里怪检测占 **502ms（91%）**，主循环因此只有 1.8fps
        （配置写的是 30）。怪其实不需要每帧重新找 —— 降频后省下的时间会让
        帧率涨回来，"两次检测之间"只慢 50~100ms，整体响应却快一倍。

    Args:
        cfg: 配置 dict。None / 不是 dict / 缺 monster_detect 段 / 缺键 /
             非数字 / 小于 1 → 一律返回 **1**（= 每帧都跑 = 老行为，最保守）。

    Returns:
        int: >= 1 的整数。1 = 关掉降频。
    '''
    try:
        n = int((cfg.get("monster_detect") or {}).get("every_n_frames", 1))
    except (AttributeError, TypeError, ValueError):
        return 1
    return n if n >= 1 else 1
