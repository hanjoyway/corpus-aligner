"""邮件发送（阿里云邮件推送 DirectMail，SMTP over SSL:465）。

要点（踩过的坑，改代码前先看）：
- 端口必须 465 + SSL，**不是** STARTTLS；25 端口被云厂商封死，不可用。
- 发件地址必须是已验证的 SMTP_USER 本身，换成别的会被 DirectMail 拒收；
  只有尖括号前的“显示名”可以自定义。
- **免费额度 200 封/天，是所有项目共用的**。期刊追踪项目已自设 190 封上限，
  所以本项目必须自设一个小上限（MAIL_DAILY_CAP），避免两边加起来超额。
  本项目用量极低（绑定邮箱 / 找回密码各一封），几十封足够。
"""
from __future__ import annotations

import smtplib
import ssl
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import config

_lock = threading.Lock()
_sent = {"day": 0, "count": 0}


def _today() -> int:
    return int(time.time()) // 86400


def quota_left() -> int:
    with _lock:
        if _sent["day"] != _today():
            return config.MAIL_DAILY_CAP
        return max(config.MAIL_DAILY_CAP - _sent["count"], 0)


def _take_quota() -> bool:
    with _lock:
        day = _today()
        if _sent["day"] != day:
            _sent.update(day=day, count=0)
        if _sent["count"] >= config.MAIL_DAILY_CAP:
            return False
        _sent["count"] += 1
        return True


def configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS)


def send(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """发一封纯文本邮件。返回 (是否成功, 出错信息)。绝不抛异常给调用方。"""
    if not configured():
        return False, "邮件功能未配置（服务器 .env 缺 SMTP_* 配置）"
    if not _take_quota():
        return False, "今日邮件发送量已达上限，请稍后再试或联系管理员"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    # 发件人地址必须是已验证的 SMTP_USER；仅显示名可自定义
    msg["From"] = formataddr((str(Header(config.MAIL_FROM_NAME, "utf-8")),
                              config.SMTP_USER))
    msg["To"] = to_addr
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                              context=ctx, timeout=20) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(config.SMTP_USER, [to_addr], msg.as_string())
        return True, ""
    except Exception as e:  # noqa: BLE001 - 邮件失败不该让请求 500
        return False, f"邮件发送失败：{e}"
