"""종목명(또는 종목코드)으로 일봉 캔들 차트 이미지를 만든다."""

import datetime
import io
import logging
import sys
import threading
from dataclasses import dataclass

# 서버에는 화면이 없으므로, 창을 띄우지 않고 이미지로만 그리는 백엔드를 쓴다.
import matplotlib

matplotlib.use("Agg")

import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import mplfinance as mpf

logger = logging.getLogger(__name__)

# 약 6개월치 거래일(120일 안팎)을 보여 준다.
CHART_LOOKBACK_DAYS = 180
# 종목 목록은 자주 바뀌지 않으므로 하루 한 번만 새로 받는다.
_listing_cache: dict = {"date": None, "name_to_code": {}, "code_to_name": {}}
# pyplot은 여러 스레드에서 동시에 그리면 그림이 섞일 수 있어 한 번에 하나만 그린다.
_draw_lock = threading.Lock()

# 국내 관례대로 상승은 빨강, 하락은 파랑.
_KOREAN_COLORS = mpf.make_marketcolors(up="r", down="b", inherit=True)
_CHART_STYLE = mpf.make_mpf_style(marketcolors=_KOREAN_COLORS, gridstyle=":")


class StockNotFoundError(Exception):
    """종목명·코드가 KRX 목록에 없을 때. 조회 실패와 다른 안내를 보내기 위해 따로 둔다."""


@dataclass
class StockChart:
    code: str
    name: str
    image: bytes  # PNG
    last_close: float
    last_date: datetime.date


def _load_listing() -> tuple[dict[str, str], dict[str, str]]:
    """KRX 종목 목록에서 이름->코드, 코드->이름 사전을 만든다. 하루 동안 재사용한다."""
    today = datetime.date.today()
    if _listing_cache["date"] != today:
        listing = fdr.StockListing("KRX")
        # 영문 종목명(NAVER 등)은 대소문자 상관없이 찾도록 대문자로 맞춘다.
        _listing_cache["name_to_code"] = {
            str(name).upper(): str(code) for name, code in zip(listing["Name"], listing["Code"])
        }
        _listing_cache["code_to_name"] = {
            str(code): str(name) for name, code in zip(listing["Name"], listing["Code"])
        }
        _listing_cache["date"] = today
    return _listing_cache["name_to_code"], _listing_cache["code_to_name"]


def resolve_stock(query: str) -> tuple[str, str] | None:
    """'삼성전자' 또는 '005930'을 (코드, 이름)으로 바꾼다. 못 찾으면 None."""
    keyword = query.strip()
    if not keyword:
        return None
    name_to_code, code_to_name = _load_listing()
    if keyword.isdigit() and len(keyword) == 6:
        return keyword, code_to_name.get(keyword, keyword)
    code = name_to_code.get(keyword.upper())
    if code is None:
        return None
    return code, code_to_name.get(code, keyword)


def _draw_candle_png(prices, code: str) -> bytes:
    """캔들 + 거래량 차트를 PNG 바이트로 그린다. 파일로 저장하지 않는다."""
    buffer = io.BytesIO()
    with _draw_lock:
        figure, _ = mpf.plot(
            prices,
            type="candle",
            volume=True,
            style=_CHART_STYLE,
            # 서버에 한글 글꼴이 없을 수 있어 그림 안 글자는 영어·숫자만 쓴다.
            title=f"{code} Daily",
            ylabel="Price (KRW)",
            ylabel_lower="Volume",
            returnfig=True,
            figsize=(10, 6),
        )
        try:
            figure.savefig(buffer, format="png", dpi=100, bbox_inches="tight")
        finally:
            # 닫지 않으면 요청마다 그림이 메모리에 쌓인다.
            plt.close(figure)
    return buffer.getvalue()


def build_stock_chart(query: str) -> StockChart | None:
    """종목을 찾아 차트를 만든다. 종목이 없으면 StockNotFoundError, 조회·그리기가 실패하면 None."""
    try:
        resolved = resolve_stock(query)
        if resolved is None:
            raise StockNotFoundError(query)
        code, name = resolved

        start_date = datetime.date.today() - datetime.timedelta(days=CHART_LOOKBACK_DAYS)
        prices = fdr.DataReader(code, start_date)
        # 거래정지일처럼 값이 빈 행은 캔들을 그릴 수 없어 뺀다.
        prices = prices.dropna(subset=["Open", "High", "Low", "Close"])
        if prices.empty:
            logger.warning("%s(%s) 시세 데이터가 비어 있습니다.", name, code)
            return None

        return StockChart(
            code=code,
            name=name,
            image=_draw_candle_png(prices, code),
            last_close=float(prices["Close"].iloc[-1]),
            last_date=prices.index[-1].date(),
        )
    except StockNotFoundError:
        raise
    except Exception:
        # 네트워크, 데이터 형식, 그리기 오류가 나도 봇은 계속 살아 있어야 한다.
        logger.exception("차트 생성에 실패했습니다. query=%s", query)
        return None


if __name__ == "__main__":
    # 터미널 확인용: python -m bot.chart 삼성전자  (chart_test.png로 저장)
    logging.basicConfig(level=logging.INFO)
    keyword = " ".join(sys.argv[1:]).strip() or "삼성전자"
    try:
        chart = build_stock_chart(keyword)
    except StockNotFoundError:
        print(f"종목을 찾지 못했어요: {keyword}")
        sys.exit(1)
    if chart is None:
        print("데이터를 가져오지 못했어요")
    else:
        with open("chart_test.png", "wb") as file:
            file.write(chart.image)
        print(f"{chart.name}({chart.code}) 종가 {chart.last_close:,.0f} ({chart.last_date}) -> chart_test.png")
