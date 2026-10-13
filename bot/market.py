"""국내 주요 지수(코스피, 코스닥)와 원/달러 환율 조회."""

import datetime
import logging
import unicodedata
from dataclasses import dataclass

import FinanceDataReader as fdr

logger = logging.getLogger(__name__)

# 지수 이름 -> FinanceDataReader 기호.
# 기본 기호(KS11 등)는 KRX 출처라 로그인 없이는 빈 결과가 와서 네이버 출처를 쓴다.
INDEX_SYMBOLS = {
    "코스피": "NAVER:KOSPI",
    "코스닥": "NAVER:KOSDAQ",
}

# 원/달러는 네이버 지수와 달리 Change 열이 없다. 전일 종가로 등락률을 계산한다.
FX_NAME = "원/달러"
FX_SYMBOL = "USD/KRW"

# 긴 연휴(추석 등)에도 최소 하루치 데이터가 남도록 넉넉히 조회한다.
LOOKBACK_DAYS = 14

# 조회가 전부 실패했을 때 사용자에게 보내는 문장. README 문구를 그대로 쓴다.
FETCH_FAILED_MESSAGE = "데이터를 가져오지 못했어요"

# 한글은 전각이라 글자 수와 보이는 폭이 다르다. 표 칸은 이 폭에 맞춘다.
NAME_COLUMN_WIDTH = 10
PRICE_COLUMN_WIDTH = 12


@dataclass
class IndexQuote:
    name: str
    price: float
    change_percent: float  # 전일 대비 등락률(%), 예: -1.23
    date: datetime.date  # 데이터 기준일


def fetch_index_quote(name: str, symbol: str) -> IndexQuote | None:
    """지수 하나의 최신 값을 가져온다. 실패하면 로그를 남기고 None을 돌려준다."""
    start_date = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    try:
        prices = fdr.DataReader(symbol, start_date)
        if prices.empty:
            logger.warning("%s(%s) 데이터가 비어 있습니다.", name, symbol)
            return None
        latest = prices.iloc[-1]
        return IndexQuote(
            name=name,
            price=float(latest["Close"]),
            # Change는 비율(-0.0123)이라 100을 곱해 퍼센트로 바꾼다.
            change_percent=float(latest["Change"]) * 100,
            date=prices.index[-1].date(),
        )
    except Exception:
        # 외부 데이터 조회 실패가 봇 전체를 멈추지 않도록 여기서 막는다.
        logger.exception("%s(%s) 조회에 실패했습니다.", name, symbol)
        return None


def fetch_indices() -> dict[str, IndexQuote | None]:
    """코스피, 코스닥을 조회한다. 실패한 지수는 값이 None이다."""
    return {
        name: fetch_index_quote(name, symbol)
        for name, symbol in INDEX_SYMBOLS.items()
    }


def fetch_market_quotes() -> list[tuple[str, IndexQuote | None]]:
    """코스피, 코스닥, 원/달러를 순서대로 조회한다. 실패한 항목은 None이다."""
    rows = [
        (name, fetch_index_quote(name, symbol))
        for name, symbol in INDEX_SYMBOLS.items()
    ]
    rows.append((FX_NAME, fetch_usdkrw()))
    return rows


def _display_width(text: str) -> int:
    """터미널과 텔레그램 고정폭 글꼴에서 차지하는 칸 수."""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + (" " * max(width - _display_width(text), 0))


def format_market_message(rows: list[tuple[str, IndexQuote | None]]) -> str:
    """지수명, 현재가, 등락률을 한 메시지의 표로 만든다."""
    if not rows or all(quote is None for _, quote in rows):
        return FETCH_FAILED_MESSAGE

    lines = ["시황"]
    dates = {quote.date for _, quote in rows if quote is not None}
    if len(dates) == 1:
        lines.append(f"기준일 {next(iter(dates))}")
    lines.append(f"{_pad('구분', NAME_COLUMN_WIDTH)}{_pad('현재가', PRICE_COLUMN_WIDTH)}등락률")
    for name, quote in rows:
        if quote is None:
            lines.append(f"{_pad(name, NAME_COLUMN_WIDTH)}{FETCH_FAILED_MESSAGE}")
            continue
        price = f"{quote.price:,.2f}"
        change = f"{quote.change_percent:+.2f}%"
        # 기준일이 서로 다르면 어느 숫자인지 알 수 있게 날짜를 붙인다.
        date_suffix = f"  ({quote.date})" if len(dates) > 1 else ""
        lines.append(
            f"{_pad(name, NAME_COLUMN_WIDTH)}{_pad(price, PRICE_COLUMN_WIDTH)}{change}{date_suffix}"
        )
    return "\n".join(lines)


def fetch_usdkrw() -> IndexQuote | None:
    """원/달러 환율을 가져온다. 실패하면 로그를 남기고 None을 돌려준다."""
    start_date = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    try:
        prices = fdr.DataReader(FX_SYMBOL, start_date)
        # 휴장일 행은 종가가 비어 있으므로, 값이 있는 날만 남긴다.
        valid = prices.dropna(subset=["Close"])
        if len(valid) < 2:
            logger.warning("%s(%s) 비교할 데이터가 부족합니다.", FX_NAME, FX_SYMBOL)
            return None
        price = float(valid.iloc[-1]["Close"])
        previous_close = float(valid.iloc[-2]["Close"])
        if previous_close == 0:
            logger.warning("%s(%s) 전일 종가가 0입니다.", FX_NAME, FX_SYMBOL)
            return None
        return IndexQuote(
            name=FX_NAME,
            price=price,
            change_percent=(price - previous_close) / previous_close * 100,
            date=valid.index[-1].date(),
        )
    except Exception:
        logger.exception("%s(%s) 조회에 실패했습니다.", FX_NAME, FX_SYMBOL)
        return None


if __name__ == "__main__":
    # 터미널 확인용: python -m bot.market
    logging.basicConfig(level=logging.INFO)
    print(format_market_message(fetch_market_quotes()))
