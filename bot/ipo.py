"""한국거래소 KIND의 공모기업 상장예정일을 조회한다."""

import datetime
import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://kind.krx.co.kr/listinvstg/pubofrprogcom.do"
SOURCE_PAGE = SOURCE_URL + "?method=searchPubofrProgComMain"
PAGE_SIZE = 100


@dataclass(frozen=True)
class IpoListing:
    name: str
    date: datetime.date
    price: str
    underwriter: str


def fetch_listings(day: datetime.date) -> list[IpoListing]:
    """신고일 기준 최근 2년의 모든 페이지에서 해당일 상장예정 종목을 찾는다."""
    start = day - datetime.timedelta(days=730)
    result = []
    page = 1
    total_expected = None
    seen_rows = 0
    page_signatures = set()
    while True:
        response = requests.post(
            SOURCE_URL,
            data={"method": "searchPubofrProgComSub", "pageIndex": str(page),
                  "currentPageSize": str(PAGE_SIZE), "marketType": "",
                  "fromDate": start.isoformat(), "toDate": day.isoformat()},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.select_one("table.list.type-00")
        summary = table.get("summary", "") if table else ""
        summary = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), summary)
        if not table or "상장예정일" not in summary or not ({"확정공모가", "확정곰모가"} & set(summary.split(", "))):
            raise ValueError("KIND 공모기업 표 형식이 변경됐습니다.")
        count = re.search(r"전체\s*([\d,]+)\s*건", soup.get_text(" ", strip=True))
        if not count:
            raise ValueError("KIND 공모기업 전체 건수를 확인할 수 없습니다.")
        total = int(count.group(1).replace(",", ""))
        if total_expected is None:
            total_expected = total
        elif total != total_expected:
            raise ValueError("조회 중 공모기업 목록이 변경됐습니다.")
        rows = [row.select("td") for row in table.select("tr")]
        rows = [cells for cells in rows if len(cells) == 9]
        if not rows and seen_rows < total:
            raise ValueError("KIND 공모기업 목록의 일부를 읽지 못했습니다.")
        signature = tuple(tuple(cell.get_text(" ", strip=True) for cell in row) for row in rows)
        if signature and signature in page_signatures:
            raise ValueError("KIND 공모기업 목록 페이지가 반복됐습니다.")
        page_signatures.add(signature)
        for cells in rows:
            values = [cell.get_text(" ", strip=True) for cell in cells]
            name, date_text = values[0], values[7]
            if not date_text or date_text in {"-", "미정"}:
                continue
            try:
                listing_date = datetime.date.fromisoformat(date_text)
            except ValueError as error:
                raise ValueError("KIND 상장예정일 형식이 변경됐습니다.") from error
            if listing_date == day:
                result.append(IpoListing(name, listing_date, values[5], values[8]))
        seen_rows += len(rows)
        if seen_rows >= total:
            return result
        page += 1


def format_listings(day: datetime.date, listings: list[IpoListing]) -> str:
    lines = [f"오늘 공모주 상장 예정 ({day:%m/%d})", ""]
    for item in listings:
        price = f"{item.price}원" if re.fullmatch(r"[\d,]+", item.price) else "미확인"
        lines.append(f"{item.name} · 확정 공모가 {price}\n주관사: {item.underwriter or '미확인'}")
    lines.extend(["", "상장 일정은 변경될 수 있습니다.", f"출처: 한국거래소 KIND\n{SOURCE_PAGE}"])
    return "\n".join(lines)
