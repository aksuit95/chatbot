import asyncio
import datetime
import html
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# python bot/main.py 로 실행해도 프로젝트 루트에서 bot 패키지를 찾는다.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.chart import StockNotFoundError, build_stock_chart
from bot.market import (
    FETCH_FAILED_MESSAGE,
    fetch_market_cap_tops,
    fetch_market_quotes,
    format_market_cap_message,
    format_market_message,
)
from bot.news import DEFAULT_QUERY, NEWS_LIMIT, fetch_news, format_news_message

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
# 요청 주소에 봇 토큰이 포함되므로, httpx 로그는 남기지 않는다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NO_NEW_NEWS_MESSAGE = "새 뉴스가 없어요."
SEEN_NEWS_KEY = "seen_news_links"

# 서버 시간대와 상관없이 한국 시각 기준으로 보낸다.
BRIEFING_TIME = datetime.time(hour=8, minute=30, tzinfo=ZoneInfo("Asia/Seoul"))
# python-telegram-bot 20 이후 0=일요일 ~ 6=토요일. 월~금만 보낸다.
WEEKDAYS = (1, 2, 3, 4, 5)
BRIEFING_JOB_NAME = "daily_briefing"

START_MESSAGE = (
    "시황 뉴스 봇입니다.\n"
    "국내 주요 지수와 뉴스를 알려 드립니다.\n"
    "\n"
    "사용 가능한 명령어\n"
    "/start - 사용법 안내\n"
    "/market - 코스피, 코스닥, 원/달러, 시가총액 상위 20 등락률\n"
    "/news - 최신 증시 뉴스 5건\n"
    "/news 키워드 - 해당 키워드 뉴스 5건\n"
    "/chart 종목명 - 최근 6개월 일봉 캔들 차트 (예: /chart 삼성전자)"
)
CHART_USAGE_MESSAGE = "종목명이나 종목코드를 함께 보내 주세요. 예: /chart 삼성전자"


def parse_allowed_chat_ids(raw: str | None) -> set[int]:
    """쉼표로 구분한 chat_id 목록을 숫자 집합으로 바꾼다. 숫자가 아닌 값은 건너뛴다."""
    chat_ids: set[int] = set()
    for part in (raw or "").split(","):
        value = part.strip()
        if not value:
            continue
        if not value.lstrip("-").isdigit():
            logger.error("ALLOWED_CHAT_ID에 숫자가 아닌 값이 있어 건너뜁니다.")
            continue
        chat_ids.add(int(value))
    return chat_ids


# 1:1 대화방과 단체방을 함께 허용하려면 "123456789,-5315329531"처럼 쉼표로 적는다.
ALLOWED_CHAT_IDS = parse_allowed_chat_ids(os.getenv("ALLOWED_CHAT_ID"))


def is_allowed(chat_id: int | None) -> bool:
    """허용된 chat_id만 True. 설정이 없거나 다른 대화방이면 False."""
    return chat_id is not None and chat_id in ALLOWED_CHAT_IDS


def seen_links_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> set[str]:
    """대화방마다 보낸 뉴스를 따로 기록해, 한 방에 보냈다고 다른 방에서 빠지지 않게 한다."""
    seen_by_chat = context.bot_data.setdefault(SEEN_NEWS_KEY, {})
    return seen_by_chat.setdefault(chat_id, set())


def can_reply(update: Update) -> bool:
    """허용된 사용자의 메시지일 때만 답장을 보낸다."""
    if update.effective_chat is None or update.message is None:
        return False
    return is_allowed(update.effective_chat.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_reply(update):
        return
    await update.message.reply_text(START_MESSAGE)


async def build_market_html() -> str:
    """시황 표를 텔레그램 HTML로 만든다. 실패하면 안내 문장만 돌려준다."""
    try:
        # 시세 조회는 동기 함수라, 기다리는 동안 다른 명령을 막지 않게 스레드에서 실행한다.
        rows = await asyncio.to_thread(fetch_market_quotes)
        text = format_market_message(rows)
    except Exception:
        logger.exception("시세 조회에 실패했습니다.")
        text = FETCH_FAILED_MESSAGE

    if text == FETCH_FAILED_MESSAGE:
        return html.escape(text)
    # <pre>는 칸을 맞춘 표를 고정폭으로 보여 준다.
    return f"<pre>{html.escape(text)}</pre>"


async def build_market_cap_html() -> str:
    """코스피·코스닥 시가총액 상위 표를 텔레그램 HTML로 만든다."""
    try:
        tops = await asyncio.to_thread(fetch_market_cap_tops)
    except Exception:
        logger.exception("시가총액 순위 조회에 실패했습니다.")
        return html.escape(FETCH_FAILED_MESSAGE)
    tables = [format_market_cap_message(name, ranks) for name, ranks in tops]
    return "\n\n".join(f"<pre>{html.escape(table)}</pre>" for table in tables)


async def market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_reply(update):
        return
    # 지수와 시총 순위는 서로 기다릴 필요가 없어 동시에 조회한다.
    market_html, market_cap_html = await asyncio.gather(
        build_market_html(), build_market_cap_html()
    )
    await update.message.reply_text(
        f"{market_html}\n\n{market_cap_html}", parse_mode=ParseMode.HTML
    )


async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_reply(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(CHART_USAGE_MESSAGE)
        return
    try:
        # 목록 조회와 그리기가 몇 초 걸리므로 스레드에서 실행해 다른 명령을 막지 않는다.
        stock_chart = await asyncio.to_thread(build_stock_chart, query)
    except StockNotFoundError:
        await update.message.reply_text(f"종목을 찾지 못했어요: {query}")
        return
    if stock_chart is None:
        await update.message.reply_text(FETCH_FAILED_MESSAGE)
        return
    await update.message.reply_photo(
        photo=stock_chart.image,
        caption=(
            f"{stock_chart.name}({stock_chart.code}) 최근 6개월 일봉\n"
            f"종가 {stock_chart.last_close:,.0f}원 ({stock_chart.last_date})"
        ),
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_reply(update):
        return
    # /news 반도체 처럼 명령 뒤의 단어를 검색어로 쓴다. 없으면 증시.
    query = " ".join(context.args or []).strip() or DEFAULT_QUERY
    seen_links = seen_links_for(context, update.effective_chat.id)
    try:
        articles = await asyncio.to_thread(fetch_news, query, seen_links, NEWS_LIMIT)
    except Exception:
        logger.exception("뉴스 조회에 실패했습니다.")
        articles = None

    if articles is None:
        await update.message.reply_text(FETCH_FAILED_MESSAGE)
        return
    if not articles:
        await update.message.reply_text(NO_NEW_NEWS_MESSAGE)
        return

    await update.message.reply_text(format_news_message(query, articles))
    # 답장을 보낸 뒤에 기록해야, 전송이 실패했을 때 같은 기사를 다시 시도할 수 있다.
    seen_links.update(article.link for article in articles)


async def send_daily_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """예약 시각에 시황과 뉴스 헤드라인을 한 메시지로 보낸다."""
    chat_id = context.job.chat_id
    seen_links = seen_links_for(context, chat_id)

    market_html = await build_market_html()
    try:
        articles = await asyncio.to_thread(
            fetch_news, DEFAULT_QUERY, seen_links, NEWS_LIMIT
        )
    except Exception:
        logger.exception("자동 발송용 뉴스 조회에 실패했습니다.")
        articles = None

    # 뉴스가 실패해도 시황은 보내야 하므로, 뉴스 칸만 안내 문장으로 바꾼다.
    if articles is None:
        news_text = f"{DEFAULT_QUERY} 뉴스\n{FETCH_FAILED_MESSAGE}"
    elif not articles:
        news_text = f"{DEFAULT_QUERY} 뉴스\n{NO_NEW_NEWS_MESSAGE}"
    else:
        news_text = format_news_message(DEFAULT_QUERY, articles)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{market_html}\n\n{html.escape(news_text)}",
        parse_mode=ParseMode.HTML,
        # 뉴스 링크 미리보기가 붙으면 시황 표가 밀려 보이지 않는다.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    if articles:
        seen_links.update(article.link for article in articles)


def schedule_daily_briefing(application: Application) -> None:
    """평일 08:30(한국 시각)에 허용된 대화방마다 자동 발송을 등록한다."""
    if application.job_queue is None:
        logger.error("JobQueue를 쓸 수 없어 자동 발송을 등록하지 못했습니다.")
        return
    if not ALLOWED_CHAT_IDS:
        logger.error("ALLOWED_CHAT_ID가 올바르지 않아 자동 발송을 등록하지 못했습니다.")
        return
    for chat_id in sorted(ALLOWED_CHAT_IDS):
        application.job_queue.run_daily(
            send_daily_briefing,
            time=BRIEFING_TIME,
            days=WEEKDAYS,
            chat_id=chat_id,
            name=BRIEFING_JOB_NAME,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """명령 처리 중 예외가 나도 봇 프로세스는 유지한다."""
    logger.exception("명령 처리 중 오류가 났습니다.", exc_info=context.error)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("market", market))
    application.add_handler(CommandHandler("news", news))
    application.add_handler(CommandHandler("chart", chart))
    application.add_error_handler(on_error)
    schedule_daily_briefing(application)
    return application


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN이 .env에 없습니다.")
    application = build_application(TELEGRAM_BOT_TOKEN)
    application.run_polling()


if __name__ == "__main__":
    main()
