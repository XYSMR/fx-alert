"""Email 通知：組信件內容、透過 Gmail SMTP 寄出。

設定全部從環境變數讀取（GitHub Actions 上對應 Secrets），不寫在程式或 config.yaml 裡：
    SMTP_USER       寄件的 Gmail 帳號，例如 you@gmail.com
    SMTP_PASSWORD   Gmail「應用程式密碼」（16 碼，不是登入密碼）
    MAIL_TO         收件人，多個用逗號分隔
    SMTP_HOST       選填，預設 smtp.gmail.com
    SMTP_PORT       選填，預設 465（SSL）

本機手動寄一封測試信：
    python -m fx_alert.notifier --test
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from email.message import EmailMessage
from typing import Protocol

from fx_alert.rules import Rule
from fx_alert.unsubscribe import UnsubscribeLinks

BANK_LABEL = "兆豐銀行 現金賣出"
RATE_PAGE = (
    "https://www.megabank.com.tw/personal-banking/foreign-exchange/exchange-rate/exchange-rate"
)
TIMEOUT_SECONDS = 20


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_emails(text: str) -> str:
    """把文字裡的 email 換成 ***。錯誤訊息會進公開的 GitHub Actions log。"""
    return _EMAIL_RE.sub("***", text)


class ConfigError(RuntimeError):
    """必要的環境變數沒設定。"""


class NotifyError(RuntimeError):
    """寄信失敗。呼叫端遇到這個錯誤時「不要」更新狀態，下次執行會再試。"""


@dataclass(frozen=True)
class EmailConfig:
    user: str
    password: str = field(repr=False)  # 不出現在 repr / log 裡
    to: tuple[str, ...]
    host: str = "smtp.gmail.com"
    port: int = 465

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, require_to: bool = True
    ) -> EmailConfig:
        """require_to=False：多人版，收件人來自資料庫，不需要 MAIL_TO。"""
        env = os.environ if env is None else env
        required = (
            ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO")
            if require_to
            else ("SMTP_USER", "SMTP_PASSWORD")
        )
        missing = [k for k in required if not env.get(k, "").strip()]
        if missing:
            raise ConfigError(f"缺少環境變數：{', '.join(missing)}")
        to = tuple(a.strip() for a in env.get("MAIL_TO", "").split(",") if a.strip())
        try:
            port = int(env.get("SMTP_PORT", "465"))
        except ValueError as exc:
            raise ConfigError("SMTP_PORT 必須是數字") from exc
        return cls(
            user=env["SMTP_USER"].strip(),
            # Google 顯示應用程式密碼時會用空格分成 4 組，這裡把空格去掉
            password=env["SMTP_PASSWORD"].replace(" ", ""),
            to=to,
            host=env.get("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
            port=port,
        )


# ---------- 信件內容 ----------


def build_alert_message(
    cfg: EmailConfig,
    rule: Rule,
    rate: Decimal,
    quoted_at: datetime | None,
    *,
    to: Sequence[str] | None = None,
    unsubscribe: UnsubscribeLinks | None = None,
) -> EmailMessage:
    """到價通知信。to 沒給就寄給 cfg.to（單人版的 MAIL_TO）。

    unsubscribe：多人版才有。加上 List-Unsubscribe 標頭（RFC 8058 一鍵退訂）和內文的退訂連結。
    """
    when = quoted_at.strftime("%Y-%m-%d %H:%M") if quoted_at else "（銀行未提供）"
    msg = EmailMessage()
    msg["Subject"] = f"[FX Alert] {rule.currency} 到價：{rate} ≤ 目標 {rule.target}"
    msg["From"] = cfg.user
    msg["To"] = ", ".join(cfg.to if to is None else to)
    msg.set_content(
        "\n".join(
            [
                f"{rule.currency} 的{BANK_LABEL}價已達到你設定的目標。",
                "",
                f"  目前匯率：{rate}",
                f"  目標匯率：≤ {rule.target}",
                f"  牌告時間：{when}",
                "",
                "這次到價只會通知一次；",
                f"匯率回升到 > {rule.rearm_above} 之後再跌回目標，才會再通知。",
                "",
                f"兆豐銀行牌告匯率：{RATE_PAGE}",
                "",
                "— fx-alert（GitHub Actions 自動寄出）",
                *(
                    ["", f"不想再收到到價通知？取消訂閱：{unsubscribe.page_url}"]
                    if unsubscribe
                    else []
                ),
            ]
        )
    )
    if unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsubscribe.one_click_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return msg


def build_test_message(cfg: EmailConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "[FX Alert] 測試信"
    msg["From"] = cfg.user
    msg["To"] = ", ".join(cfg.to)
    msg.set_content("收到這封信，代表 fx-alert 的 Gmail SMTP 設定正確。\n\n— fx-alert")
    return msg


# ---------- 寄送 ----------


class Notifier(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class SmtpNotifier:
    def __init__(self, cfg: EmailConfig, smtp_factory=smtplib.SMTP_SSL):
        self.cfg = cfg
        self._smtp_factory = smtp_factory  # 測試時換成假的

    def send(self, msg: EmailMessage) -> None:
        try:
            with self._smtp_factory(
                self.cfg.host,
                self.cfg.port,
                timeout=TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            ) as smtp:
                smtp.login(self.cfg.user, self.cfg.password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyError(
                "Gmail 登入失敗：請確認 SMTP_USER，以及 SMTP_PASSWORD 是「應用程式密碼」"
                "（帳號需開啟兩步驟驗證）"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            # 例如 SMTPRecipientsRefused 的訊息會包含收件人地址
            raise NotifyError(redact_emails(f"寄信失敗：{type(exc).__name__}: {exc}")) from exc


class DryRunNotifier:
    """不真的寄信，只印出來並記錄（本機 --dry-run 和測試用）。"""

    def __init__(self, out=None):
        self.sent: list[EmailMessage] = []
        self._out = out

    def send(self, msg: EmailMessage) -> None:
        self.sent.append(msg)
        out = self._out or sys.stdout
        # 不印出收件人地址：多人版的 dry-run 也可能在公開的 Actions log 裡執行
        count = len([a for a in str(msg["To"] or "").split(",") if a.strip()])
        print(f"[dry-run] 不寄出：收件人 {count} 位，Subject={msg['Subject']}", file=out)


def _main(argv: list[str]) -> int:
    if argv != ["--test"]:
        print("用法：python -m fx_alert.notifier --test", file=sys.stderr)
        return 2
    try:
        cfg = EmailConfig.from_env()
        SmtpNotifier(cfg).send(build_test_message(cfg))
    except (ConfigError, NotifyError) as exc:
        print(f"失敗：{exc}", file=sys.stderr)
        return 1
    print(f"已寄出測試信給 {', '.join(cfg.to)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
