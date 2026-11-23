"""运行报告：_报告/YYYY-MM-DD_HHMM_备份日志.md（每次运行一份）。"""
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from .explain import is_limit
from .names import SHANGHAI
from .sync import Outcome

SECTIONS = [
    (Outcome.NEW, "新增"),
    (Outcome.UPDATED, "更新"),
    (Outcome.MOVED, "移动"),
    (Outcome.DUPLICATE, "去重"),
    (Outcome.SKIPPED, "跳过"),
    (Outcome.FAILED, "失败"),
]
_LABEL = dict(SECTIONS + [(Outcome.UNCHANGED, "未变化")])
LIMIT_LABEL = "其中飞书限制（需手动处理）"


def _fmt_time(ts) -> str:
    return datetime.fromtimestamp(int(ts), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(sec) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d 小时 %d 分 %d 秒" % (h, m, s)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


class RunReport:
    """线程安全的运行记录。entries 只记 UNCHANGED 以外的结果。"""

    def __init__(self):
        self.counts = Counter()  # type: Counter
        self.entries = []  # type: List[Tuple[Outcome, str, str]]
        self.errors = []  # type: List[str]  遍历/扫描层面的错误
        self.notes = []  # type: List[str]
        self._lock = threading.Lock()

    def add(self, outcome: Outcome, path: str, reason: str = "") -> None:
        with self._lock:
            self.counts[outcome] += 1
            if outcome != Outcome.UNCHANGED:
                self.entries.append((outcome, path, reason))

    def error(self, msg: str) -> None:
        with self._lock:
            self.errors.append(msg)

    def note(self, msg: str) -> None:
        with self._lock:
            self.notes.append(msg)

    def limited(self) -> List[Tuple[str, str]]:
        """失败里属于飞书限制（程序绕不过去、要手动处理）的项。"""
        return [(p, r) for o, p, r in self.entries if o == Outcome.FAILED and is_limit(r)]

    def summary(self) -> dict:
        d = {_LABEL[o]: n for o, n in self.counts.items() if n}
        n_limit = len(self.limited())
        if n_limit:
            d[LIMIT_LABEL] = n_limit
        d["已归档的历史版本"] = self.counts[Outcome.UPDATED]
        d["遍历错误"] = len(self.errors)
        return d

    def write_markdown(self, path: Path, started, finished) -> Path:
        path = Path(path)
        lines = ["# 飞书备份日志 %s" % datetime.fromtimestamp(int(started), SHANGHAI).strftime("%Y-%m-%d"), "",
                 "- 开始：%s" % _fmt_time(started),
                 "- 结束：%s" % _fmt_time(finished),
                 "- 耗时：%s" % _fmt_dur(int(finished) - int(started)),
                 "- " + "，".join("%s %d" % (_LABEL[o], self.counts[o])
                                  for o in [o for o, _ in SECTIONS] + [Outcome.UNCHANGED]),
                 "- 已归档的历史版本 %d" % self.counts[Outcome.UPDATED]]
        limited = self.limited()
        if limited:
            lines.append("- 失败里有 %d 项是飞书自身的限制，程序没法备份，需要手动处理，见「需手动处理」一节"
                         % len(limited))
        for n in self.notes:
            lines.append("- %s" % n)
        lines.append("")
        for outcome, title in SECTIONS:
            rows = [(p, r) for o, p, r in self.entries if o == outcome
                    and not (outcome == Outcome.FAILED and is_limit(r))]
            lines += ["## %s" % title, ""]
            if not rows:
                lines += ["无", ""]
                continue
            for p, r in rows:
                lines.append("- %s%s" % (p, "：%s" % r if r else ""))
            lines.append("")
        if limited:
            lines += ["## 需手动处理（飞书限制）", "",
                      "以下文件因为飞书自身的限制没法通过接口备份，每轮都会失败。"
                      "需要的话请在飞书客户端里手动下载或另存为 PDF，放进对应目录。", ""]
            lines += ["- %s%s" % (p, "：%s" % r if r else "") for p, r in limited]
            lines.append("")
        lines += ["## 已归档的历史版本", ""]
        upd = [p for o, p, _r in self.entries if o == Outcome.UPDATED]
        if upd:
            lines.append("以下文件的上一版已移入同目录的 `历史版本/`：")
            lines.append("")
            lines += ["- %s" % p for p in upd]
        else:
            lines.append("无")
        lines.append("")
        if self.errors:
            lines += ["## 遍历与扫描错误", "", "以下位置本次没有读全，下次运行会重试；本次不标记云端已删除的文件。", ""]
            lines += ["- %s" % e for e in self.errors]
            lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "\n".join(lines).encode("utf-8")
        # 文件名带时分，每次运行一份；同一分钟内再跑（例如手动连跑两次）不覆盖上一份，
        # 改名为 …_HHMM（2）_备份日志.md（「（」排在「_」之后，按文件名排序时新的一份仍在最后）
        stem, sep, tail = path.name.rpartition("_")
        n = 1
        while True:
            cand = path if n == 1 else path.with_name("%s（%d）%s%s" % (stem, n, sep, tail))
            try:
                with open(cand, "xb") as f:
                    f.write(data)
                return cand
            except FileExistsError:
                n += 1
