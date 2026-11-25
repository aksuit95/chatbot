"""TrendForce 공개 DRAM 현물가격과 관련 보도자료를 확인한다."""

import datetime
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SEOUL = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parent.parent
PRICE_URL = "https://www.trendforce.com/price/dram/dram_spot"
NEWS_URL = "https://www.trendforce.com/presscenter/news/"
INSIGHTS_URL = "https://www.trendforce.com/news/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
PRODUCTS = ("DDR5 16Gb (2Gx8) 4800/5600", "DDR4 16Gb (2Gx8) 3200", "DDR4 8Gb (1Gx8) 3200")


def _get(url: str, **kwargs) -> BeautifulSoup:
    response = requests.get(url, headers=HEADERS, timeout=15, **kwargs)
    response.raise_for_status()
    response.encoding = "utf-8"
    return BeautifulSoup(response.text, "html.parser")


def fetch_prices() -> dict:
    soup = _get(PRICE_URL)
    text = soup.get_text(" ", strip=True)
    stamp = re.search(r"Last Update\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\(GMT\+8\)", text)
    if not stamp:
        raise ValueError("DRAM 가격 기준시각을 찾지 못했습니다.")
    timestamp = datetime.datetime.strptime(" ".join(stamp.groups()), "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    now = datetime.datetime.now(datetime.timezone.utc)
    if timestamp > now + datetime.timedelta(minutes=10) or now - timestamp > datetime.timedelta(days=7):
        raise ValueError("DRAM 가격 기준시각이 오래됐거나 올바르지 않습니다.")
    tables = soup.select("table.price-table")
    if not tables:
        raise ValueError("DRAM 가격표를 찾지 못했습니다.")
    table = tables[0]
    headers = [th.get_text(" ", strip=True) for th in table.select("thead th")]
    if headers[:7] != ["Item", "Daily High", "Daily Low", "Session High", "Session Low", "Session Average", "Session Change"]:
        raise ValueError("DRAM 현물가격표 형식이 변경됐습니다.")
    prices = {}
    for row in table.select("tbody tr"):
        cells = row.select("td")
        if len(cells) < 7:
            raise ValueError("DRAM 가격 항목 형식이 변경됐습니다.")
        name = cells[0].get_text(" ", strip=True)
        if name in PRODUCTS:
            value = cells[5].get_text(" ", strip=True).replace(",", "")
            try:
                price = Decimal(value)
            except InvalidOperation as error:
                raise ValueError("DRAM 가격을 읽지 못했습니다.") from error
            if not price.is_finite() or price <= 0:
                raise ValueError("DRAM 가격이 올바르지 않습니다.")
            prices[name] = str(price)
    if set(prices) != set(PRODUCTS):
        raise ValueError("대상 DRAM 제품의 가격이 누락됐습니다.")
    return {"prices": prices, "as_of": timestamp.astimezone(SEOUL).isoformat()}


def fetch_articles(today: datetime.date) -> list[dict]:
    articles = []
    seen = set()
    for page_type in ("press", "news"):
      for page in range(1, 4):
        soup = _get(NEWS_URL, params={"page": page}) if page_type == "press" else _get(INSIGHTS_URL if page == 1 else f"{INSIGHTS_URL}page/{page}/")
        links = soup.select("a.title-link[href]") if page_type == "press" else soup.select("a[href]")
        valid_count = 0
        for link in links:
            url = urljoin(INSIGHTS_URL, link["href"])
            parsed = urlparse(url)
            match = re.fullmatch(r"/presscenter/news/(\d{8})-\d+\.html", parsed.path) if page_type == "press" else re.fullmatch(r"/news/(\d{4})/(\d{2})/(\d{2})/[^/]+/?", parsed.path)
            if parsed.hostname != "www.trendforce.com" or not match:
                continue
            title = link.get_text(" ", strip=True)
            if len(title) < 25:
                continue
            valid_count += 1
            published = datetime.datetime.strptime("".join(match.groups()), "%Y%m%d").date()
            if not today - datetime.timedelta(days=7) <= published <= today:
                continue
            lower = title.casefold()
            specific = re.search(r"\b(?:dram|ddr[3456]?|lpddr\w*|hbm\w*)\b", lower)
            memory = (re.search(r"\bmemory\b", lower) and not re.search(r"\b(?:nand|flash|ssd)\b", lower)) or "memory spot price update" in lower
            if (specific or memory) and url not in seen:
                articles.append({"url": url, "title": title, "date": published.isoformat()})
                seen.add(url)
        if not valid_count:
            raise ValueError("TrendForce 기사 목록을 읽지 못했습니다.")
    return articles


def state_path(chat_id: int) -> Path:
    root = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT)
    return root / f".dram-monitor-{chat_id}.json"


def load_state(chat_id: int) -> dict:
    path = state_path(chat_id)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("prices", {}), dict) or not isinstance(data.get("seen_urls", []), list):
        raise ValueError("DRAM 확인 기록을 읽지 못했습니다.")
    try:
        valid = all(Decimal(value).is_finite() and Decimal(value) > 0 for value in data.get("prices", {}).values())
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("DRAM 가격 기록이 올바르지 않습니다.") from error
    if not valid:
        raise ValueError("DRAM 가격 기록이 올바르지 않습니다.")
    return data


def save_state(chat_id: int, data: dict) -> None:
    path = state_path(chat_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def make_update(day: datetime.date, previous: dict, prices: dict | None, articles: list[dict] | None) -> tuple[str | None, dict]:
    updated = dict(previous)
    lines = ["DRAM 동향 · TrendForce"]
    if prices is not None:
        old_prices = previous.get("prices", {})
        changed = [(name, Decimal(value), Decimal(old_prices[name]) if name in old_prices else None)
                   for name, value in prices["prices"].items()
                   if name not in old_prices or Decimal(value) != Decimal(old_prices[name])]
        if changed:
            initial = not old_prices
            lines.append("현물가격 최초 기준" if initial else "현물가격 변화 · 직전 확인 대비")
            as_of = datetime.datetime.fromisoformat(prices["as_of"]).astimezone(SEOUL)
            lines.append(f"기준 {as_of:%m/%d %H:%M} 한국시간 · 회차 평균")
            for name, value, old in changed:
                delta = "" if old is None else f" ({(value - old) / old * 100:+.2f}%)"
                lines.append(f"{name}: US${value}{delta}")
            lines.append(PRICE_URL)
        updated["prices"] = prices["prices"]
    if articles is not None:
        known = set(previous.get("seen_urls", []))
        fresh = [article for article in articles if article["url"] not in known]
        if fresh:
            lines.append("관련 보도자료 · 원문 제목")
            for article in fresh:
                lines.append(f"{article['date']} {article['title']}\n{article['url']}")
        updated["seen_urls"] = list(dict.fromkeys([article["url"] for article in articles] + previous.get("seen_urls", [])))[:200]
    missing = [name for name, value in (("현물가격", prices), ("기사", articles)) if value is None]
    if missing and previous.get("failure_date") != day.isoformat():
        lines.append(f"조회 실패: {', '.join(missing)}. 다음 확인 때 다시 시도합니다.")
        updated["failure_date"] = day.isoformat()
    if len(lines) == 1:
        return None, updated
    lines.append("현물 시세와 계약 가격은 다르며, 제목만으로 시장 영향은 확정할 수 없습니다.")
    return "\n\n".join(lines), updated
