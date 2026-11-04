"""국내 종목 시세와 개인 관심종목. 별도 API 키 없이 기존 네이버 출처를 쓴다."""

import datetime
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
SEOUL = ZoneInfo("Asia/Seoul")
WATCH_LIMIT = 10
_storage_lock = threading.Lock()


class StockNotFoundError(ValueError):
    pass


class WatchlistFullError(ValueError):
    pass


@dataclass(frozen=True)
class StockQuote:
    code: str
    name: str
    price: Decimal
    change_percent: Decimal
    traded_at: datetime.datetime
    market_status: str


def _valid_code(value: str) -> bool:
    return re.fullmatch(r"[0-9][0-9A-Z]{5}", value) is not None


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_stock_code(query: str) -> str:
    keyword = query.strip()
    if not keyword or len(keyword) > 80:
        raise StockNotFoundError("정확한 종목명이나 6자리 종목코드를 입력해 주세요.")
    if _valid_code(keyword.upper()):
        return keyword.upper()
    params = urllib.parse.urlencode({"q": keyword, "target": "stock"})
    payload = _request_json(f"https://ac.stock.naver.com/ac?{params}")
    matches = {
        str(item["code"]).upper() for item in payload.get("items", [])
        if item.get("nationCode") == "KOR"
        and str(item.get("name", "")).casefold() == keyword.casefold()
        and _valid_code(str(item.get("code", "")).upper())
    }
    if len(matches) != 1:
        raise StockNotFoundError("종목을 찾지 못했어요. 정확한 종목명이나 6자리 코드를 입력해 주세요.")
    return matches.pop()


def fetch_stock_quote(query: str) -> StockQuote:
    code = resolve_stock_code(query)
    try:
        payload = _request_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise StockNotFoundError("종목을 찾지 못했어요.") from error
        raise
    if payload.get("itemCode") != code or not payload.get("stockName"):
        raise StockNotFoundError("종목을 찾지 못했어요.")
    price = Decimal(str(payload["closePrice"]).replace(",", ""))
    change = Decimal(str(payload["fluctuationsRatio"]).replace(",", ""))
    if not price.is_finite() or price <= 0 or not change.is_finite():
        raise ValueError("시세 값이 올바르지 않습니다.")
    traded_at = datetime.datetime.fromisoformat(str(payload["localTradedAt"]).replace("Z", "+00:00"))
    if traded_at.tzinfo is None:
        raise ValueError("체결 시각에 시간대가 없습니다.")
    return StockQuote(code, str(payload["stockName"]), price, change, traded_at.astimezone(SEOUL), str(payload.get("marketStatus", "")))


def format_stock_quote(quote: StockQuote) -> str:
    status = {"OPEN": "장중", "CLOSE": "장마감"}.get(quote.market_status, "조회 시세")
    return (
        f"{quote.name} ({quote.code})\n"
        f"{quote.price:,}원 · 전일 대비 {quote.change_percent:+.2f}%\n"
        f"체결 {quote.traded_at:%m/%d %H:%M} · {status}"
    )


def watchlist_path() -> Path:
    default = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT) / ".watchlist.json"
    return Path(os.getenv("WATCHLIST_PATH") or default)


def _load_all() -> dict:
    path = watchlist_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("관심종목 저장 형식이 올바르지 않습니다.")
    # 잘못된 저장 파일을 빈 목록으로 덮어쓰지 않는다.
    for items in data.values():
        if not isinstance(items, list) or len(items) > WATCH_LIMIT:
            raise ValueError("관심종목 저장 형식이 올바르지 않습니다.")
        for item in items:
            if not isinstance(item, dict) or not _valid_code(str(item.get("code", ""))) or not isinstance(item.get("name"), str):
                raise ValueError("관심종목 저장 형식이 올바르지 않습니다.")
    return data


def _save_all(data: dict) -> None:
    path = watchlist_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def get_watchlist(chat_id: int) -> list[dict]:
    with _storage_lock:
        return _load_all().get(str(chat_id), [])


def add_watch_stock(chat_id: int, quote: StockQuote) -> bool:
    with _storage_lock:
        data = _load_all()
        items = data.setdefault(str(chat_id), [])
        if any(item["code"] == quote.code for item in items):
            return False
        if len(items) >= WATCH_LIMIT:
            raise WatchlistFullError(f"관심종목은 최대 {WATCH_LIMIT}개까지 등록할 수 있어요.")
        items.append({"code": quote.code, "name": quote.name})
        _save_all(data)
        return True


def remove_watch_stock(chat_id: int, query: str) -> dict | None:
    with _storage_lock:
        data = _load_all()
        items = data.get(str(chat_id), [])
        keyword = query.strip().casefold()
        found = next((item for item in items if keyword in (item["code"].casefold(), item["name"].casefold())), None)
        if found is not None:
            items.remove(found)
            _save_all(data)
        return found


def build_watchlist_message(chat_id: int, quiet_empty: bool = False) -> str | None:
    items = get_watchlist(chat_id)
    if not items:
        return None if quiet_empty else "관심종목이 없어요.\n/watch add 삼성전자\n/watch add 000660\n처럼 등록해 주세요."

    def fetch(item):
        try:
            return format_stock_quote(fetch_stock_quote(item["code"]))
        except Exception:
            logger.warning("관심종목 시세 조회 실패: %s", item["code"])
            return f"{item['name']} ({item['code']})\n데이터를 가져오지 못했어요."

    with ThreadPoolExecutor(max_workers=4) as executor:
        sections = list(executor.map(fetch, items))
    return "관심종목 · 네이버 KRX 시세\n체결 시각은 한국 시각입니다.\n\n" + "\n\n".join(sections)
