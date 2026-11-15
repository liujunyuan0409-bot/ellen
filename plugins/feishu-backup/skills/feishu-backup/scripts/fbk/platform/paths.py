"""各系统的配置 / 日志 / 程序副本位置。"""
import os
import sys
from pathlib import Path
from typing import Mapping

_NAME = "feishu-backup"


def _win_base(env: Mapping, key: str, home: Path, *fallback: str) -> Path:
    v = env.get(key)
    return Path(v) if v else home.joinpath(*fallback)


def config_dir(sysplat: str = sys.platform, env: Mapping = os.environ, home: Path = None) -> Path:
    home = Path.home() if home is None else home
    if sysplat == "win32":
        return _win_base(env, "APPDATA", home, "AppData", "Roaming") / _NAME
    return home / ".config" / _NAME


def log_dir(sysplat: str = sys.platform, env: Mapping = os.environ, home: Path = None) -> Path:
    home = Path.home() if home is None else home
    if sysplat == "win32":
        return _win_base(env, "LOCALAPPDATA", home, "AppData", "Local") / _NAME / "logs"
    return home / "Library" / "Logs" / _NAME


def app_root(sysplat: str = sys.platform, env: Mapping = os.environ, home: Path = None) -> Path:
    home = Path.home() if home is None else home
    if sysplat == "win32":
        return _win_base(env, "LOCALAPPDATA", home, "AppData", "Local") / _NAME / "app"
    return home / "Library" / "Application Support" / _NAME / "app"
