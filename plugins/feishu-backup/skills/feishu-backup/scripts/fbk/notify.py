"""macOS 系统通知（osascript），带按 key 的每日节流。尽力而为：任何失败都吞掉。"""
import hashlib
import json
import logging
import subprocess
import time
from datetime import datetime
from typing import Optional

from . import config
from .names import SHANGHAI

log = logging.getLogger(__name__)

STATE_FILE = "notify_state.json"


def _quote(s: str) -> str:
    """AppleScript 字符串字面量：转义反斜杠和双引号，换行改空格。"""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % s.replace("\r", " ").replace("\n", " ")


def _load_state() -> dict:
    try:
        d = json.loads((config.CONFIG_DIR / STATE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("通知节流状态写入失败：%s", e)


def notify_if_changed(title: str, message: str, key: str, ids, now: Optional[int] = None) -> bool:
    """只在「失败集合」与上次通知时不同才通知（同时仍受 key 每日一次限制）。
    ids 为空时清掉记录，下次再出现同样的失败也会重新通知。"""
    fp_key = key + "_set"
    ids = sorted(set(ids))
    state = _load_state()
    if not ids:
        if fp_key in state:
            del state[fp_key]
            _save_state(state)
        return False
    fp = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    if state.get(fp_key) == fp:
        log.info("失败集合与上次通知相同，不再通知：%s", key)
        return False
    if not notify(title, message, key=key, now=now):
        return False
    state = _load_state()  # notify 可能刚写过节流日期
    state[fp_key] = fp
    _save_state(state)
    return True


def notify(title: str, message: str, key: Optional[str] = None, now: Optional[int] = None) -> bool:
    """弹系统通知。带 key 时同一 key 每天（北京时间）最多一次。返回是否真的发出。"""
    today = datetime.fromtimestamp(int(now if now is not None else time.time()), SHANGHAI).strftime("%Y-%m-%d")
    state = None
    if key:
        state = _load_state()
        if state.get(key) == today:
            log.info("通知已节流（今天发过）：%s", key)
            return False
    script = "display notification %s with title %s" % (_quote(message), _quote(title))
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        if r.returncode != 0:
            log.warning("系统通知失败：%s", (r.stderr or b"").decode("utf-8", "replace").strip())
            return False
    except Exception as e:  # noqa: BLE001 —— 通知是尽力而为
        log.warning("系统通知失败：%s", e)
        return False
    if key:
        state[key] = today
        _save_state(state)
    return True
