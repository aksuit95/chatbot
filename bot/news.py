"""네이버 클라우드(NAVER API HUB) 검색 API로 뉴스 헤드라인을 가져온다."""

import html
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 스크립트로 실행해도 프로젝트 루트의 .env를 읽는다.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 개발자센터 openapi.naver.com 대신 클라우드 API HUB를 쓴다.
NAVER_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
# /news 뒤에 키워드가 없으면 증시 뉴스를 보여 준다.
DEFAULT_QUERY = "증시"
NEWS_LIMIT = 5
# 중복을 빼도 5건이 남도록 최신 기사 30건을 가져온다. 네이버 최대는 100건이다.
FETCH_COUNT = 30
TIMEOUT_SECONDS = 10

# 네이버 제목에는 <b> 같은 태그와 &quot; 같은 문자가 섞여 있다.
_TAG_PATTERN = re.compile(r"<[^>]+>")


def clean_env(name: str) -> str:
    """환경변수 앞뒤 공백·따옴표를 제거한다. Railway 붙여넣기 실수에 대비한다."""
    return os.getenv(name, "").strip().strip('"').strip("'")


def naver_key_lengths() -> tuple[int, int]:
    """로그용으로 키 값 대신 길이만 반환한다."""
    return len(clean_env("NAVER_CLIENT_ID")), len(clean_env("NAVER_CLIENT_SECRET"))


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str


def clean_title(raw_title: str) -> str:
    """제목에서 HTML 태그를 지우고 이스케이프된 문자를 원래 글로 되돌린다."""
    without_tags = _TAG_PATTERN.sub("", raw_title)
    return html.unescape(without_tags).strip()


def parse_news_items(payload: dict) -> list[NewsItem]:
    """API 응답에서 제목과 링크를 꺼내고, 같은 링크는 한 번만 남긴다."""
    items: list[NewsItem] = []
    seen_links: set[str] = set()
    for raw in payload.get("items", []):
        title = clean_title(str(raw.get("title", "")))
        # originallink가 언론사 원문이다. 없으면 네이버 링크를 쓴다.
        link = str(raw.get("originallink") or raw.get("link") or "").strip()
        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)
        items.append(NewsItem(title=title, link=link))
    return items


def fetch_news(
    query: str,
    seen_links: set[str] | None = None,
    limit: int = NEWS_LIMIT,
) -> list[NewsItem] | None:
    """키워드로 최신 뉴스를 가져온다. 실패하면 None, 새 기사가 없으면 빈 리스트."""
    keyword = query.strip()
    if not keyword:
        logger.warning("뉴스 검색어가 비어 있습니다.")
        return None

    # Railway에 붙인 값에 따옴표가 섞여 있으면 인증이 실패한다.
    client_id = clean_env("NAVER_CLIENT_ID")
    client_secret = clean_env("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error(
            "NAVER_CLIENT_ID 또는 NAVER_CLIENT_SECRET이 없습니다. "
            "id_len=%s secret_len=%s",
            len(client_id),
            len(client_secret),
        )
        return None

    params = urllib.parse.urlencode(
        {"query": keyword, "display": FETCH_COUNT, "sort": "date", "format": "json"}
    )
    # 클라우드 키는 X-Naver-* 가 아니라 X-NCP-APIGW-* 헤더로 보낸다.
    request = urllib.request.Request(
        f"{NAVER_NEWS_URL}?{params}",
        headers={
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Railway 로그에서 401/403 원인을 바로 볼 수 있게 응답 본문도 남긴다.
        body = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error(
            "뉴스 조회 HTTP 실패. query=%s status=%s body=%s",
            keyword,
            exc.code,
            body,
        )
        return None
    except Exception:
        # 네트워크, 응답 형식 오류가 나도 호출한 쪽(봇)은 계속 살아 있어야 한다.
        logger.exception("뉴스 조회에 실패했습니다. query=%s", keyword)
        return None

    already_sent = seen_links or set()
    fresh: list[NewsItem] = []
    for item in parse_news_items(payload):
        if item.link in already_sent:
            continue
        fresh.append(item)
        if len(fresh) >= limit:
            break
    return fresh


def format_news_message(query: str, items: list[NewsItem]) -> str:
    """제목과 링크만 번호 목록으로 만든다."""
    lines = [f"{query} 뉴스", ""]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.title}")
        lines.append(item.link)
        lines.append("")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    # 터미널 확인용: python -m bot.news 반도체
    logging.basicConfig(level=logging.INFO)
    keyword = " ".join(sys.argv[1:]).strip() or DEFAULT_QUERY
    articles = fetch_news(keyword)
    if articles is None:
        print("데이터를 가져오지 못했어요")
    elif not articles:
        print("새 뉴스가 없어요.")
    else:
        for article in articles:
            print(article.title)
            print(article.link)
            print()
