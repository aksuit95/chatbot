import asyncio
import datetime
import html
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# python bot/main.py 로 실행해도 프로젝트 루트에서 bot 패키지를 찾는다.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.calendar import FETCH_FAILED_MESSAGE as CALENDAR_FETCH_FAILED_MESSAGE
from bot.calendar import (
    SEOUL,
    build_today_message,
    fetch_reminder_events,
    format_reminder,
    load_reminder_history,
    reminder_key,
    save_reminder_history,
)
from bot.chart import StockNotFoundError, build_stock_chart
from bot.ipo import fetch_listings, format_listings
from bot.market import (
    FETCH_FAILED_MESSAGE,
    fetch_market_cap_tops,
    fetch_market_quotes,
    fetch_us_quotes,
    format_market_cap_message,
    format_market_message,
)
from bot.news import (
    DEFAULT_QUERY,
    NEWS_LIMIT,
    fetch_news,
    format_news_message,
    naver_key_lengths,
)
from bot.watchlist import (
    StockNotFoundError as WatchStockNotFoundError,
    WatchlistFullError,
    add_watch_stock,
    build_watchlist_message,
    fetch_stock_quote,
    format_stock_quote,
    remove_watch_stock,
)

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
# 일정은 시황과 달리 주말에도 보낸다. 0=일요일 ~ 6=토요일.
CALENDAR_DAYS = (0, 1, 2, 3, 4, 5, 6)
CALENDAR_JOB_NAME = "daily_calendar"
CALENDAR_REMINDER_JOB_NAME = "calendar_reminders"
CALENDAR_REMINDER_HISTORY_KEY = "calendar_reminder_history"
CALENDAR_HEALTH_KEY = "calendar_health"
CALENDAR_WARNING_DATES_KEY = "calendar_warning_dates"
IPO_JOB_NAME = "daily_ipo"

START_MESSAGE = (
    "시황 뉴스 봇입니다.\n"
    "국내 주요 지수와 뉴스를 알려 드립니다.\n"
    "\n"
    "사용 가능한 명령어\n"
    "/start - 사용법 안내\n"
    "/schedule - 오늘 구글 캘린더 일정 (개인 대화)\n"
    "/status - 서버와 자동 알림 상태 (개인 대화)\n"
    "/stock 종목명 - 개별 종목 가격과 등락률\n"
    "/watch - 관심종목 시세 (개인 대화)\n"
    "/watch add 종목명 - 관심종목 추가\n"
    "/watch remove 종목명 - 관심종목 삭제\n"
    "/market - 코스피, 코스닥, 원/달러, 미국 마감, 시가총액 상위 20 등락률\n"
    "/news - 최신 증시 뉴스 5건\n"
    "/news 키워드 - 해당 키워드 뉴스 5건\n"
    "/chart 종목명 - 최근 6개월 일봉 캔들 차트 (예: /chart 삼성전자)"
)
CHART_USAGE_MESSAGE = "종목명이나 종목코드를 함께 보내 주세요. 예: /chart 삼성전자"
WATCH_USAGE_MESSAGE = (
    "/watch - 관심종목 시세 조회\n"
    "/watch add 삼성전자 - 추가 (최대 10개)\n"
    "/watch remove 삼성전자 - 삭제\n"
    "종목코드도 사용할 수 있어요. 예: /watch add 005930"
)


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


def is_private_chat(chat_id: int) -> bool:
    """개인 대화만 True. 단체방·채널 id는 음수라 개인 일정 발송에서 뺀다."""
    return chat_id > 0


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


async def can_reply_privately(update: Update) -> bool:
    if not can_reply(update):
        return False
    if not is_private_chat(update.effective_chat.id):
        await update.message.reply_text("이 명령은 봇과의 개인 대화에서 사용해 주세요.")
        return False
    return True


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """아침 발송을 기다리지 않고 오늘 일정을 직접 조회한다."""
    if not await can_reply_privately(update):
        return
    try:
        text = await asyncio.to_thread(build_today_message)
    except Exception:
        logger.exception("명령으로 오늘 일정 조회에 실패했습니다.")
        text = CALENDAR_FETCH_FAILED_MESSAGE
    # 긴 일정도 텔레그램 메시지 한도를 넘지 않게 나눠 보낸다.
    while text:
        end = min(len(text), 2000)
        if len(text) > end:
            newline = text.rfind("\n", 0, end)
            if newline > 0:
                end = newline + 1
        await update.message.reply_text(
            text[:end], link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        text = text[end:]


def _next_job_text(context: ContextTypes.DEFAULT_TYPE, name: str, chat_id: int) -> str:
    if context.job_queue is None:
        return "예약 기능 꺼짐"
    jobs = context.job_queue.get_jobs_by_name(name)
    times = [
        job.next_t for job in jobs
        if job.chat_id == chat_id and getattr(job, "next_t", None) is not None
    ]
    if not times:
        return "예약 없음"
    return min(times).astimezone(SEOUL).strftime("%m/%d %H:%M")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """응답 중인 서버의 실제 예약과 최근 캘린더 확인 결과를 보여 준다."""
    if not await can_reply_privately(update):
        return
    now = datetime.datetime.now(SEOUL)
    chat_id = update.effective_chat.id
    health = context.bot_data.get(CALENDAR_HEALTH_KEY, {})
    checked_at = health.get("checked_at")
    if checked_at is None:
        check_text = "아직 확인 전"
    else:
        result = "정상" if health.get("ok") else f"실패 (연속 {health.get('failures', 0)}회)"
        check_text = f"{result} · {checked_at.astimezone(SEOUL):%H:%M:%S}"
        if now - checked_at > datetime.timedelta(minutes=3):
            check_text += " · 최근 확인이 지연되고 있어요"
    reminder_active = (
        context.job_queue is not None
        and context.job_queue.scheduler.running
        and bool(context.job_queue.get_jobs_by_name(CALENDAR_REMINDER_JOB_NAME))
    )
    lines = [
        "봇 상태", f"한국 시각: {now:%m/%d %H:%M:%S}",
        f"최근 일정 조회: {check_text}",
        f"30분 전 알림: {'매분 확인 중' if reminder_active else '예약 기능을 확인해 주세요'}",
        f"다음 시황: {_next_job_text(context, BRIEFING_JOB_NAME, chat_id)}",
        f"다음 아침 일정: {_next_job_text(context, CALENDAR_JOB_NAME, chat_id)}",
    ]
    last_sent = health.get("last_sent_at")
    if last_sent:
        lines.append(f"최근 사전 알림 전송: {last_sent.astimezone(SEOUL):%m/%d %H:%M}")
    if health.get("delivery_failed"):
        lines.append("최근 사전 알림 전송이 실패해 재시도 중입니다.")
    await update.message.reply_text("\n".join(lines))


def record_calendar_check(context: ContextTypes.DEFAULT_TYPE, now: datetime.datetime, ok: bool) -> dict:
    health = context.bot_data.setdefault(CALENDAR_HEALTH_KEY, {})
    health.update(checked_at=now, ok=ok, failures=0 if ok else health.get("failures", 0) + 1)
    return health


async def notify_calendar_failure(
    context: ContextTypes.DEFAULT_TYPE, now: datetime.datetime, targets: list[int]
) -> None:
    """일시 장애는 재시도하고, 3회 연속 실패하면 하루 한 번만 안내한다."""
    if context.bot_data[CALENDAR_HEALTH_KEY]["failures"] < 3:
        return
    notices = context.bot_data.setdefault(CALENDAR_WARNING_DATES_KEY, {})
    today = now.date().isoformat()
    for chat_id in targets:
        if notices.get(chat_id) == today:
            continue
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "일정 조회가 3회 이상 연속 실패했습니다.\n"
                    "일정 시작 전 알림이 지연될 수 있어요.\n"
                    "/schedule로 다시 조회하거나 /status로 상태를 확인해 주세요.\n"
                    "계속 실패하면 구글 인증과 Railway 환경변수를 확인해 주세요."
                ),
            )
        except Exception:
            logger.exception("일정 조회 장애 안내 전송에 실패했습니다.")
        else:
            notices[chat_id] = today


def _quote_section_html(text: str) -> str:
    """성공한 시세 표는 고정폭으로, 실패 문장은 그대로 이스케이프한다."""
    if text == FETCH_FAILED_MESSAGE:
        return html.escape(text)
    return f"<pre>{html.escape(text)}</pre>"


async def _load_quote_section(fetcher, title: str) -> str:
    """시세 표 하나를 조회한다. 이 표가 실패해도 다른 표는 그대로 보낸다."""
    try:
        # 시세 조회는 동기 함수라, 기다리는 동안 다른 명령을 막지 않게 스레드에서 실행한다.
        rows = await asyncio.to_thread(fetcher)
        text = format_market_message(rows, title)
    except Exception:
        logger.exception("%s 조회에 실패했습니다.", title)
        text = FETCH_FAILED_MESSAGE
    return _quote_section_html(text)


async def build_market_html() -> str:
    """국내 시황과 미국 마감을 텔레그램 HTML로 만든다."""
    # 두 표는 서로 다른 출처라 동시에 조회한다. 한쪽이 실패해도 다른 쪽은 남긴다.
    domestic, us_close = await asyncio.gather(
        _load_quote_section(fetch_market_quotes, "시황"),
        _load_quote_section(fetch_us_quotes, "미국 마감"),
    )
    return f"{domestic}\n\n{us_close}"


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


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not can_reply(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text("예: /stock 삼성전자 또는 /stock 005930")
        return
    try:
        quote = await asyncio.to_thread(fetch_stock_quote, query)
        text = "네이버 KRX 시세\n\n" + format_stock_quote(quote)
    except WatchStockNotFoundError as error:
        text = str(error)
    except Exception:
        logger.exception("개별 종목 시세 조회에 실패했습니다.")
        text = FETCH_FAILED_MESSAGE
    await update.message.reply_text(text)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await can_reply_privately(update):
        return
    args = context.args or []
    chat_id = update.effective_chat.id
    try:
        if not args:
            text = await asyncio.to_thread(build_watchlist_message, chat_id)
        else:
            action = args[0].casefold()
            query = " ".join(args[1:]).strip()
            if not query or action not in {"add", "remove", "추가", "삭제"}:
                text = WATCH_USAGE_MESSAGE
            elif action in {"add", "추가"}:
                quote = await asyncio.to_thread(fetch_stock_quote, query)
                added = await asyncio.to_thread(add_watch_stock, chat_id, quote)
                text = (
                    f"{quote.name} ({quote.code}) "
                    + ("관심종목에 추가했어요.\n/watch로 확인하고 평일 08:30에 시세를 받아보세요." if added else "이미 등록되어 있어요.")
                )
            else:
                removed = await asyncio.to_thread(remove_watch_stock, chat_id, query)
                text = f"{removed['name']} ({removed['code']}) 삭제했어요." if removed else "등록된 종목을 찾지 못했어요. /watch로 확인해 주세요."
    except WatchStockNotFoundError as error:
        text = str(error)
    except WatchlistFullError as error:
        text = str(error)
    except Exception:
        logger.exception("관심종목 처리에 실패했습니다.")
        text = "관심종목을 처리하지 못했어요. 잠시 후 다시 시도해 주세요."
    await update.message.reply_text(text)


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

    # 개인 관심종목은 같은 시각의 별도 메시지로 보내며 단체방에는 공개하지 않는다.
    if is_private_chat(chat_id):
        try:
            watch_text = await asyncio.to_thread(build_watchlist_message, chat_id, True)
            if watch_text:
                await context.bot.send_message(chat_id=chat_id, text=watch_text)
        except Exception:
            logger.exception("관심종목 아침 발송에 실패했습니다.")


async def send_daily_calendar(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매일 08:30에 오늘 일정을 개인 대화에만 보낸다."""
    chat_id = context.job.chat_id
    # 예약이 잘못 걸려도 단체방에는 개인 일정이 나가지 않게 한 번 더 거른다.
    if chat_id is None or not is_private_chat(chat_id):
        logger.info("단체방에는 개인 일정을 보내지 않습니다. chat_id=%s", chat_id)
        return
    try:
        # 구글 조회는 동기 함수라, 기다리는 동안 다른 명령을 막지 않게 스레드에서 실행한다.
        text = await asyncio.to_thread(build_today_message)
    except Exception:
        logger.exception("자동 발송용 일정 조회에 실패했습니다.")
        text = CALENDAR_FETCH_FAILED_MESSAGE
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        # 제목에 링크가 있으면 미리보기가 일정 목록을 가린다.
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def send_calendar_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """매분 확인해 시작 30분 전 알림을 개인 대화에 한 번씩 보낸다."""
    targets = sorted(chat_id for chat_id in ALLOWED_CHAT_IDS if is_private_chat(chat_id))
    if not targets:
        return
    now = datetime.datetime.now(SEOUL)
    try:
        history = context.bot_data.get(CALENDAR_REMINDER_HISTORY_KEY)
        if history is None:
            history = await asyncio.to_thread(load_reminder_history, now)
            context.bot_data[CALENDAR_REMINDER_HISTORY_KEY] = history
        for key, expiry in list(history.items()):
            if expiry <= now.timestamp():
                del history[key]
        events = await asyncio.to_thread(fetch_reminder_events, now)
    except Exception:
        logger.exception("일정 사전 알림 조회에 실패했습니다. 다음 확인 때 재시도합니다.")
        record_calendar_check(context, now, False)
        await notify_calendar_failure(context, now, targets)
        return
    health = record_calendar_check(context, now, events is not None)
    if events is None:
        await notify_calendar_failure(context, now, targets)
        return
    delivery_failed = False
    for event in events:
        for chat_id in targets:
            sent_at = datetime.datetime.now(SEOUL)
            if event.start <= sent_at:
                continue
            key = reminder_key(chat_id, event)
            if key in history:
                continue
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_reminder(event, sent_at),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception:
                delivery_failed = True
                logger.exception("일정 사전 알림 전송에 실패했습니다. 다음 확인 때 재시도합니다.")
                continue
            health["last_sent_at"] = sent_at
            # 성공한 전송만 기록한다. 저장 실패 시에도 실행 중 중복 전송은 막는다.
            history[key] = event.start.timestamp()
            try:
                await asyncio.to_thread(save_reminder_history, history)
            except Exception:
                logger.exception("일정 사전 알림 발송 기록 저장에 실패했습니다.")
    health["delivery_failed"] = delivery_failed


def schedule_calendar_reminders(application: Application) -> None:
    if application.job_queue is None:
        logger.error("JobQueue를 쓸 수 없어 일정 사전 알림을 등록하지 못했습니다.")
        return
    if not any(is_private_chat(chat_id) for chat_id in ALLOWED_CHAT_IDS):
        return
    application.job_queue.run_repeating(
        send_calendar_reminders,
        interval=60,
        first=1,
        name=CALENDAR_REMINDER_JOB_NAME,
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
    )


async def send_daily_ipo(context: ContextTypes.DEFAULT_TYPE) -> None:
    """평일 08:30에 오늘 상장 예정 공모주를 별도 안내한다."""
    now = datetime.datetime.now(SEOUL)
    # 실행 지연으로 개장 후에 도착하는 '30분 전' 알림은 보내지 않는다.
    if now.time() >= datetime.time(9, 0):
        logger.warning("개장 시간이 지나 공모주 알림을 보내지 않았습니다.")
        return
    try:
        listings = await asyncio.to_thread(fetch_listings, now.date())
    except Exception:
        logger.exception("공모주 상장 예정 조회에 실패했습니다.")
        try:
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text="오늘 공모주 상장 예정 정보를 확인하지 못했어요. 한국거래소 KIND에서 직접 확인해 주세요.\nhttps://kind.krx.co.kr/listinvstg/pubofrprogcom.do?method=searchPubofrProgComMain",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            logger.exception("공모주 조회 실패 안내도 전송하지 못했습니다.")
        return
    if listings:
        await context.bot.send_message(
            chat_id=context.job.chat_id, text=format_listings(now.date(), listings),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def schedule_daily_jobs(
    application: Application,
    callback,
    days: tuple[int, ...],
    name: str,
    chat_ids: set[int] | None = None,
) -> None:
    """같은 시각에 대상 대화방마다 반복 작업을 등록한다."""
    targets = ALLOWED_CHAT_IDS if chat_ids is None else chat_ids
    if application.job_queue is None:
        logger.error("JobQueue를 쓸 수 없어 %s 자동 발송을 등록하지 못했습니다.", name)
        return
    if not targets:
        logger.error("ALLOWED_CHAT_ID가 올바르지 않아 %s 자동 발송을 등록하지 못했습니다.", name)
        return
    for chat_id in sorted(targets):
        application.job_queue.run_daily(
            callback,
            time=BRIEFING_TIME,
            days=days,
            chat_id=chat_id,
            name=name,
        )


def schedule_daily_briefing(application: Application) -> None:
    """평일 08:30(한국 시각)에 허용된 대화방마다 자동 발송을 등록한다."""
    schedule_daily_jobs(application, send_daily_briefing, WEEKDAYS, BRIEFING_JOB_NAME)


def schedule_daily_calendar(application: Application) -> None:
    """매일 08:30(한국 시각, 주말 포함)에 개인 대화에만 오늘 일정을 등록한다."""
    private_ids = {chat_id for chat_id in ALLOWED_CHAT_IDS if is_private_chat(chat_id)}
    if not private_ids:
        logger.info("개인 대화방이 없어 일정 자동 발송을 등록하지 않았습니다.")
        return
    schedule_daily_jobs(
        application,
        send_daily_calendar,
        CALENDAR_DAYS,
        CALENDAR_JOB_NAME,
        private_ids,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """명령 처리 중 예외가 나도 봇 프로세스는 유지한다."""
    logger.exception("명령 처리 중 오류가 났습니다.", exc_info=context.error)


async def register_commands(application: Application) -> None:
    """새 명령을 텔레그램 메뉴에서도 바로 찾을 수 있게 등록한다."""
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "봇 사용법"),
            BotCommand("schedule", "오늘 일정 · 개인 대화"),
            BotCommand("status", "서버와 자동 알림 상태 · 개인 대화"),
            BotCommand("market", "국내·미국 시황과 시가총액 순위"),
            BotCommand("stock", "개별 종목 가격과 등락률"),
            BotCommand("watch", "관심종목 시세 · 개인 대화"),
            BotCommand("news", "최신 증시 뉴스"),
            BotCommand("chart", "종목의 최근 6개월 차트"),
        ])
    except Exception:
        logger.exception("명령어 메뉴 등록에 실패했습니다. 봇은 계속 실행합니다.")


def build_application(token: str) -> Application:
    application = Application.builder().token(token).post_init(register_commands).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("schedule", schedule))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("market", market))
    application.add_handler(CommandHandler("stock", stock))
    application.add_handler(CommandHandler("watch", watch))
    application.add_handler(CommandHandler("news", news))
    application.add_handler(CommandHandler("chart", chart))
    application.add_error_handler(on_error)
    schedule_daily_briefing(application)
    schedule_daily_calendar(application)
    schedule_calendar_reminders(application)
    schedule_daily_jobs(application, send_daily_ipo, WEEKDAYS, IPO_JOB_NAME)
    return application


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN이 .env에 없습니다.")
    # 값은 찍지 않고 길이만 남겨, Railway에서 키 누락/따옴표 문제를 바로 본다.
    id_len, secret_len = naver_key_lengths()
    logger.info("네이버 검색 키 상태 id_len=%s secret_len=%s", id_len, secret_len)
    application = build_application(TELEGRAM_BOT_TOKEN)
    application.run_polling()


if __name__ == "__main__":
    main()
