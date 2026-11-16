"""路径、Config、token 读写。配置文件 0600，目录 0700。"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .platform import paths as _paths

CONFIG_DIR = _paths.config_dir()
LOG_DIR = _paths.log_dir()


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(s: str) -> Tuple[int, int]:
    """'HH:MM' → (时, 分)；不合法抛 ValueError（中文信息）。schedule.parse_time 与加载配置共用这一套规则。"""
    m = _TIME_RE.match((s or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("时间格式不对：%r，请写成 HH:MM，例如 12:00" % (s,))
    return int(m.group(1)), int(m.group(2))


class ConfigMissing(Exception):
    pass


class ConfigCorrupt(ConfigMissing, ValueError):
    """config.json 读得到但解析不了或缺字段：按「还没配置好」处理，提示重新设置。"""


@dataclass
class Config:
    app_id: str
    app_secret: str
    backup_root: Optional[Path] = None
    interval_hours: int = 24
    lookback_days: int = 180
    schedule_weekday: int = 1  # ISO：1=周一 … 7=周日
    schedule_time: str = "12:00"  # 本机时区
    schedule_installed_at: Optional[int] = None  # 第一次 setup --schedule 成功的时间（从没成功备份过时，过期提醒以它为起点）


def _write_private(name: str, data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    path = CONFIG_DIR / name
    # 先写同目录临时文件再 os.replace：中途崩溃不会写坏旧文件（refresh token 只能用一次）
    tmp = CONFIG_DIR / (".%s.%d.tmp" % (name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _opt_int(v) -> Optional[int]:
    """可选的整数字段：缺失或写坏了就当没有（只影响提醒的起点，不值得因此判配置损坏）。"""
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def load_config() -> Config:
    path = CONFIG_DIR / "config.json"
    if not path.exists():
        raise ConfigMissing("未找到配置文件：%s" % path)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        root = d.get("backup_root")
        wd, tm = int(d.get("schedule_weekday", 1)), str(d.get("schedule_time", "12:00"))
        if not 1 <= wd <= 7:
            raise ValueError("计划星期不合法：%s（应为 1–7）" % wd)
        parse_time(tm)
        return Config(
            app_id=d["app_id"],
            app_secret=d["app_secret"],
            backup_root=Path(root) if root else None,
            interval_hours=int(d.get("interval_hours", 24)),
            lookback_days=int(d.get("lookback_days", 180)),
            schedule_weekday=int(d.get("schedule_weekday", 1)),
            schedule_time=str(d.get("schedule_time", "12:00")),
            schedule_installed_at=_opt_int(d.get("schedule_installed_at")),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ConfigCorrupt("配置文件损坏：%s（%s）" % (path, e))


def save_config(cfg: Config) -> None:
    _write_private("config.json", {
        "app_id": cfg.app_id,
        "app_secret": cfg.app_secret,
        "backup_root": str(cfg.backup_root) if cfg.backup_root else None,
        "interval_hours": cfg.interval_hours,
        "lookback_days": cfg.lookback_days,
        "schedule_weekday": cfg.schedule_weekday,
        "schedule_time": cfg.schedule_time,
        "schedule_installed_at": cfg.schedule_installed_at,
    })


def load_token() -> Optional[dict]:
    path = CONFIG_DIR / "token.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_token(tok: dict) -> None:
    _write_private("token.json", tok)
