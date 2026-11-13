"""공개 공모주 락업 일정에서 날짜별 해제 예정 항목을 찾는다."""

import datetime
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://jangjeon.kr/ipo/lockup/"
MAX_SOURCE_AGE_DAYS = 7


@dataclass(frozen=True)
class LockupEvent:
    name: str
    category: str
    amount: str
    url: str


def fetch_lockups(day: datetime.date) -> tuple[datetime.date, list[LockupEvent]]:
    """출처가 제공하는 예정표 전체에서 해당일의 계산된 일정을 조회한다."""
    response = requests.get(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    response.raise_for_status()
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"데이터\s*최종\s*확인\s*:\s*(\d{4}-\d{2}-\d{2})", text)
    if not match:
        raise ValueError("락업 일정 기준일을 확인할 수 없습니다.")
    checked = datetime.date.fromisoformat(match.group(1))
    if checked > day or (day - checked).days > MAX_SOURCE_AGE_DAYS:
        raise ValueError("락업 일정 자료가 오래되었거나 기준일이 올바르지 않습니다.")
    tables = soup.select("table")
    if not tables:
        raise ValueError("락업 일정 표를 찾지 못했습니다.")
    upcoming_tables = 0
    events = []
    for table in tables:
        section = table.find_previous("h2")
        if not section or section.get_text(" ", strip=True) != "다가오는 해제 일정":
            continue
        heading = table.find_previous("h3")
        heading_match = re.match(r"(\d{4})년\s*(\d{1,2})월", heading.get_text(" ", strip=True) if heading else "")
        headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
        if not heading_match or headers[:3] != ["해제일", "종목 · 시점", "새로 풀리는 주식"]:
            raise ValueError("락업 일정 표 형식이 변경됐습니다.")
        upcoming_tables += 1
        year, first_month = map(int, heading_match.groups())
        for row in table.select("tbody tr"):
            cells = row.select("td")
            if len(cells) != 4:
                raise ValueError("락업 일정 행 형식이 변경됐습니다.")
            date_match = re.match(r"(\d{1,2})\.(\d{1,2})\s*\(", cells[0].get_text(" ", strip=True))
            link = cells[1].select_one("a[href]")
            category = cells[1].select_one("span.sub")
            if not date_match or not link or not category:
                raise ValueError("락업 일정의 날짜 또는 종목을 읽지 못했습니다.")
            month, date = map(int, date_match.groups())
            event_date = datetime.date(year + (month < first_month), month, date)
            if event_date == day:
                events.append(LockupEvent(
                    link.get_text(" ", strip=True), category.get_text(" ", strip=True),
                    cells[2].get_text(" ", strip=True), urljoin(SOURCE_URL, link["href"]),
                ))
    if not upcoming_tables:
        raise ValueError("예정된 락업 일정 표를 찾지 못했습니다.")
    return checked, events


def format_lockups(day: datetime.date, checked: datetime.date, events: list[LockupEvent]) -> str:
    lines = [f"오늘 공모주 락업 해제 예정 ({day:%m/%d})", ""]
    for event in events:
        lines.append(f"{event.name} · {event.category}\n{event.amount}\n{event.url}")
    lines.extend([
        "", f"출처: 장전 공모주 일정 (자료 확인 {checked:%Y-%m-%d})",
        "사이트에 등록된 일부 공모주 기준이며, 해제일은 계산값입니다. 실제 일자와 물량은 공시를 확인해 주세요.",
        SOURCE_URL,
    ])
    return "\n\n".join(lines)
