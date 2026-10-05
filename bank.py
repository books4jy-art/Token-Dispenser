"""Read Korean bank deposit notifications (SMS / app push) forwarded from a phone.

Banks do not offer an API for personal accounts, so the bot learns about a deposit
from the notification your phone gets. A forwarding app (MacroDroid, SMS Forwarder,
Tasker, an iPhone Shortcut…) sends that text to the bot's /deposit webhook, and
`parse_deposit` pulls the amount and the sender's name out of it.

If your bank's wording is not understood, set DEPOSIT_REGEX in .env to a regex with
named groups `amount` and `name`, or have the forwarding app send JSON with the
amount and name already separated.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

CUSTOM_REGEX = os.environ.get("DEPOSIT_REGEX", "").strip()

# Words that appear in notifications but are never the sender's name.
STOPWORDS = {
    "입금", "출금", "잔액", "이체", "입금완료", "출금완료", "체크", "승인", "취소", "웹발신",
    "알림", "계좌", "보통예금", "저축예금", "입출금", "님이", "님", "원", "보냈어요", "보냈습니다",
    "입금했어요", "입금했습니다", "입금되었습니다", "받았어요", "누적", "통장", "내", "모임통장",
    "국민", "신한", "우리", "하나", "농협", "기업", "카카오", "카카오뱅크", "토스", "토스뱅크",
    "케이뱅크", "새마을", "새마을금고", "우체국", "수협", "신협", "부산", "대구", "경남", "광주",
    "전북", "제주", "씨티", "SC제일", "KB", "NH", "IBK", "SH", "MG",
}
BANK_SUFFIXES = ("은행", "뱅크", "금고", "증권", "카드", "저축")

AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d+)"


@dataclass
class Deposit:
    amount: int
    name: str


def _to_int(text: str) -> int:
    return int(text.replace(",", ""))


def _is_name(token: str) -> bool:
    if token in STOPWORDS or token.endswith(BANK_SUFFIXES):
        return False
    # 2–10 Hangul characters, or a short English name (some banks show those)
    return bool(re.fullmatch(r"[가-힣]{2,10}|[A-Za-z][A-Za-z ]{1,19}", token))


def parse_deposit(text: str) -> Deposit | None:
    """Return the deposit in a bank notification, or None if it is not a deposit
    (a withdrawal, a card payment, an ad…) or cannot be read."""
    if CUSTOM_REGEX:
        m = re.search(CUSTOM_REGEX, text, re.S)
        if not m:
            return None
        return Deposit(_to_int(m.group("amount")), m.group("name").strip())

    text = text.replace("[Web발신]", " ").replace("\r", "\n")
    if "출금" in text and "입금" not in text:
        return None
    if not re.search(r"입금|보냈|받았|송금", text):
        return None
    # Drop the balance, dates/times and masked account numbers so they are not
    # mistaken for the amount.
    body = re.sub(r"(잔액|잔고|누적)\s*:?\s*" + AMOUNT + r"\s*원?", " ", text)
    body = re.sub(r"\d{1,2}/\d{1,2}|\d{1,2}:\d{2}|\d{1,2}월\s*\d{1,2}일", " ", body)
    body = re.sub(r"[\d*]+-[\d*-]+|\d*\*+\d*", " ", body)

    # Toss / KakaoBank push style: "홍길동님이 10,000원을 보냈어요"
    m = re.search(r"([가-힣A-Za-z]{2,10})\s*님[이께]?\s*" + AMOUNT + r"\s*원", body)
    if m and _is_name(m.group(1)):
        return Deposit(_to_int(m.group(2)), m.group(1))

    amount_match = re.search(r"입금\s*:?\s*" + AMOUNT + r"\s*원?", body) or re.search(
        AMOUNT + r"\s*원", body
    )
    if amount_match is None:
        amounts = re.findall(r"(?<![\d,])" + AMOUNT + r"(?![\d,])", body)
        amounts = [a for a in amounts if _to_int(a) >= 100]
        if len(amounts) != 1:
            return None
        amount = _to_int(amounts[0])
        rest = body
    else:
        amount = _to_int(amount_match.group(1))
        rest = body[: amount_match.start()] + " " + body[amount_match.end():]

    tokens = re.findall(r"[가-힣]+|[A-Za-z]+", re.sub(r"\[[^\]]*\]", " ", rest))
    names = [t for t in tokens if _is_name(t)]
    if not names or amount <= 0:
        return None
    return Deposit(amount, names[0])
