"""把失败原因翻成用户看得懂的话。

飞书自身的限制（文件太大下载不了、文档太大导出不了）程序绕不过去，原因前面统一加 LIMIT_TAG，
报告里据此和真正的失败分开列，提示用户手动处理。"""
from .client import ApiError, RATE_LIMIT_CODES

LIMIT_TAG = "【飞书限制】"

ATTACHMENT_TOO_LARGE = 234037

# 飞书导出任务的 job_status（官方没有公开含义表，以下按实际返回整理）
_EXPORT_STATUS = {
    107: (True, "文档太大，飞书导出不了（job_status=107）。可以在飞书里把它另存为 PDF，手动放进这个目录"),
    108: (False, "飞书导出超时（job_status=108），下轮会自动重试"),
    3: (False, "飞书导出失败，没有给出原因（job_status=3，类型：%s）。下轮会自动重试，一直失败就在飞书里打开看看能不能手动导出"),
}


def _type_label(item_type) -> str:
    from .wizard import TYPE_LABEL  # 延迟导入：wizard 依赖较多，避免循环
    if not item_type:
        return "未知"
    return TYPE_LABEL.get(item_type, item_type)


def explain(e: BaseException, item_type=None) -> str:
    """失败原因。飞书限制以 LIMIT_TAG 开头；不认识的错误原样返回（附上对象类型，方便排查）。"""
    status = getattr(e, "job_status", None)
    if status in _EXPORT_STATUS:
        limited, text = _EXPORT_STATUS[status]
        if "%s" in text:
            text = text % _type_label(item_type)
        return (LIMIT_TAG if limited else "") + text
    if isinstance(e, ApiError):
        if e.code == ATTACHMENT_TOO_LARGE:
            return LIMIT_TAG + "文件太大，飞书不允许通过接口下载（code=234037），请用飞书客户端手动下载"
        if e.code in RATE_LIMIT_CODES or e.code == 429:
            return "飞书限流，重试多次仍没成功（code=%s），下轮会自动重试" % e.code
    if status is not None:  # 其他导出状态码：带上类型
        return "%s（类型：%s）" % (e, _type_label(item_type))
    return str(e)


def is_limit(reason: str) -> bool:
    return LIMIT_TAG in (reason or "")
