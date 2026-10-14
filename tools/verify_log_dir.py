# -*- coding: utf-8 -*-
'''「日志目录可以选择」自检（2026-10-14 加，用户 m09283 拍板）。

背景（用户 m09283：「环境变量指定日志目录、自检/CI 写临时目录」）：
    真机实测 2026-10-14：`MapleStoryAutoLevelUp-CN/log/` 攒到 **1300 个文件 / 15 MB，
    其中 1201 个不到 10 KB**。根因是 `src/utils/logger.py` 末尾的 `logger = MSLogger()`
    **在 import 时就实例化**，于是每个 import 引擎的自检/工具进程都会在仓库 `log/` 下
    新建一条日志。危害不只是占地方：这些空壳比用户那一轮的会话日志
    （`MSBot_2026-10-14_15-47-36.log`，2.1 MB）**更新** ⇒ 人工排查时「打开 log 目录里
    最新的那个文件」会打开一个空壳。

    同一条日志里我给的验收口径：`[性能]` 一帧仍在 33~35ms、`怪检测` ~16ms/次……

判据（本文件钉的东西）：
    【1】`is_tool_process()` —— 只有一条判据：「`sys.argv[0]` 落在 `<APP_ROOT>/tools/` 下」
        （`python -m tools.x` 会把 argv[0] 设成模块文件的**完整路径**，已实测确认；
          仓库根的 `run_gui.py`、`src/engine/**.py`、打包后的 exe 都不许被误判）。
    【2】`log_dir_candidate()` 的优先级：环境变量 > 工具进程（系统临时目录）> 默认 `log/`。
    【3】`resolve_log_dir()` 的**真行为**：能用就用；**建不出来 ⇒ 退回 log/ 并打 WARNING**
        （失败绝不静默）；相对路径按 APP_ROOT 解析（与 `resource_path` 同一约定）。
    【4】接线白盒：logger 里只有一处读环境变量；`ci.yml` 与工作区的 `_run_all_verify.py`
        都设了 `MSBOT_LOG_DIR`（改了写入方、忘了改判定方是这类机制的典型故障形态）。
    【5】端到端（**真起子进程**）：带环境变量 ⇒ 日志真落在指定目录；不带且不是工具进程
        ⇒ 仍落仓库 `log/`（**挂机/界面的行为一个字节没变** —— 这条必须有，否则就成了
        「顺手改了生产行为」）；工具进程 ⇒ 写系统临时目录。
    【6】口径：日志文件名格式不变、默认目录仍是 `<APP_ROOT>/log`。
'''
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.paths import APP_ROOT          # noqa: E402
import src.utils.logger as _LG                # noqa: E402

PASS = 0
FAIL = 0

# 让子进程打印「它自己的日志文件在哪」——用 `-c` 时 argv[0] = "-c"（不是工具进程）
_PROBE_CODE = "import src.utils.logger as L; print(L.logger._file_handler.baseFilename)"
# 工具进程分支的端到端：临时在 tools/ 下放一个探针模块（跑完就删）
_PROBE_MODULE = os.path.join(APP_ROOT, "tools", "_tmp_logdir_probe.py")


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


def _same(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _file_from_out(out):
    '''从探针输出里挑出那个绝对路径的 .log（找不到返回空串）。'''
    for _l in (out or "").splitlines():
        _l = _l.strip()
        if _l.lower().endswith(".log") and os.path.isabs(_l):
            return _l
    return ""


def _run_probe(extra_env=None, module=None):
    '''起一个真子进程跑探针；`MSBOT_LOG_DIR` 先清干净再按 extra_env 注入。'''
    env = {k: v for k, v in os.environ.items() if k != "MSBOT_LOG_DIR"}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    cmd = ([sys.executable, "-m", module] if module
           else [sys.executable, "-c", _PROBE_CODE])
    p = subprocess.run(cmd, cwd=APP_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _rm(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def main():
    global PASS, FAIL
    _tool_py = os.path.join(APP_ROOT, "tools", "verify_log_dir.py")

    # ------------------------------------------------------------------ 【1】
    print("\n【1】is_tool_process()：判据只有「sys.argv[0] 落在 <APP_ROOT>/tools/ 下」")
    check("tools/ 下的脚本 ⇒ True", _LG.is_tool_process(_tool_py), True)
    check("仓库根的 run_gui.py ⇒ False",
          _LG.is_tool_process(os.path.join(APP_ROOT, "run_gui.py")), False)
    check("引擎源码 ⇒ False",
          _LG.is_tool_process(os.path.join(APP_ROOT, "src", "engine",
                                          "MapleStoryAutoLevelUp.py")), False)
    check("tools 的子目录 ⇒ False（判据是「直接落在 tools/ 下」）",
          _LG.is_tool_process(os.path.join(APP_ROOT, "tools", "sub", "x.py")), False)
    check("空串 ⇒ False", _LG.is_tool_process(""), False)
    check("带 .. 但 realpath 落在 tools/ ⇒ True",
          _LG.is_tool_process(os.path.join(APP_ROOT, "src", "..", "tools", "verify_x.py")), True)
    check("换一个 app_root ⇒ False",
          _LG.is_tool_process(os.path.join(APP_ROOT, "tools", "x.py"),
                              app_root=os.path.join(APP_ROOT, "src")), False)

    # ------------------------------------------------------------------ 【2】
    print("\n【2】log_dir_candidate()：环境变量 > 工具进程 > 默认 log/")
    _T = tempfile.mkdtemp(prefix="msbot_logdir_")
    check("环境变量优先于工具进程",
          _LG.log_dir_candidate(env_value="D:/somewhere",
                                argv0=_tool_py, temp_dir=_T)[0], "D:/somewhere")
    check("环境变量来源标成 env",
          _LG.log_dir_candidate(env_value="D:/somewhere", argv0="" , temp_dir=_T)[1], "env")
    check("空白环境变量 = 当作没设（工具进程分支）",
          _LG.log_dir_candidate(env_value="   ", argv0=_tool_py, temp_dir=_T),
          (os.path.join(_T, "msbot_tool_logs"), "tool"))
    check("工具进程 + 没设环境变量 ⇒ 临时目录",
          _LG.log_dir_candidate(env_value="", argv0=_tool_py, temp_dir=_T),
          (os.path.join(_T, "msbot_tool_logs"), "tool"))
    check("不是工具进程 + 没设环境变量 ⇒ 默认 log/",
          _LG.log_dir_candidate(env_value="", argv0=os.path.join(APP_ROOT, "run_gui.py")),
          ("log", "default"))
    check("相对路径原样返回（绝对化交给 ensure_dir/resource_path）",
          _LG.log_dir_candidate(env_value="debug/x", argv0="")[0], "debug/x")

    # ------------------------------------------------------------------ 【3】
    print("\n【3】resolve_log_dir()：真建目录 / 相对路径按 APP_ROOT / 建不出来退回并留痕")
    _d, _src, _warn = _LG.resolve_log_dir(env_value=_T, argv0="")
    check("指定绝对目录 ⇒ 原样用", _same(_d, _T), True)
    check("来源 = env", _src, "env")
    check("没有警告", _warn, None)
    check_true("目录确实存在", os.path.isdir(_T))

    _rel = "debug/_verify_log_dir_tmp"
    _d2, _src2, _ = _LG.resolve_log_dir(env_value=_rel, argv0="")
    check("相对路径按 APP_ROOT 解析（与 resource_path 同一约定）",
          _same(_d2, os.path.join(APP_ROOT, "debug", "_verify_log_dir_tmp")), True)
    try:
        os.rmdir(_d2)                      # 只可能是空目录（本检查刚建出来）
    except OSError:
        pass

    _d3, _src3, _ = _LG.resolve_log_dir(env_value="  ", argv0=_tool_py, temp_dir=_T)
    check("工具进程 ⇒ 临时目录下的 msbot_tool_logs",
          _same(_d3, os.path.join(_T, "msbot_tool_logs")), True)
    check("来源 = tool", _src3, "tool")

    _occupy = os.path.join(_T, "occupying_file")
    with open(_occupy, "w", encoding="utf-8") as _fp:
        _fp.write("x")                     # 用「文件」占位，让 makedirs 必失败
    _d4, _src4, _w4 = _LG.resolve_log_dir(env_value=os.path.join(_occupy, "sub"), argv0="")
    check("建不出来 ⇒ 退回默认 log/", _same(_d4, os.path.join(APP_ROOT, "log")), True)
    check("来源改标 default", _src4, "default")
    check_true("退回必须留痕（WARNING 文案齐全）",
               bool(_w4) and "不能用" in _w4 and "已退回默认日志目录" in _w4, str(_w4)[:60])

    # ------------------------------------------------------------------ 【4】
    print("\n【4】接线白盒：写入方与判定方都要改（改了写入方忘了判定方 = 静默失效）")
    with open(os.path.join(APP_ROOT, "src", "utils", "logger.py"), encoding="utf-8") as _fp:
        _LSRC = _fp.read()
    check("logger 里读环境变量只有一处", _LSRC.count("os.environ.get(LOG_DIR_ENV)"), 1)
    check("LOG_DIR_ENV 常量只有一处定义", _LSRC.count('LOG_DIR_ENV = "MSBOT_LOG_DIR"'), 1)
    check("MSLogger 里 resolve_log_dir() 只调一处", _LSRC.count("resolve_log_dir()"), 1)
    check_true("启动日志带「日志目录来源」行", "[启动] 日志目录来源:" in _LSRC)
    check_true("退回时走 warning 留痕", "self._logger.warning(_log_warn)" in _LSRC)
    check("三种来源的人话文案齐全",
          sum(t in _LSRC for t in ("环境变量", "自检/工具进程", "默认（仓库 log/）")), 3)

    with open(os.path.join(APP_ROOT, ".github", "workflows", "ci.yml"),
              encoding="utf-8") as _fp:
        _CI = _fp.read()
    check_true("ci.yml 里给自检设了 MSBOT_LOG_DIR", "$env:MSBOT_LOG_DIR" in _CI)

    _runner = os.path.join(os.path.dirname(APP_ROOT), "_run_all_verify.py")
    if os.path.isfile(_runner):
        with open(_runner, encoding="utf-8") as _fp:
            _RSRC = _fp.read()
        check_true("_run_all_verify.py 里给子进程注入了 MSBOT_LOG_DIR",
                   "MSBOT_LOG_DIR" in _RSRC and "env={**os.environ" in _RSRC)
    else:
        print("  [SKIP] 工作区根的 _run_all_verify.py 不在（它不进仓库）；"
              "CI 侧由 ci.yml 的 $env:MSBOT_LOG_DIR 覆盖")

    # ------------------------------------------------------------------ 【5】
    print("\n【5】端到端（真起子进程）：日志到底落在哪")
    _e1 = os.path.join(_T, "e2e_env")
    _rc, _out = _run_probe({"MSBOT_LOG_DIR": _e1})
    _got = _file_from_out(_out)
    check_true("带 MSBOT_LOG_DIR ⇒ 日志落在指定目录",
               bool(_got) and _same(os.path.dirname(_got), _e1), f"{_got} rc={_rc}")
    check_true("那个文件真的存在", bool(_got) and os.path.isfile(_got))
    _rm(_got)

    _rc2, _out2 = _run_probe({})                        # 不带环境变量、argv0 = "-c"
    _got2 = _file_from_out(_out2)
    check_true("不带环境变量且不是工具进程 ⇒ 仍写仓库 log/（挂机行为没变）",
               bool(_got2) and _same(os.path.dirname(_got2), os.path.join(APP_ROOT, "log")),
               f"{_got2} rc={_rc2}")
    if _got2 and os.path.isfile(_got2):
        print(f"          （本条刚在仓库 log/ 里建了一条并立刻删除：{os.path.basename(_got2)}）")
    _rm(_got2)

    _tool_log_dir = os.path.join(tempfile.gettempdir(), _LG.TOOL_LOG_DIRNAME)
    try:
        with open(_PROBE_MODULE, "w", encoding="utf-8") as _fp:
            _fp.write("# -*- coding: utf-8 -*-\n" + _PROBE_CODE + "\n")
        _rc3, _out3 = _run_probe({}, module="tools._tmp_logdir_probe")
        _got3 = _file_from_out(_out3)
        check_true("工具进程（python -m tools.x）⇒ 写系统临时目录的 msbot_tool_logs",
                   bool(_got3) and _same(os.path.dirname(_got3), _tool_log_dir),
                   f"{_got3} rc={_rc3}")
        _rm(_got3)
    finally:
        _rm(_PROBE_MODULE)

    # ------------------------------------------------------------------ 【6】
    print("\n【6】口径：文件名格式与默认目录不变")
    check("环境变量名", _LG.LOG_DIR_ENV, "MSBOT_LOG_DIR")
    check("工具日志子目录名", _LG.TOOL_LOG_DIRNAME, "msbot_tool_logs")
    check_true("默认目录仍是 <APP_ROOT>/log",
               _same(_LG.resolve_log_dir(env_value="", argv0=os.path.join(APP_ROOT, "run_gui.py"))[0],
                     os.path.join(APP_ROOT, "log")))
    _probe_name = "MSBot_2026-10-14_15-47-36.log"
    check_true("日志文件名格式没变（MSBot_年-月-日_时-分-秒.log）",
               bool(re.match(r"^MSBot_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.log$", _probe_name)))
    _rc4, _out4 = _run_probe({"MSBOT_LOG_DIR": _T})
    _got4 = _file_from_out(_out4)
    check_true("真进程产出的文件名也符合该格式（端到端口径）",
               bool(_got4) and bool(re.match(r"^MSBot_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.log$",
                                             os.path.basename(_got4))),
               os.path.basename(_got4) if _got4 else "(无)")
    _rm(_got4)

    shutil.rmtree(_T, ignore_errors=True)
    print(f"\n===== verify_log_dir: 通过 {PASS} / 失败 {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
