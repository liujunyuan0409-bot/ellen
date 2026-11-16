#!/usr/bin/env python3
"""飞书备份唯一入口。由 AI 助手（非交互地运行命令）和定时任务（经程序副本的 launch.py 运行 run）调用。

子命令：install / setup / doctor / login / scan / run / status / perms / schedule。
每个命令都打印中文说明；加 --json 时输出机器可读的 JSON。
退出码：检查失败为 1；run 原样返回 runner 的退出码（0/1/2/3/4）。
"""
import argparse
import contextlib
import csv
import dataclasses
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fbk import auth, config, runner, schedule, wizard  # noqa: E402
from fbk import platform as fplat  # noqa: E402
from fbk.client import FeishuClient  # noqa: E402
from fbk.names import SHANGHAI  # noqa: E402
from fbk.store import Store  # noqa: E402

STEPS = ["环境检查", "应用凭证", "登录授权", "权限体检", "备份目录", "首次扫描", "定时任务"]
DOCTOR_STATE = "doctor.json"
SCAN_STATE = "scan.json"
RUN_MEANING = {0: "正常结束（或还没到时间、已有备份在跑、网络不通时静默跳过）", 1: "运行出错，详见日志",
               2: "还没配置好，或备份目录不存在（外接盘没插？）", 3: "需要重新登录：运行 setup --login",
               4: "备份盘空间不足"}
MARK = {"done": "✓", "failed": "✗", "todo": "–"}


# ---------------- 小工具 ----------------

WINDOWS_NOTICE = "⚠ Windows 版未经真机验证，遇到问题请对照 WINDOWS_CHECKLIST.md"


def _emit(args, data, text, notice=False):
    """notice=True（doctor / status）且在 Windows 上：文字输出第一行加标注，JSON 加 windows_unverified。"""
    win = notice and fplat.IS_WINDOWS
    if getattr(args, "json", False):
        if win:
            data = dict(data, windows_unverified=True)
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print((WINDOWS_NOTICE + "\n" + text) if win else text)


def _checks_out(args, checks, title=None, notice=False):
    ok = all(c.ok for c in checks)
    text = wizard.render_checks(checks)
    if title:
        text = title + "\n" + text
    _emit(args, {"ok": ok, "checks": [asdict(c) for c in checks]}, text, notice)
    return 0 if ok else 1


def _fail(args, detail, fix="", notice=False):
    return _checks_out(args, [wizard.Check("前置条件", False, detail, fix)], notice=notice)


def _fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), SHANGHAI).strftime("%Y-%m-%d %H:%M") if ts else None


def _load_state(name):
    try:
        return json.loads((config.CONFIG_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(name, data):
    config._write_private(name, data)


def _cfg_or_none():
    try:
        return config.load_config()
    except (config.ConfigMissing, ValueError, KeyError):
        return None


def _token_or_none():
    try:
        tok = config.load_token()
    except (OSError, ValueError):
        return None  # 损坏的 token.json 当作未登录
    return tok if isinstance(tok, dict) else None


def _make_client(cfg):
    tm = auth.TokenManager(cfg)
    return FeishuClient(tm.access_token)


SCHEDULE_FIX = "重新运行 setup --schedule（插件更新或换了 Python 之后都要重新运行一次）"


def _schedule_check():
    """已装定时任务时检查它还能不能跑；没装返回 None。"""
    probs = schedule.verify()
    if probs is None:
        return None
    if probs:
        return wizard.Check("定时任务", False, "；".join(probs), SCHEDULE_FIX)
    return wizard.Check("定时任务", True, "已安装，程序版本 %s" % schedule.__version__)


def _need_bytes():
    st = _load_state(SCAN_STATE) or {}
    return int(st.get("upload_bytes") or 0) + int(st.get("export_est_bytes") or 0)


# ---------------- setup ----------------

def cmd_credentials(args):
    title = "[2/7] 应用凭证"
    try:
        app_id, secret = wizard.prompt_secret()
    except wizard.Cancelled as e:
        return _checks_out(args, [wizard.Check("应用凭证", False, str(e), "重新运行 setup --credentials")], title)
    old = _cfg_or_none()
    cfg = dataclasses.replace(old, app_id=app_id, app_secret=secret) if old else config.Config(
        app_id=app_id, app_secret=secret)
    c = wizard.check_credentials(cfg)
    if c.ok:
        config.save_config(cfg)
        c.detail += "，已保存（App ID：%s）" % app_id
    return _checks_out(args, [c], title)


def cmd_login(args):
    title = "[3/7] 登录授权"
    cfg = _cfg_or_none()
    if cfg is None:
        return _fail(args, "还没有应用凭证", "先运行 setup --credentials")
    try:
        # run_login 会打印授权链接；--json 时改打到 stderr，免得弄脏 JSON
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            tok = auth.run_login(cfg)
        info = _make_client(cfg).get("/authen/v1/user_info")
        name = info.get("name") or info.get("en_name") or "（没读到姓名）"
        checks = [wizard.Check("登录", True, "已登录：%s" % name)]
        miss = wizard.check_granted_scopes(tok)
        if miss:
            checks.append(miss)
        return _checks_out(args, checks, title)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):  # EADDRINUSE
            c = wizard.Check("登录", False, "本机端口 8765 被占用", "关掉占用 8765 端口的程序（或重启电脑）后重新运行 setup --login")
        else:
            d, f = wizard.explain(e)
            c = wizard.Check("登录", False, d, f)
    except auth.LoginRequired as e:
        c = wizard.Check("登录", False, str(e),
                         "确认开发者后台「安全设置 → 重定向 URL」里有 %s，然后重新运行 setup --login" % auth.REDIRECT_URI)
    except Exception as e:  # noqa: BLE001
        d, f = wizard.explain(e)
        c = wizard.Check("登录", False, d, f)
    return _checks_out(args, [c], title)


def cmd_root(args):
    title = "[5/7] 备份目录"
    cfg = _cfg_or_none()
    if cfg is None:
        return _fail(args, "还没有应用凭证", "先运行 setup --credentials")
    path = Path(os.path.expanduser(args.root))
    if args.create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _checks_out(args, [wizard.Check("目录存在", False, "建不了目录：%s" % e.strerror,
                                                   "换一个路径，或确认外接盘已插好")], title)
    checks = wizard.check_root(path, _need_bytes())
    if all(c.ok for c in checks):
        cfg.backup_root = path.resolve()
        config.save_config(cfg)
        checks.append(wizard.Check("已保存", True, "备份目录设为 %s" % cfg.backup_root))
    return _checks_out(args, checks, title)


def _slot_text(cfg) -> str:
    hh, mm = schedule.parse_time(cfg.schedule_time)
    return "每%s %02d:%02d" % (schedule.weekday_name(cfg.schedule_weekday), hh, mm)


def _next_run_dt(cfg) -> datetime:
    hh, mm = schedule.parse_time(cfg.schedule_time)
    return schedule.next_slot(datetime.now().astimezone(), cfg.schedule_weekday, hh, mm)


def _launch_cmd() -> str:
    """用户手动运行程序副本的命令（启动器不在时退回仓库里的 fbk.py）。"""
    launcher = schedule.default_app_root() / "launch.py"
    target = launcher if launcher.is_file() else schedule.default_script()
    return ('py -3 "%s"' if schedule.IS_WINDOWS else 'python3 "%s"') % target


def cmd_install(args):
    try:
        launcher = schedule.install_app()
    except Exception as e:  # noqa: BLE001
        return _checks_out(args, [wizard.Check("程序副本", False, "复制失败：%s" % e, "把这一行发给你的 AI 助手排查")])
    app_dir = launcher.parent / "versions" / schedule.__version__
    data = {"ok": True, "launcher": str(launcher), "app_dir": str(app_dir), "version": schedule.__version__}
    text = ("程序副本已复制好（版本 %s）：%s\n启动器：%s\n"
            "这一步没有安装定时任务；要每周自动备份，请运行 setup --schedule。") % (
        schedule.__version__, app_dir, launcher)
    _emit(args, data, text)
    return 0


def _tls_problem_check(problem, python):
    """setup --schedule 前用定时任务要用的 Python 试连飞书失败：不安装，说清楚怎么修。"""
    if problem == "cert":
        return wizard.Check("定时任务", False,
                            "定时任务要用的 Python（%s）连飞书时证书校验失败，装上也备份不了，所以这次没有安装" % python,
                            wizard.cert_fix(python, is_windows=schedule.IS_WINDOWS) + "；修好后重新运行 setup --schedule")
    if problem == "network":
        return wizard.Check("定时任务", False, "用定时任务要用的 Python（%s）连不上飞书，所以这次没有安装" % python,
                            "检查网络（公司网络可能要设代理）后重新运行 setup --schedule")
    return wizard.Check("定时任务", False, "定时任务要用的 Python（%s）运行不了，所以这次没有安装" % python,
                        "把这一行发给你的 AI 助手排查")


def cmd_schedule_install(args, title="[7/7] 定时任务"):
    try:
        cfg = config.load_config()
    except config.ConfigCorrupt as e:
        return _fail(args, str(e), "重新运行 setup --credentials（会重写配置文件），再运行 setup --root <备份目录>，"
                                   "然后再运行 setup --schedule")
    except (config.ConfigMissing, ValueError, KeyError):
        cfg = None
    if cfg is None or not cfg.backup_root:
        return _fail(args, "还没设置备份目录", "先完成 setup --credentials 和 setup --root")
    wd, tm = getattr(args, "weekday", None), getattr(args, "time", None)
    try:
        if wd is not None:
            w = str(wd).strip()
            if not (w.isdigit() and w.isascii() and 1 <= int(w) <= 7):
                raise ValueError("星期不对：%s，请写 1–7（1=周一 … 7=周日）" % wd)
            wd = int(w)
        if tm is not None:
            schedule.parse_time(tm)
    except ValueError as e:
        return _fail(args, str(e), "例如：setup --schedule --weekday 1 --time 12:00")
    if wd is not None or tm is not None:
        if wd is not None:
            cfg.schedule_weekday = wd
        if tm is not None:
            cfg.schedule_time = "%02d:%02d" % schedule.parse_time(tm)
        config.save_config(cfg)
    python = schedule.pick_python()
    problem = schedule.tls_probe(python)
    if problem is not None:
        return _checks_out(args, [_tls_problem_check(problem, python)], title)
    try:
        path = schedule.install(python=python)
    except Exception as e:  # noqa: BLE001
        return _checks_out(args, [wizard.Check("定时任务", False, "安装失败：%s" % e, "把这一行发给你的 AI 助手排查")], title)
    if cfg.schedule_installed_at is None:
        cfg.schedule_installed_at = int(time.time())
        config.save_config(cfg)
    detail = "已安装（%s）。计划：%s（错过会在开机后补跑），本机时区。下次计划：%s" % (
        path, _slot_text(cfg), _next_run_dt(cfg).strftime("%Y-%m-%d %H:%M"))
    fix = "卸载：%s schedule uninstall；插件更新后请重新运行 setup --schedule" % _launch_cmd()
    c = wizard.Check("定时任务", True, detail, fix)
    if args.json:
        return _checks_out(args, [c], title)
    print("%s\n%s\n    %s" % (title, wizard.render_checks([c]), fix))
    return 0


def _setup_steps():
    v = sys.version_info
    steps = [("done" if tuple(v[:2]) >= wizard.MIN_PY else "failed", "Python %d.%d（网络未检查）" % v[:2])]
    cfg = _cfg_or_none()
    steps.append(("done", "已保存 App ID：%s" % cfg.app_id) if cfg and cfg.app_secret
                 else ("todo", "运行 setup --credentials"))
    tok = _token_or_none()
    steps.append(("done", "已登录（未联网验证）") if tok and tok.get("refresh_token") else ("todo", "运行 setup --login"))
    doc = _load_state(DOCTOR_STATE)
    if doc is None:
        steps.append(("todo", "运行 doctor"))
    else:
        steps.append(("done" if doc.get("ok") else "failed",
                      "%s 体检%s" % (_fmt_ts(doc.get("at")), "全部通过" if doc.get("ok") else "有未通过项，运行 doctor 查看")))
    root = cfg.backup_root if cfg else None
    if not root:
        steps.append(("todo", "运行 setup --root <路径>"))
    elif Path(root).is_dir():
        steps.append(("done", str(root)))
    else:
        steps.append(("failed", "%s 不存在（外接盘没插？）" % root))
    sc = _load_state(SCAN_STATE)
    steps.append(("done", "%s 扫描过" % _fmt_ts(sc.get("at"))) if sc else ("todo", "运行 scan"))
    probs = schedule.verify()
    if probs is None:
        steps.append(("todo", "运行 setup --schedule"))
    elif probs:
        steps.append(("failed", "已安装但跑不了：%s。怎么办：%s" % ("；".join(probs), SCHEDULE_FIX)))
    else:
        steps.append(("done", "已安装"))
    return [{"step": i + 1, "name": STEPS[i], "state": s, "detail": d} for i, (s, d) in enumerate(steps)]


def cmd_setup_status(args):
    steps = _setup_steps()
    lines = ["[%d/7] %s %s：%s" % (s["step"], MARK[s["state"]], s["name"], s["detail"]) for s in steps]
    nxt = next((s for s in steps if s["state"] != "done"), None)
    lines.append("全部完成。" if nxt is None else "下一步：%s" % nxt["detail"])
    _emit(args, {"steps": steps, "complete": nxt is None}, "\n".join(lines))
    return 0


def cmd_setup(args):
    if args.credentials:
        return cmd_credentials(args)
    if args.login:
        return cmd_login(args)
    if args.root:
        return cmd_root(args)
    if args.schedule:
        return cmd_schedule_install(args)
    if args.env:
        return _checks_out(args, wizard.check_env(), "[1/7] 环境检查")
    return cmd_setup_status(args)


# ---------------- 其他命令 ----------------

def cmd_doctor(args):
    cfg = _cfg_or_none()
    if cfg is None:
        return _fail(args, "还没有应用凭证", "先运行 setup --credentials", notice=True)
    checks = wizard.doctor(_make_client(cfg))
    sc = _schedule_check()
    if sc is not None:
        checks.append(sc)
    ok = all(c.ok for c in checks)
    _save_state(DOCTOR_STATE, {"at": int(time.time()), "ok": ok})
    return _checks_out(args, checks, "[4/7] 权限体检", notice=True)


def cmd_scan(args):
    cfg = _cfg_or_none()
    if cfg is None:
        return _fail(args, "还没有应用凭证", "先运行 setup --credentials")
    try:
        st = wizard.scan_stats(_make_client(cfg))
    except Exception as e:  # noqa: BLE001
        d, f = wizard.explain(e)
        return _checks_out(args, [wizard.Check("首次扫描", False, d, f)], "[6/7] 首次扫描")
    if st["total_items"] == 0 and st["walk_errors"]:
        return _checks_out(args, [wizard.Check("首次扫描", False, "一项都没读到（%d 处目录读取失败）" % st["walk_errors"],
                                               "先运行 doctor 体检，看是网络还是权限问题")], "[6/7] 首次扫描")
    st["at"] = int(time.time())
    _save_state(SCAN_STATE, st)
    text = "[6/7] 首次扫描\n" + wizard.format_scan(st)
    checks = []
    if cfg.backup_root and Path(cfg.backup_root).is_dir():
        checks = wizard.check_root(cfg.backup_root, st["upload_bytes"] + st["export_est_bytes"])
        text += "\n备份目录核对：\n" + wizard.render_checks(checks)
    st["root_checks"] = [asdict(c) for c in checks]
    _emit(args, st, text)
    return 0 if all(c.ok for c in checks) else 1


def cmd_run(args):
    runner.install_term_handler()
    code = runner.run(force=args.now, limit=args.limit)
    _emit(args, {"exit_code": code, "meaning": RUN_MEANING.get(code, "")},
          "本次结果：%s" % RUN_MEANING.get(code, "退出码 %s" % code))
    return code


def _newest(d: Path, pattern):
    files = sorted(d.glob(pattern)) if d.is_dir() else []
    return str(files[-1]) if files else None


def _status_data():
    cfg = _cfg_or_none()
    tok = _token_or_none()
    root = Path(cfg.backup_root) if cfg and cfg.backup_root else None
    d = {"config": cfg is not None, "logged_in": bool(tok and tok.get("refresh_token")),
         "backup_root": str(root) if root else None, "root_exists": bool(root and root.is_dir()),
         "last_success": None, "last_success_text": "从未成功", "last_run": None,
         "schedule_text": None, "next_run_text": None, "schedule_problem": None,
         "schedule_installed": False, "schedule_problems": [],
         "counts": {"items": {}, "attachments": {}, "links": {}},
         "reports": {"log": None, "perms": None}}
    probs = schedule.verify()
    d["schedule_installed"], d["schedule_problems"] = probs is not None, probs or []
    if cfg:
        try:
            d["schedule_text"] = _slot_text(cfg)
            d["next_run_text"] = _next_run_dt(cfg).strftime("%Y-%m-%d %H:%M")
        except (ValueError, IndexError) as e:
            d["schedule_problem"] = str(e)
    else:
        try:
            config.load_config()
        except config.ConfigCorrupt as e:
            d["schedule_problem"] = str(e)
        except config.ConfigMissing:
            pass
    db = root / schedule.STATE_DIR / "manifest.sqlite" if root else None
    if db and db.exists():
        store = Store(db)
        try:
            ls = store.last_success()
            if ls:
                d["last_success"], d["last_success_text"] = ls, _fmt_ts(ls)
            for table in ("items", "attachments", "links"):
                d["counts"][table] = {r[0] or "": r[1] for r in store._read(
                    "SELECT status, COUNT(*) FROM %s GROUP BY status ORDER BY status" % table)}
            rows = store._read("SELECT started, finished, ok, summary FROM runs ORDER BY id DESC LIMIT 1")
            if rows:
                r = rows[0]
                d["last_run"] = {"started": _fmt_ts(r[0]), "finished": _fmt_ts(r[1]),
                                 "ok": bool(r[2]) if r[2] is not None else None,
                                 "summary": json.loads(r[3]) if r[3] else None}
        finally:
            store.close()
    if root:
        rd = root / runner.REPORT_DIR
        d["reports"] = {"log": _newest(rd, "*_备份日志.md"), "perms": _newest(rd, "*_权限缺口.csv")}
    return d


_STATUS_CN = {"ok": "已备份", "failed": "失败待重试", "duplicate": "重复（另见）", "skipped": "跳过",
              "remote_deleted": "云端已删除", "access_lost": "失去权限", "denied": "无权限", "gone": "已删除或无效",
              "retry": "待重试"}


def cmd_status(args):
    d = _status_data()
    lines = ["配置：%s" % ("已完成" if d["config"] else "还没有（运行 setup）"),
             "登录：%s" % ("已登录" if d["logged_in"] else "未登录（运行 setup --login）"),
             "备份目录：%s" % (d["backup_root"] or "未设置") +
             ("" if not d["backup_root"] or d["root_exists"] else "（不存在，外接盘没插？）"),
             "上次成功：%s" % d["last_success_text"]]
    if d["schedule_problem"]:
        lines.append("计划时间有问题：%s（请检查配置文件里的 schedule_weekday 和 schedule_time）" % d["schedule_problem"])
    elif d["config"]:
        lines.append("下次计划：%s（%s）" % (d["schedule_text"], d["next_run_text"]))
    lines += [
             "定时任务：%s" % (("✗ 已安装但跑不了：%s。怎么办：%s" % ("；".join(d["schedule_problems"]), SCHEDULE_FIX))
                              if d["schedule_problems"] else ("已安装" if d["schedule_installed"] else "未安装"))]
    if d["last_run"]:
        lr = d["last_run"]
        lines.append("最近一次运行：%s 开始，%s" % (lr["started"], "成功" if lr["ok"] else
                                             ("还在跑或被中断" if lr["finished"] is None else "没有完全成功")))
    for table, label in (("items", "云文档"), ("attachments", "群聊附件"), ("links", "群聊链接")):
        c = d["counts"][table]
        if c:
            lines.append("%s：%s" % (label, "，".join("%s %d" % (_STATUS_CN.get(k, k), v) for k, v in c.items())))
    if d["reports"]["log"]:
        lines.append("最近备份日志：%s" % d["reports"]["log"])
    if d["reports"]["perms"]:
        lines.append("最近权限缺口报告：%s" % d["reports"]["perms"])
    _emit(args, d, "\n".join(lines), notice=True)
    return 0


def cmd_perms(args):
    cfg = _cfg_or_none()
    root = Path(cfg.backup_root) if cfg and cfg.backup_root else None
    path = _newest(root / runner.REPORT_DIR, "*_权限缺口.csv") if root else None
    if not path:
        _emit(args, {"report": None}, "还没有权限缺口报告（完成一次完整备份后生成）。")
        return 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        lines = f.read().splitlines()
    head = [ln.lstrip("# ") for ln in lines if ln.startswith("#")]
    rows = list(csv.DictReader([ln for ln in lines if not ln.startswith("#")]))
    by = {}
    for r in rows:
        by[r.get("状态", "")] = by.get(r.get("状态", ""), 0) + 1
    text = "权限缺口报告：%s\n%s\n按状态：%s" % (path, "\n".join(head),
                                           "，".join("%s %d" % kv for kv in sorted(by.items())) or "无")
    _emit(args, {"report": path, "summary": head, "by_status": by}, text)
    return 0


def cmd_schedule(args):
    if args.action == "install":
        return cmd_schedule_install(args, title="定时任务")
    try:
        schedule.uninstall()
    except (RuntimeError, OSError) as e:
        fix = (("打开「任务计划程序」，在左边「任务计划程序库」里找到「%s」，右键删除；还不行就把这一行发给你的 AI 助手"
                % schedule.TASK_NAME) if schedule.IS_WINDOWS else "把这一行发给你的 AI 助手排查")
        return _checks_out(args, [wizard.Check("卸载定时任务", False, "没卸载掉：%s" % e, fix)])
    _emit(args, {"ok": True}, "已卸载定时任务。备份文件和清单都保留，没有删除任何东西。")
    return 0


# ---------------- argparse ----------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    p = argparse.ArgumentParser(prog="fbk.py", description="飞书备份")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    s = sub.add_parser("setup", parents=[common], help="配置向导（不带参数时显示进度）")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--env", action="store_true", help="[1/7] 环境检查")
    g.add_argument("--credentials", action="store_true", help="[2/7] 弹窗输入 App ID / App Secret")
    g.add_argument("--login", action="store_true", help="[3/7] 浏览器登录授权")
    g.add_argument("--root", metavar="PATH", help="[5/7] 设置备份目录")
    g.add_argument("--schedule", action="store_true", help="[7/7] 安装定时任务")
    s.add_argument("--weekday", default=None, help="配合 --schedule：星期几（1=周一 … 7=周日，默认保持现有，初次为 1）")
    s.add_argument("--time", default=None, help="配合 --schedule：几点（HH:MM，本机时区，默认保持现有，初次为 12:00）")
    g.add_argument("--status", action="store_true", help="显示每一步是否完成（不联网）")
    s.add_argument("--create", action="store_true", help="配合 --root：目录不存在时新建")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("install", parents=[common],
                   help="把程序复制到本机固定位置并打印位置（不装定时任务）").set_defaults(func=cmd_install)
    sub.add_parser("login", parents=[common], help="重新登录（同 setup --login）").set_defaults(func=cmd_login)
    sub.add_parser("doctor", parents=[common], help="权限体检（8 项实测）").set_defaults(func=cmd_doctor)
    sub.add_parser("scan", parents=[common], help="只统计不下载").set_defaults(func=cmd_scan)

    r = sub.add_parser("run", parents=[common], help="备份一次（不加 --now 时没到时间就跳过）")
    r.add_argument("--now", action="store_true", help="不管上次什么时候成功，现在就备份")
    r.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_run)

    sub.add_parser("status", parents=[common], help="备份状态").set_defaults(func=cmd_status)
    sub.add_parser("perms", parents=[common], help="最新权限缺口报告的汇总").set_defaults(func=cmd_perms)

    sc = sub.add_parser("schedule", parents=[common], help="安装或卸载定时任务")
    sc.add_argument("action", choices=["install", "uninstall"])
    sc.set_defaults(func=cmd_schedule)
    return p


def _utf8_stdio(streams, is_windows=None, env=os.environ):
    """中文 Windows 上输出被 AI 工具或重定向接走时，Python 默认按 GBK 编码，打印 ✓ ✗ ⚠ 会报错退出：
    改成 UTF-8（编不了的字符替换掉，绝不因输出报错）。用户自己设了 PYTHONIOENCODING 就照用户的。"""
    win = fplat.IS_WINDOWS if is_windows is None else is_windows
    if not win or env.get("PYTHONIOENCODING"):
        return
    for s in streams:
        enc = (getattr(s, "encoding", None) or "").lower().replace("-", "")
        if s is not None and enc != "utf8" and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


def main(argv=None):
    _utf8_stdio([sys.stdout, sys.stderr])
    args = build_parser().parse_args(argv)
    if getattr(args, "create", False) and not getattr(args, "root", None):
        print("--create 只能和 --root 一起用")
        return 1
    if args.cmd == "login":
        args.login = True
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
