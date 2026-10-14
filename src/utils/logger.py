'''
Global Logger
'''
# Standard Import
import logging
import datetime
import os
import sys
import tempfile

# Local import
from src.utils.paths import APP_ROOT, describe, ensure_dir
from src.utils.console import make_console_safe

# 【注意】 必须在 MSLogger() 实例化之前调用：日志挂了 StreamHandler 往 stderr 写，
#    GBK 控制台遇到编不出的字符（emoji）会往 stderr 刷一堆 "--- Logging error ---" 回溯，
#    把日志刷废。详见 src/utils/console.py 的说明（2026-09-10 实测踩坑）。
make_console_safe()


# ============================================================================
# 日志目录的选择（2026-10-14 加，用户 m09283 拍板）
# ============================================================================
# 治的是什么：`MSLogger()` 在 **import 时**就实例化（本文件末尾 `logger = MSLogger()`），
#   所以"随便 import 一下引擎"的自检/工具进程都会在仓库 `log/` 下**新建一条日志**。
#   真机实测（2026-10-14）：`log/` 攒到 **1300 个文件 / 15 MB，其中 1201 个不到 10 KB**
#   —— 用户自己那一轮的会话日志（`MSBot_2026-10-14_15-47-36.log`，2.1 MB）反而被淹在
#   1000 多个空壳里。而且这些空壳比真日志**更新** ⇒ 人工排查时"打开 log 目录里最新的
#   那个文件"会打开一个空壳。
#
# 规则（按优先级，纯函数 `log_dir_candidate` 可离线单测）：
#   ① 环境变量 `MSBOT_LOG_DIR` 非空 ⇒ 用它。相对路径按项目统一约定（APP_ROOT）解析，
#      绝对路径原样用（`ensure_dir` → `resource_path` 已经是这个语义）。
#   ② 没设环境变量，但当前进程是**仓库里的工具/自检**（`python -m tools.xxx` /
#      `python tools/xxx.py`）⇒ 写系统临时目录下的 `msbot_tool_logs/`。
#   ③ 其余（挂机界面 `run_gui.py` / 引擎本体）⇒ 照旧仓库 `log/`，**行为一个字节没变**。
#
# ⚠️ 指定的目录**建不出来**（权限 / 同名占位文件 / 盘符不存在）⇒ 退回 `log/` 并打一条
#    WARNING —— 失败绝不静默：否则"你以为日志在别处、其实在这里"，排查时会找错地方。
# ⚠️ 只影响"日志写到哪"，不影响任何判定逻辑、也不影响**读**日志的地方
#    （自检里读 `log/MSBot_*.log` 的那几处读的仍是用户会话日志，不受影响）。
LOG_DIR_ENV = "MSBOT_LOG_DIR"
TOOL_LOG_DIRNAME = "msbot_tool_logs"


def is_tool_process(argv0=None, app_root=None):
    '''当前进程是不是「仓库里的工具/自检」—— 纯函数，只看 `sys.argv[0]` 的落点。

    判据只有一条：`sys.argv[0]` 指向的文件**就在 `<APP_ROOT>/tools/` 下**。
    不做 `-m` 的字面匹配（`python -m tools.xxx` 会把 `sys.argv[0]` 设成模块文件的
    **完整路径**，但那是解释器行为，不值得依赖字面量），只认最终落点 ⇒
    仓库根目录的 `run_gui.py`、`src/engine/**.py`、打包后的 exe 都不会被误判。

    Args:
        argv0: 要判的路径。None = 用当前进程的 `sys.argv[0]`；"" = 明确当作"不是工具"。
        app_root: 应用根目录。None = 用 `src.utils.paths.APP_ROOT`。

    Returns:
        bool: True = 按"工具进程"处理（日志写临时目录，除非环境变量另有指定）。
    '''
    try:
        if argv0 is None:
            argv0 = (sys.argv or [""])[0]
        if not argv0:
            return False
        root = APP_ROOT if app_root is None else app_root
        here = os.path.dirname(os.path.realpath(str(argv0)))
        want = os.path.realpath(os.path.join(str(root), "tools"))
        return os.path.normcase(here) == os.path.normcase(want)
    except Exception:                       # 路径畸形等 —— 一律当"不是工具"（保守：不动默认行为）
        return False


def log_dir_candidate(env_value=None, argv0=None, temp_dir=None, app_root=None):
    '''纯函数：算出该用哪个日志目录，**不碰文件系统**。

    Returns:
        (目录, 来源)：来源 ∈ {"env", "tool", "default"}。
            目录可能是相对路径（"log"）——真正的绝对化与建目录交给 `ensure_dir`。
    '''
    _env = os.environ.get(LOG_DIR_ENV) if env_value is None else env_value
    if isinstance(_env, str) and _env.strip():
        return _env.strip(), "env"
    if is_tool_process(argv0, app_root):
        _tmp = tempfile.gettempdir() if temp_dir is None else temp_dir
        return os.path.join(str(_tmp), TOOL_LOG_DIRNAME), "tool"
    return "log", "default"


def log_dir_source_text(src):
    '''把来源代号翻成人话（写进日志，便于一眼看出这条日志为什么在这个目录）。'''
    return {
        "env": f"环境变量 {LOG_DIR_ENV}",
        "tool": "自检/工具进程（写系统临时目录）",
        "default": "默认（仓库 log/）",
    }.get(str(src), str(src))


def resolve_log_dir(env_value=None, argv0=None, temp_dir=None, app_root=None):
    '''决定并**建出**日志目录 —— 会碰文件系统。

    Returns:
        (绝对路径, 来源, 警告或 None)。警告非空 = 指定的目录不能用、已退回 `log/`。
    '''
    _cand, _src = log_dir_candidate(env_value, argv0, temp_dir, app_root)
    try:
        return ensure_dir(_cand), _src, None
    except Exception as _e:                  # 权限 / 占位文件 / 盘符不存在 ...
        if _src == "default":
            raise                            # 默认目录都建不出来 ⇒ 照旧抛（今天的行为）
        _fb = ensure_dir("log")
        _msg = (f"[日志] 指定的日志目录不能用：{LOG_DIR_ENV if _src == 'env' else '工具默认目录'}"
                f"={_cand!r}（{type(_e).__name__}: {_e}）"
                f" ⇒ 已退回默认日志目录 {_fb}（这条警告不能忽略：你以为日志在别处，其实在这里）")
        return _fb, "default", _msg

class MSLogger:
    '''
    MapleStory AutoBot Logger
    '''
    def __init__(self, name="MSBot"):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.INFO)

        formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')

        # 日志目录：环境变量 MSBOT_LOG_DIR > 自检/工具进程（临时目录）> 默认 log/
        #   —— 同样走统一路径解析（打包后落在 exe 旁边，而非不确定的 CWD）
        #   —— 详见本文件顶部 `log_dir_candidate` 的说明
        log_dir, _log_src, _log_warn = resolve_log_dir()
        file_handler = logging.FileHandler(
            os.path.join(log_dir, f"{name}_{now_str}.log"), mode='w', encoding="utf-8")
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)

        self._file_handler = file_handler
        self._console_handler = console_handler

        # 启动首行记录运行位置 —— 出问题时第一眼就能看出资源该放哪
        self._logger.info(f"[启动] {describe()}")
        self._logger.info(f"[启动] 日志文件: {file_handler.baseFilename}")
        self._logger.info(f"[启动] 日志目录来源: {log_dir_source_text(_log_src)}"
                          f"（{LOG_DIR_ENV} 可指定；自检/工具进程默认写系统临时目录）")
        if _log_warn:
            self._logger.warning(_log_warn)

    def set_level(self, level):
        '''
        Set logger level, e.g. DEBUG, INFO, WARNING
        '''
        self._logger.setLevel(level)
        self._file_handler.setLevel(level)
        self._console_handler.setLevel(level)

    def info(self, msg):
        self._logger.info(msg)

    def warning(self, msg):
        self._logger.warning(msg)

    def error(self, msg):
        self._logger.error(msg)

    def debug(self, msg):
        self._logger.debug(msg)

    def addHandler(self, handle):
        self._logger.addHandler(handle)

# Initialize shared logger instance for global import
logger = MSLogger()


def jump_trace_on(cfg) -> bool:
    '''回正起跳埋点开关 `log.jump_trace`（纯函数 → 可离线单测）。

    为什么放在 logger.py：引擎线程和键盘线程都要读它，而两侧**都已经 import 了
    logger** —— 放这儿谁都不用再加一条 import，也不会把 numpy/cv2 拖进输入线程。

    为什么**默认开**：这是个排查用的临时埋点，漏记一次就得让用户再掉一次坑。
    配错（缺 `log` 段 / 缺 `jump_trace` 键 / 填了非布尔值）时宁可多打，别静默不记。

    ⚠️ 只影响"打不打日志"，**不影响任何判定逻辑** —— 关掉不会改变机器人行为。
    ⚠️ 不进 `home_params` / `_HOME_PARAM_FLOORS`（进了 FLOORS 就等于"想关关不掉"）。

    Args:
        cfg: 配置 dict。None / 不是 dict / 没有 log 段 → 一律返回 True。

    Returns:
        bool: True = 打埋点日志。
    '''
    try:
        return bool(cfg.get("log", {}).get("jump_trace", True))
    except (AttributeError, TypeError):
        return True


def perf_trace_on(cfg) -> bool:
    '''主循环性能埋点开关 `log.perf_trace`（纯函数 → 可离线单测）。

    套路与 `jump_trace_on` **完全一致**，理由也一致：
    - 放 logger.py 是因为各模块都已 import logger，不用再加 import；
    - **默认开**：排查期漏记一次就得让用户再跑一次；
    - 只影响"打不打日志"，**不影响任何判定逻辑**；
    - 不进 `home_params` / `_HOME_PARAM_FLOORS`（进了就"想关关不掉"）。

    Args:
        cfg: 配置 dict。None / 不是 dict / 没有 log 段 → 一律返回 True。

    Returns:
        bool: True = 打性能埋点日志。
    '''
    try:
        return bool(cfg.get("log", {}).get("perf_trace", True))
    except (AttributeError, TypeError):
        return True


def perf_summary_line(stats) -> str:
    '''把 5 秒窗口内主循环各阶段的耗时压成**一行**日志（纯函数，绝不抛异常）。

    治的是什么（2026-09-14 真机实测）：
        配置 `fps_limit_main: 30`，实测主循环只有 **1.8~2 fps** —— 而全项目
        **没有任何地方记录主循环的实际帧率**（键盘线程会打印 `fps=`，主循环不会），
        于是"一帧 500ms 到底花在哪"完全靠猜。这一行把各阶段耗时**按降序**摊开，
        最慢的排最前，一眼就能看出瓶颈。

    ⚠️ 为什么做成纯函数（跟 `jump_ready` / `home_summary_line` 一个道理）：
        它跑在**主循环**里 —— 在那儿抛异常 = 整个挂机停摆。做成纯函数后
        `tools/verify_perf_trace.py` 能离线把"字段残缺 / 除零 / 类型不对"全钉死，
        不必靠真机去试。

    Args:
        stats: dict，**允许缺任何键**（缺了用占位符或省略该段，绝不抛）
            fps     实际帧率（帧/秒）
            n       本窗口帧数
            viz     bool，是否开着可视化（GUI 的「游戏画面」页签）
            gap_ms  平均帧间隔（毫秒，含限帧 sleep）
            items   [(阶段名, 平均耗时ms, 单帧峰值ms), ...]（顺序无所谓，内部会排序）
                    ⚠️ 平均耗时 <1ms 的阶段会被省略（防噪声刷屏）

    Returns:
        str: 一行日志文本，恒定以 "[性能] " 开头。
    '''
    try:
        d = dict(stats or {})
    except (TypeError, ValueError):
        d = {}

    def _f(k, default=0.0):
        try:
            v = d.get(k, default)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    parts = [f"[性能] 近5秒 fps={_f('fps'):.1f}（{int(_f('n'))}帧）"
             f" 可视化={'开' if d.get('viz') else '关'}",
             f"一帧 {_f('gap_ms'):.0f}ms"]

    rows = []
    try:
        items = list(d.get("items") or [])
    except (TypeError, ValueError):
        items = []
    for it in items:
        try:
            name, avg, peak = str(it[0]), float(it[1]), float(it[2])
        except (TypeError, ValueError, IndexError):
            continue
        if avg < 1.0:
            continue                      # <1ms 省略，防噪声
        rows.append((name, avg, peak))
    rows.sort(key=lambda r: -r[1])        # ★ 最慢的排最前 → 一眼看瓶颈

    if rows:
        parts.append(" ".join(f"{n}{a:.0f}(峰{p:.0f})" for n, a, p in rows))
    else:
        parts.append("各阶段都 <1ms")
    return " | ".join(parts)
