"""PowerShell 小工具。"""
import base64


def encode_command(script: str) -> str:
    """-EncodedCommand 要 UTF-16LE 的 base64，免去命令行引号转义。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")
