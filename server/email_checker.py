"""
邮箱检查器 —— 服务端 IMAP 拉取邮件摘要

架构：
  LLM 调 check_emails 工具 → 本模块 IMAP 拉取 → 返回摘要 → LLM 组织语言播报

使用 QQ 邮箱 IMAP，不需要 PC Agent / 浏览器 / Outlook。
"""
from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from email.header import decode_header

from config import EMAIL_HOST, EMAIL_USER, EMAIL_PASS


@dataclass
class EmailSummary:
    sender: str
    subject: str
    body_preview: str  # 前 200 字
    date_str: str
    has_attachment: bool


def _decode_mime(text: str | bytes | None) -> str:
    """解码 MIME 编码的邮件头（=?UTF-8?B?...?= 等）。"""
    if not text:
        return ""
    if isinstance(text, str):
        return text
    parts = decode_header(text)
    result: list[str] = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            try:
                result.append(payload.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(payload.decode("utf-8", errors="replace"))
        else:
            result.append(str(payload))
    return "".join(result)


def _extract_body(msg: email.message.Message) -> str:
    """提取邮件正文（纯文本优先，降级到 HTML 摘取）。"""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    # 去掉过长引用
                    body = re.sub(r"^>.*$", "", body, flags=re.MULTILINE)
                    body = re.sub(r"\n{3,}", "\n\n", body)
                    return body.strip()
        # 无纯文本 → 摘 HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode("utf-8", errors="replace")
                    # 简单去标签
                    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.I)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text)
                    return text.strip()[:500]
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace").strip()
        return ""


def _preview(text: str, max_chars: int = 200) -> str:
    """截取前 N 字的可读预览。"""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def check_emails_today(max_count: int = 10) -> list[EmailSummary]:
    """
    拉取今天收到的邮件摘要。

    返回 EmailSummary 列表，失败抛异常（调用方处理）。
    """
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("邮箱未配置，请在 .env 中设置 EMAIL_USER 和 EMAIL_PASS")

    results: list[EmailSummary] = []

    # IMAP 连接（SSL）
    imap = imaplib.IMAP4_SSL(EMAIL_HOST, timeout=10)
    try:
        imap.login(EMAIL_USER, EMAIL_PASS)

        # 选择收件箱（163/QQ 都叫 INBOX）
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            # 163 有时需要重新登录后再 select
            imap.select("INBOX", readonly=True)

        # 搜索今天的邮件（163 不支持 SINCE，改用 ALL + 手动过滤）
        status, msg_ids = imap.search(None, "ALL")
        if status != "OK" or not msg_ids[0]:
            return results

        ids = msg_ids[0].split()
        # 取最近 N 封（倒序），fetch 时再过滤日期
        ids = ids[-max_count * 3:]  # 多取一些，客户端过滤日期

        for msg_id in reversed(ids):
            status, data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw = data[0][1]
            msg = email.message_from_bytes(raw)

            # 发件人
            sender = _decode_mime(msg.get("From", ""))

            # 主题
            subject = _decode_mime(msg.get("Subject", ""))

            # 日期 + 只保留今天的
            date_str = msg.get("Date", "")
            try:
                # 解析邮件 Date 头，过滤非今天邮件
                from email.utils import parsedate_to_datetime
                mail_dt = parsedate_to_datetime(date_str)
                if mail_dt.date() != today:
                    continue
            except Exception:
                pass  # 解析失败不跳过，可能仍是今天的

            # 附件
            has_attachment = False
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get("Content-Disposition") and "attachment" in part.get("Content-Disposition"):
                        has_attachment = True
                        break

            # 正文
            body = _extract_body(msg)

            results.append(EmailSummary(
                sender=sender,
                subject=subject,
                body_preview=_preview(body),
                date_str=date_str,
                has_attachment=has_attachment,
            ))

            if len(results) >= max_count:
                break

    finally:
        try:
            imap.close()
            imap.logout()
        except Exception:
            pass

    return results


def format_email_summary(emails: list[EmailSummary]) -> str:
    """把邮件摘要列表格式化为给 LLM 的自然语言文本。"""
    if not emails:
        return "今天没有新邮件。"

    lines = [f"今天共 {len(emails)} 封邮件："]
    for i, em in enumerate(emails, 1):
        sender_short = em.sender.split("<")[0].strip().rstrip()
        att = " [有附件]" if em.has_attachment else ""
        lines.append(
            f"{i}. 发件人：{sender_short}，主题：{em.subject}{att}，"
            f"内容摘要：{em.body_preview}"
        )
    return "\n".join(lines)
