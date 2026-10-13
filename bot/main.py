import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")

START_MESSAGE = (
    "시황 뉴스 봇입니다.\n"
    "국내 주요 지수와 뉴스를 알려 드립니다.\n"
    "\n"
    "사용 가능한 명령어\n"
    "/start - 사용법 안내"
)


def is_allowed(chat_id: int | None) -> bool:
    """허용된 chat_id만 True. 설정이 없거나 다른 계정이면 False."""
    if chat_id is None or not ALLOWED_CHAT_ID:
        return False
    return str(chat_id) == ALLOWED_CHAT_ID.strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.message is None:
        return
    if not is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text(START_MESSAGE)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """명령 처리 중 예외가 나도 봇 프로세스는 유지한다."""
    logger.exception("명령 처리 중 오류가 났습니다.", exc_info=context.error)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN이 .env에 없습니다.")
    application = build_application(TELEGRAM_BOT_TOKEN)
    application.run_polling()


if __name__ == "__main__":
    main()
