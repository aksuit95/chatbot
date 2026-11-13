import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import lockup, main


def page(checked="2026-11-12", rows=None, heading="2026년 11월 1건"):
    if rows is None:
        rows = '<tr><td>11.13 (금)</td><td><a href="/ipo/test/">회사</a><span class="sub">기관 확약 3개월</span></td><td>1,000주<span class="sub">1.2%</span></td><td>—</td></tr>'
    return (f'<p>데이터 최종 확인: {checked}</p><h2>다가오는 해제 일정</h2>'
            f'<h3>{heading}</h3><table><thead><tr><th>해제일</th><th>종목 · 시점</th><th>새로 풀리는 주식</th><th>누적</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


class LockupTests(unittest.TestCase):
    def test_extracts_date_category_quantity_and_source(self):
        response = Mock(text=page())
        with patch.object(lockup.requests, "get", return_value=response):
            checked, events = lockup.fetch_lockups(dt.date(2026, 11, 13))
        self.assertEqual(checked, dt.date(2026, 11, 12))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].category, "기관 확약 3개월")
        self.assertIn("1,000주", events[0].amount)
        self.assertIn("일부 공모주", lockup.format_lockups(dt.date(2026, 11, 13), checked, events))

    def test_unrelated_dates_are_not_sent(self):
        with patch.object(lockup.requests, "get", return_value=Mock(text=page())):
            self.assertEqual(lockup.fetch_lockups(dt.date(2026, 11, 14))[1], [])

    def test_stale_or_missing_data_fails_closed(self):
        for body in (page(checked="2026-10-01"), "<html>error</html>", page().replace("새로 풀리는 주식", "unexpected")):
            with patch.object(lockup.requests, "get", return_value=Mock(text=body)):
                with self.assertRaises(ValueError):
                    lockup.fetch_lockups(dt.date(2026, 11, 13))

    def test_year_rollover_under_following_months_heading(self):
        rows = '<tr><td>01.05 (화)</td><td><a href="/ipo/test/">회사</a><span class="sub">상장 후 1개월</span></td><td>10주</td><td>—</td></tr>'
        with patch.object(lockup.requests, "get", return_value=Mock(text=page(checked="2026-12-31", heading="2026년 12월 이후", rows=rows))):
            self.assertEqual(len(lockup.fetch_lockups(dt.date(2027, 1, 5))[1]), 1)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_does_not_send_but_source_failure_is_reported(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=100), bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main.datetime, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = dt.datetime(2026, 11, 13, 8, 30, tzinfo=main.SEOUL)
            with patch.object(main, "fetch_lockups", return_value=(dt.date(2026, 11, 12), [])):
                await main.send_daily_lockup(context)
            context.bot.send_message.assert_not_awaited()
            with patch.object(main, "fetch_lockups", side_effect=ValueError("stale")), self.assertLogs(main.logger, level="ERROR"):
                await main.send_daily_lockup(context)
        self.assertIn("확인하지 못했어요", context.bot.send_message.call_args.kwargs["text"])

    async def test_no_late_preopen_notice(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=100), bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main.datetime, "datetime", wraps=dt.datetime) as clock, patch.object(main, "fetch_lockups") as fetch:
            clock.now.return_value = dt.datetime(2026, 11, 13, 9, 1, tzinfo=main.SEOUL)
            await main.send_daily_lockup(context)
        fetch.assert_not_called()
        context.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
