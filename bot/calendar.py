"""구글 캘린더에서 오늘 일정을 가져온다.

터미널 확인: python -m bot.calendar
인증 정보가 없으면 브라우저 로그인을 한 번 열고, refresh token은 .env에 저장한다.
"""

import datetime
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httplib2
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# 스크립트로 실행해도 프로젝트 루트의 .env를 읽는다.
load_dotenv(ROOT / ".env")

# 일정 읽기만 한다. 캘린더를 수정할 권한이 필요 없다.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# 서버 시간대와 상관없이 한국 날짜의 일정을 가져온다.
SEOUL = ZoneInfo("Asia/Seoul")
TIMEOUT_SECONDS = 10
TOKEN_URI = "https://oauth2.googleapis.com/token"

FETCH_FAILED_MESSAGE = "일정을 가져오지 못했어요."
NO_EVENTS_MESSAGE = "오늘 일정이 없어요."
MISSING_CLIENT_MESSAGE = (
    "GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET이 .env에 없습니다.\n"
    "Google Cloud에서 OAuth 클라이언트(데스크톱 앱)를 만든 뒤 .env에 넣어 주세요."
)
WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True)
class CalendarEvent:
    calendar_name: str
    title: str
    all_day: bool
    start: datetime.datetime | None
    end: datetime.datetime | None


def seoul_today() -> datetime.date:
    return datetime.datetime.now(SEOUL).date()


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def save_env_value(key: str, value: str, env_path: Path | None = None) -> None:
    """지정한 키 한 줄만 바꿔 .env의 다른 값은 그대로 둔다."""
    path = env_path or (ROOT / ".env")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{key}={value}"
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            updated.append(new_line)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(new_line)
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _client_config(client_id: str, client_secret: str) -> dict:
    # 파일로 받은 클라이언트 비밀 대신 .env 값으로 같은 설정을 만든다.
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def login_and_save() -> bool:
    """PC에서 한 번만 브라우저 로그인을 하고 refresh token을 .env에 저장한다."""
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return False
    try:
        # 함수 안에서만 불러 일정 조회에는 로그인 창 라이브러리가 필요 없게 한다.
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_config(
            _client_config(client_id, client_secret), [CALENDAR_SCOPE]
        )
        # prompt=consent가 있어야 이미 승인한 계정에서도 refresh token이 다시 나온다.
        credentials = flow.run_local_server(
            port=0, access_type="offline", prompt="consent"
        )
    except Exception:
        logger.exception("구글 로그인에 실패했습니다.")
        return False
    if not credentials.refresh_token:
        logger.error("refresh token을 받지 못했습니다.")
        return False
    save_env_value("GOOGLE_REFRESH_TOKEN", credentials.refresh_token)
    os.environ["GOOGLE_REFRESH_TOKEN"] = credentials.refresh_token
    return True


def _credentials() -> Credentials | None:
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    refresh_token = _env("GOOGLE_REFRESH_TOKEN")
    if not client_id or not client_secret or not refresh_token:
        logger.error("구글 캘린더 인증 정보가 .env에 없습니다.")
        return None
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=[CALENDAR_SCOPE],
    )
    try:
        credentials.refresh(Request())
    except RefreshError:
        logger.exception("구글 로그인 갱신에 실패했습니다.")
        return None
    except Exception:
        logger.exception("구글 로그인 갱신에 실패했습니다.")
        return None
    return credentials


def _build_service(credentials: Credentials):
    # 응답이 없으면 아침 발송이 계속 기다리지 않도록 시간을 제한한다.
    authed_http = AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=TIMEOUT_SECONDS)
    )
    return build("calendar", "v3", http=authed_http)


def _day_bounds(today: datetime.date) -> tuple[str, str]:
    # 시작은 오늘 0시, 끝은 내일 0시라서 오늘과 겹치는 일정만 남는다.
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=SEOUL)
    end = start + datetime.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _list_selected_calendars(service) -> list[dict] | None:
    """캘린더 화면에서 체크된 캘린더만 가져온다.

    구글은 체크된 캘린더도 hidden으로 내려줄 때가 있다.
    숨김을 빼면 그 캘린더의 오늘 일정이 통째로 빠진다.
    """
    try:
        calendars: list[dict] = []
        page_token = None
        while True:
            result = (
                service.calendarList()
                .list(showHidden=True, pageToken=page_token)
                .execute()
            )
            calendars.extend(result.get("items", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    except Exception:
        logger.exception("캘린더 목록 조회에 실패했습니다.")
        return None
    selected = [item for item in calendars if item.get("selected")]
    # 기본 캘린더를 먼저, 나머지는 이름 순으로 보여 준다.
    selected.sort(key=lambda item: (not item.get("primary"), item.get("summary") or ""))
    return selected


def _list_events(service, calendar_id: str, time_min: str, time_max: str) -> list[dict] | None:
    try:
        events: list[dict] = []
        page_token = None
        while True:
            result = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    timeZone="Asia/Seoul",
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            events.extend(result.get("items", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return events
    except Exception:
        logger.exception("일정 조회에 실패했습니다. calendar_id=%s", calendar_id)
        return None


def _user_declined(event: dict) -> bool:
    for attendee in event.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def _parse_bound(bound: dict) -> tuple[datetime.datetime | None, bool]:
    """dateTime이면 시각, date면 종일 일정이다."""
    if bound.get("dateTime"):
        raw = str(bound["dateTime"]).replace("Z", "+00:00")
        parsed = datetime.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SEOUL)
        return parsed.astimezone(SEOUL), False
    if bound.get("date"):
        return None, True
    return None, False


def _to_calendar_event(calendar_name: str, event: dict) -> CalendarEvent | None:
    if event.get("status") == "cancelled" or _user_declined(event):
        return None
    start_raw = event.get("start") or {}
    end_raw = event.get("end") or {}
    start, all_day = _parse_bound(start_raw)
    end, end_all_day = _parse_bound(end_raw)
    if not all_day and start is None:
        return None
    title = str(event.get("summary") or "").strip() or "(제목 없음)"
    return CalendarEvent(
        calendar_name=calendar_name,
        title=title,
        all_day=all_day or end_all_day,
        start=None if all_day else start,
        end=None if all_day else end,
    )


def fetch_today_events(today: datetime.date | None = None) -> list[CalendarEvent] | None:
    """오늘과 겹치는 일정을 가져온다. 실패하면 None, 없으면 빈 목록이다."""
    credentials = _credentials()
    if credentials is None:
        return None
    try:
        service = _build_service(credentials)
    except Exception:
        logger.exception("구글 캘린더 연결에 실패했습니다.")
        return None

    calendars = _list_selected_calendars(service)
    if calendars is None:
        return None

    day = today or seoul_today()
    time_min, time_max = _day_bounds(day)
    events: list[CalendarEvent] = []
    any_success = False
    for calendar in calendars:
        calendar_id = calendar.get("id")
        if not calendar_id:
            continue
        raw_events = _list_events(service, calendar_id, time_min, time_max)
        if raw_events is None:
            continue
        any_success = True
        calendar_name = str(calendar.get("summary") or "").strip() or "(이름 없음)"
        for raw_event in raw_events:
            parsed = _to_calendar_event(calendar_name, raw_event)
            if parsed is not None:
                events.append(parsed)
    # 캘린더는 있는데 전부 실패하면 빈 일정으로 오해하지 않게 실패로 본다.
    if calendars and not any_success:
        return None
    return events


def _format_when(event: CalendarEvent, today: datetime.date) -> str:
    """자정을 넘는 일정은 오늘 구간만 짧게 보여 준다."""
    if event.all_day or event.start is None:
        return "종일"
    start = event.start.astimezone(SEOUL)
    end = event.end.astimezone(SEOUL) if event.end else None
    today_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=SEOUL)
    tomorrow_start = today_start + datetime.timedelta(days=1)
    started_before = start < today_start
    ends_after = end is not None and end > tomorrow_start
    if started_before and ends_after:
        return "종일"
    if started_before and end is not None:
        return f"~{end.strftime('%H:%M')}"
    if ends_after:
        return f"{start.strftime('%H:%M')}~"
    if end is not None:
        return f"{start.strftime('%H:%M')}~{end.strftime('%H:%M')}"
    return start.strftime("%H:%M")


def format_schedule(events: list[CalendarEvent], today: datetime.date) -> str:
    """캘린더 이름별로 시간순 목록을 만든다."""
    weekday = WEEKDAYS[today.weekday()]
    header = f"오늘 일정 ({today.month}월 {today.day}일 {weekday})"
    if not events:
        return f"{header}\n\n{NO_EVENTS_MESSAGE}"

    lines = [header, ""]
    current_name = None
    for event in events:
        if event.calendar_name != current_name:
            if current_name is not None:
                lines.append("")
            current_name = event.calendar_name
            lines.append(current_name)
        lines.append(f"{_format_when(event, today)} {event.title}")
    return "\n".join(lines)


def build_today_message(today: datetime.date | None = None) -> str:
    """아침 발송에 넣을 문장. 조회 실패와 일정 없음을 구분한다."""
    day = today or seoul_today()
    events = fetch_today_events(day)
    if events is None:
        return FETCH_FAILED_MESSAGE
    return format_schedule(events, day)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    if not _env("GOOGLE_CLIENT_ID") or not _env("GOOGLE_CLIENT_SECRET"):
        print(MISSING_CLIENT_MESSAGE)
        raise SystemExit(1)
    if not _env("GOOGLE_REFRESH_TOKEN"):
        print("브라우저에서 구글 로그인을 진행합니다.")
        if not login_and_save():
            print(FETCH_FAILED_MESSAGE)
            raise SystemExit(1)
    message = build_today_message()
    print(message)
    if message == FETCH_FAILED_MESSAGE:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
