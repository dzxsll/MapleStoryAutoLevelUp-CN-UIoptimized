# -*- coding: utf-8 -*-
'''
cfg_names — 配置项的中文显示名

高级设置页的分组标题和字段标签原本直接用配置字段名（英文 key）显示，
这里做一层“key -> 中文名”的映射，只影响显示，不影响任何取值/写回逻辑。

设计原则：
1. 查不到就原样显示英文 key —— 以后新增配置项不会炸，也不会显示成 undefined。
2. 支持 "分组.字段" 的精确覆盖，同名 key 在不同分组可以显示各自的中文含义。
3. 删功能时直接删配置段即可；本表的条目也一并清理，免得留成"查不到对应配置"的死条目。

（2026-09-10 已随功能删除清理：符文、创角掷骰、邮件、Mac、巡逻模式、
  遇人换频道、定时换频道、瞬移赶路、性能分析、其他玩家检测）
'''

# 分组标题
SECTION_CN = {
    "buff_skill": "增益技能",
    "directional_attack": "单体攻击",
    "aoe_skill": "群体技能",
    "nametag": "角色名标签",
    "character": "角色尺寸",
    "monster_detect": "怪物检测",
    "ui_coords": "界面坐标",
    "route": "路线跟随",
    "watchdog": "看门狗（卡死自救）",
    "minimap": "小地图",
    "route_recoder": "路线录制器",
    "game_window": "游戏窗口",
    "system": "系统设置",
    "demo_record": "手动示范录制（你自己玩，录成样本）",
}

# 通用字段名（所有分组共用）
KEY_CN = {
    "enable": "启用",
    # ── 手动示范录制（2026-10-14 加）────────────────────────────────────
    "enabled": "启用录制（你自己玩，引擎只记不按）",
    "record_only": "只记录不操作（键盘静音，必须为 true 才是干净样本）",
    "out_dir": "落盘目录（JSONL，每帧一行）",
    "record_keys": "记录真人按键（含按下/抬起时间戳）",
    "every_n_frames": "每几帧记一条（1=每帧）",
    "max_minutes": "单文件最长分钟数（到点换文件）",
    "max_frames": "单文件最多帧数（0=只按分钟切）",
    "gzip": "压缩落盘（.jsonl.gz）",
    "mode": "模式",
    "cooldown": "冷却时间（秒）",
    "offset": "坐标偏移",
    "debug": "绘制调试框",
    "keys": "按键列表",
    "action_cooldown": "施放后间隔（秒）",
    "range_x": "水平范围 X",
    "range_y": "垂直范围 Y",
    "turn_frames": "转身保持帧数（出招前按住方向键几帧）",
    "color_code": "颜色编码（RGB）",
    "name": "名字",
    "diff_thres": "匹配阈值",
    "global_diff_thres": "全局重搜阈值",
    "split_width": "标签切分宽度（像素）",
    "relax_ratio": "抗闪：分数放宽倍数（1=关，半峰也采信以免丢位置）",
    "relax_max_jump": "抗闪：放宽候选离上次严格命中的最大像素",
    "width": "宽度",
    "height": "高度",
    "search_box_margin": "搜索框外扩像素",
    "contour_blur": "轮廓模糊核大小",
    "verify_color": "启用前景色校验（挡植被假框）",
    "verify_color_tol": "前景色校验：单像素色差上限",
    "verify_min_ratio": "前景色校验：达标像素占比下限",
    "stop_to_attack": "巡逻中停下来打完再走",
    "stop_attack_clear_frames": "判定「打完了」的空检测帧数",
    "stop_attack_timeout_s": "停下打怪最长秒数（超时强制恢复巡逻）",
    "target_hold_s": "目标锁定：连续几秒没再看见才算打完（0=关）",
    "stop_attack_clear_s": "判定「打完了」的空窗秒数（与帧数判据取交集）",
    "approach_confirm_s": "追怪：连续多久内两次看见才算真目标",
    "approach_dir_min_s": "追怪方向最小保持秒数（治左右抖动）",
    "engaged_scan": "交战时只扫攻击框（提速，松开即恢复全框）",
    "detect_range_scale": "感知范围倍数（看得远；不影响攻击范围）",
    "detect_range_scale_y": "感知范围倍数·竖直（收窄省时间）",
    "approach_mob": "够不着就走过去打",
    "approach_dy_tol": "「同一层」高度容差（px）",
    "stale_pos_full_frame_after": "位置丢失多少帧后放宽到整帧（0=关）",
    "miss_dump": "丢名字时存一张画面到 debug/（诊断用）",
    "with_enemy_hp_bar": "启用怪物血条检测",
    "hp_bar_color": "怪物血条颜色（BGR）",
    "max_mob_area_trigger": "判定为目标的最小重叠面积",
    "mob_engage_mode": "够得着判据（默认 center=中心在框内 / min_area=交叠面积，老口径）",
    "mob_engage_margin": "宽判定余量（px；判定时把攻击框向外各扩这么多，0=关掉）",
    "detect_log": "写「[怪检测] 明细」行（诊断用；关掉不影响其它日志）",
    "ui_y_start": "界面区域起始 Y 坐标",
    "select_character": "选角色按钮坐标",
    "login_button_thres": "登录按钮匹配阈值",
    "login_button_top_left": "登录按钮搜索区左上角",
    "login_button_bottom_right": "登录按钮搜索区右下角",
    "search_range": "路线搜索半径",
    "color_code_up_down": "上下方向颜色编码（RGB）",
    "timeout": "超时时间（秒）",
    "last_attack_timeout": "最长未攻击时间（秒）",
    "player_color": "小地图玩家点颜色（BGR）",
    "debug_window_upscale": "调试窗口放大倍数",
    "blob_cooldown": "动作标记冷却（秒）",
    "map_padding": "地图边缘留白（像素）",
    "title": "窗口标题",
    "size": "客户区尺寸 [高, 宽]",
    "coord_base_size": "坐标录制尺寸 [高, 宽]",
    "title_bar_height": "标题栏高度（像素）",
    "fps_limit_main": "主循环帧率上限",
    "fps_limit_keyboard_controller": "键盘线程帧率上限",
    "fps_limit_window_capturor": "截图线程帧率上限",
    "fps_limit_route_recorder": "路线录制帧率上限",
    "language": "界面语言",
}

# 同名 key 在不同分组含义不同时，用 "分组.字段" 精确覆盖
SECTION_KEY_CN = {
    "watchdog.range": "移动判定阈值（像素）",
    "nametag.offset": "标签左上角到角色中心的偏移",
    "minimap.offset": "小地图整体偏移",
    "monster_detect.mode": "检测模式",
    "monster_detect.scales": "模板缩放比例（多尺度匹配）",
    "nametag.mode": "比对模式",
    "nametag.pos_cache": "记住上次定位到的位置（按地图存盘）",
    "character.width": "角色宽度（像素）",
    "character.height": "角色高度（像素）",

    # ── route：v0.9「回正线 / 落点锚」专用参数 ────────────────────────────
    #    这批键全是 v0.9 新增的，不补的话高级设置页（route 段被放出来时）
    #    会直接显示英文 key，非程序员看不懂。
    #    v0.9 第二版已**删除「落点锚」概念**，改为「谁近走谁」：
    #    d_home < d_main - home_dist_tol 才进回正，否则主线优先（相等时主线优先）。
    "route.home_dist_tol": "两条线比距离的容差（像素）：明显更靠近回正线才进回正",
    "route.home_min_return_span": "判废③：起点到终点的最小距离（像素），太小视为原地起头",
    "route.home_min_cover_span": "判废④：起始段的最小横向跨度（像素），太小接不住落点区间",
    "route.home_cover_far_tol": "判定「起始段」的容差：离主线最远那批像素的允许误差",
    "route.home_goal_mainline_tol": "判废⑤：终点到主线的最大距离（像素），超了就接不上",
    "route.home_min_leave": "保险丝：回正最小位移（像素，0=关闭）",
    "route.home_min_frames": "保险丝：回正最小持续帧数（0=关闭）",
    "route.home_timeout": "回正超时（秒）",
    "route.home_rearm_margin": "去武装后重新武装：角色离开这条线多远才生效（像素）",
    "route.home_rearm_timeout": "去武装兜底时间（秒）：这么久后无条件重新武装",
    "route.home_reenter_lock": "回正失败后的冷静期（秒，期间不再重试回正、先打怪）",
    # 2026-10-14（用户拍板 A）：梯上让路的"真在爬"窗口。
    #   界面上不显示（与 route.ladder_leave_x 同口径：算法判据，改错会把爬梯搞坏），
    #   但要有个中文名 —— 日志里那行会点名这个键，用户去 yaml 里能找到。
    "route.ladder_still_s": "梯上让路：多久没有竖向移动就不再让路（秒；0=关掉本层，退回老行为）",
    # 2026-10-14（用户拍板 D）：绳梯脚下"蹭一下"自救。
    #   界面上不显示（同 ladder_leave_x / ladder_still_s 口径：算法判据），
    #   但日志里那行会点名 rope_nudge_s，用户去 yaml 里能找到。
    "route.rope_nudge_s": "绳梯脚下自救：按着上下多久没动就开始左右蹭（秒；0=关掉本层）",
    "route.rope_nudge_ms": "绳梯脚下自救：每次蹭多少毫秒",
    "route.rope_nudge_cycle_s": "绳梯脚下自救：两次蹭之间隔多少秒",
    "route.rope_nudge_max_s": "绳梯脚下自救：蹭多久还没动就放弃（秒，之后退回老行为）",
    # 2026-10-14（用户拍板 E1）：goal 像素不再吃掉整帧。
    #   界面上不显示（同 ladder_leave_x / ladder_still_s / rope_nudge_s 口径：
    #   算法判据），但日志里 `[路线·goal过滤]` 那行会点名它，去 yaml 里能找到。
    "route.goal_pixel_skip": "goal 像素过滤（1=开：站在本段终点正下方时不再收到不会动的 `none none goal` 指令；0=关回老行为）",

    # ── route_recoder：v0.9 手绘回正线面板参数 ────────────────────────────
    "route_recoder.home_draw_scale": "手绘画布放大倍数",
    "route_recoder.home_draw_scale_min": "手绘画布最小放大倍数",
    "route_recoder.home_draw_scale_max": "手绘画布最大放大倍数",
    "route_recoder.home_draw_goal_radius": "手绘终点标记半径（像素）",
}


def cn_section(section):
    """分组名 -> 中文标题（查不到返回原名）"""
    return SECTION_CN.get(section, section)


def cn_key(section, key):
    """字段名 -> 中文标签：优先 "分组.字段"，再通用表，都没有则原样返回 key"""
    return SECTION_KEY_CN.get(f"{section}.{key}", KEY_CN.get(key, key))
