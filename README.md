# 시황 뉴스 텔레그램 봇

## 목표

텔레그램으로 국내 시장의 주요 지수와 뉴스를 받아보는 개인용 봇. 처음에는 LLM 없이 데이터 조회와 발송만 구현하고, 요약 기능은 나중에 추가한다.

## 핵심 기능

- `/start`: 봇 사용법과 명령어 안내
- `/market`: 주요 지수 현황 (코스피, 코스닥, 원/달러 환율) + 코스피·코스닥 시가총액 상위 20 등락률
- `/news`: 최신 뉴스 헤드라인 + 링크
- `/chart 종목명`: 최근 6개월 일봉 캔들 차트 (종목코드로도 가능)
- 평일 08:30 자동 시황 발송
- (나중에) 뉴스 LLM 요약 `/summary`

## 기능 요구사항

- `/market`은 지수명, 현재가, 등락률을 한 메시지에 표 형태로 보여준다.
- `/market`은 그 아래에 코스피·코스닥 각각 시가총액 상위 20종목의 순위, 종목명, 등락률을 붙인다. 우선주도 포함한다. 자동 발송에는 넣지 않는다.
- `/chart`는 상승 빨강·하락 파랑 캔들과 거래량을 그린 이미지를 보낸다. 서버 한글 글꼴 문제를 피하려고 그림 안 글자는 영어로 쓰고, 종목명은 사진 설명에 넣는다.
- `/news`는 최신 5건만 보낸다. 제목과 링크만 포함한다.
- 키워드(예: 삼성전자, 반도체)가 포함된 뉴스만 필터링할 수 있다.
- 같은 뉴스는 중복해서 보내지 않는다.
- 허용된 사용자(내 chat_id)만 봇을 사용할 수 있고, 그 외 사용자의 요청은 무시한다.
- 외부 API 호출이 실패해도 봇은 종료되지 않고, "데이터를 가져오지 못했어요" 메시지를 보낸다.

## 범위 밖 (만들지 않을 것)

- 매매 주문, 투자 추천
- 웹 대시보드
- 여러 사용자 지원
- 실시간 틱 단위 시세 (조회 시점 기준 데이터만 제공)

## 기술 스택

- 언어: Python 3.11+
- 봇 라이브러리: python-telegram-bot
- 시세 데이터: FinanceDataReader (코스피·코스닥은 네이버 출처, 필요 시 증권사 API로 교체)
- 시가총액 순위: 네이버 증권 모바일 API (KRX는 로그인이 필요해서 사용하지 않음. 비공식 API라 형식이 바뀌면 조회 실패로 안내)
- 차트: mplfinance (matplotlib 기반)
- 뉴스 데이터: 네이버 검색 API (뉴스)
- 스케줄: 봇 내장 JobQueue (서버 상시 실행)
- 설정 관리: python-dotenv
- 배포: GitHub 연동 후 Railway에서 상시 실행

## 환경변수

`.env` 파일에 저장하며, 절대 GitHub에 올리지 않는다. (`.gitignore`에 포함)

| 이름 | 설명 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather에서 발급받은 봇 토큰 |
| `ALLOWED_CHAT_ID` | 봇 사용을 허용할 chat_id. 단체방도 허용하려면 `123456789,-5315329531`처럼 쉼표로 구분 |
| `NAVER_CLIENT_ID` | 네이버 검색 API 클라이언트 ID |
| `NAVER_CLIENT_SECRET` | 네이버 검색 API 시크릿 |

## 폴더 구조

```
chatbot/
├── bot/
│   ├── __init__.py
│   ├── main.py        # 봇 실행, 명령어(/start, /market, /news, /chart), 평일 08:30 자동 발송
│   ├── market.py      # 코스피·코스닥·원/달러, 시가총액 상위 20 조회와 표 만들기
│   ├── chart.py       # 종목명 -> 종목코드 변환, 캔들 차트 이미지 생성
│   └── news.py        # 네이버 뉴스 검색, 제목 정리, 중복 제거
├── .env.example       # 환경변수 이름 목록 (값은 비어 있음)
├── .python-version    # Railway에서 쓸 Python 버전
├── railway.json       # Railway 시작 명령과 재시작 정책
├── requirements.txt   # 필요한 라이브러리
├── PLAN.md            # 개발 단계별 진행 상황
└── README.md
```

## 실행 방법

### 1. 준비물

- Python 3.11 이상
- 텔레그램 봇 토큰: 텔레그램에서 [@BotFather](https://t.me/BotFather)에게 `/newbot`을 보내 발급
- 내 chat_id: 텔레그램에서 [@userinfobot](https://t.me/userinfobot)에게 아무 메시지나 보내면 `Id` 숫자를 알려 준다
- (선택) 네이버 검색 API 키: [네이버 개발자 센터](https://developers.naver.com/)에서 애플리케이션 등록 후 "검색" API 선택. 무료이며 하루 25,000회까지 쓸 수 있다. 비워 두면 `/news`는 "데이터를 가져오지 못했어요"라고 답한다.

### 2. 내 PC에서 실행 (Windows PowerShell 기준)

```powershell
git clone https://github.com/aksuit95/chatbot.git
cd chatbot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env`를 열어 값을 채운 뒤 봇을 실행한다. macOS/Linux는 `source .venv/bin/activate`, `cp .env.example .env`를 쓴다.

```powershell
python -m bot.main
```

터미널에 `Application started`가 보이면 텔레그램에서 `/start`를 보내 확인한다. 끌 때는 `Ctrl+C`.

시세만 터미널에서 확인하려면 `python -m bot.market`, 뉴스만 확인하려면 `python -m bot.news 반도체`, 차트는 `python -m bot.chart 삼성전자` (현재 폴더에 `chart_test.png`로 저장).

### 3. Railway에 배포 (PC를 꺼도 동작)

1. [railway.com](https://railway.com)에 GitHub 계정으로 로그인한다.
2. **New Project → Deploy from GitHub repo**에서 이 저장소를 선택한다.
3. 서비스의 **Variables** 탭에 위 환경변수 4개를 입력한다.
4. **Deployments** 로그에 `Application started`가 보이면 완료. 시작 명령은 `railway.json`에 들어 있어 따로 입력하지 않는다.

### 주의

- 같은 토큰으로 봇을 두 곳에서 동시에 실행하면 `Conflict` 오류가 나고 응답이 끊긴다. Railway에 배포했다면 내 PC의 봇은 끈다. Railway 실행 개수(replicas)도 1로 둔다.
- 자동 발송은 평일 08:30(한국 시각)에 `ALLOWED_CHAT_ID`에 적힌 대화방마다 간다. 봇이 그 시각에 켜져 있어야 한다.
- 단체방 chat_id는 [@RawDataBot](https://t.me/RawDataBot)을 잠깐 초대하면 알 수 있다. 일반 그룹이 슈퍼그룹으로 바뀌면 `-100`으로 시작하는 새 ID가 생기므로 다시 확인해 넣는다.
- 이미 보낸 뉴스 기록은 메모리에만 저장되어, 봇을 재시작하면 지워진다.

## 개발 진행 상태

`PLAN.md` 참고
