import datetime as dt
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import main
from bot import watchlist as wl


def quote(code="005930", name="삼성전자"):
    return wl.StockQuote(code, name, Decimal("70000"), Decimal("-1.25"), dt.datetime(2026, 11, 4, 15, 30, tzinfo=wl.SEOUL), "CLOSE")


class QuoteTests(unittest.TestCase):
    def test_exact_name_resolves_domestic_stock_without_partial_match(self):
        items = [
            {"code": "005930", "name": "삼성전자", "nationCode": "KOR"},
            {"code": "005935", "name": "삼성전자우", "nationCode": "KOR"},
        ]
        with patch.object(wl, "_request_json", return_value={"items": items}):
            self.assertEqual(wl.resolve_stock_code("삼성전자"), "005930")
            with self.assertRaises(wl.StockNotFoundError):
                wl.resolve_stock_code("삼성")

    def test_invalid_and_ambiguous_queries_do_not_choose_a_stock(self):
        with patch.object(wl, "_request_json", return_value={"items": []}):
            for value in ("", "x" * 81, "not-a-stock"):
                with self.assertRaises(wl.StockNotFoundError):
                    wl.resolve_stock_code(value)
        with patch.object(wl, "_request_json", return_value={"items": [
            {"code": "005930", "name": "Same", "nationCode": "KOR"},
            {"code": "000660", "name": "Same", "nationCode": "KOR"},
        ]}):
            with self.assertRaises(wl.StockNotFoundError):
                wl.resolve_stock_code("Same")

    def test_code_skips_search_and_price_keeps_previous_day_timestamp(self):
        payload = {"itemCode": "005930", "stockName": "삼성전자", "closePrice": "70,000", "fluctuationsRatio": "-1.25", "localTradedAt": "2026-11-03T15:30:00+09:00", "marketStatus": "CLOSE"}
        with patch.object(wl, "_request_json", return_value=payload) as fetch:
            result = wl.fetch_stock_quote("005930")
        fetch.assert_called_once()
        self.assertEqual(result.price, Decimal("70000"))
        text = wl.format_stock_quote(result)
        self.assertIn("11/03 15:30", text)
        self.assertIn("-1.25%", text)
        self.assertIn("장마감", text)

    def test_missing_or_nonfinite_prices_fail_instead_of_showing_wrong_data(self):
        payload = {"itemCode": "005930", "stockName": "삼성전자", "closePrice": "NaN", "fluctuationsRatio": "1", "localTradedAt": "2026-11-03T15:30:00+09:00"}
        with patch.object(wl, "_request_json", return_value=payload):
            with self.assertRaises(ValueError):
                wl.fetch_stock_quote("005930")


class WatchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "watch.json"
        self.storage = patch.object(wl, "watchlist_path", return_value=self.path)
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def test_add_duplicate_remove_and_persistence_are_per_recipient(self):
        self.assertTrue(wl.add_watch_stock(100, quote()))
        self.assertFalse(wl.add_watch_stock(100, quote()))
        self.assertEqual(wl.get_watchlist(100), [{"code": "005930", "name": "삼성전자"}])
        self.assertEqual(wl.get_watchlist(200), [])
        self.assertEqual(wl.remove_watch_stock(100, "삼성전자")["code"], "005930")
        self.assertEqual(wl.get_watchlist(100), [])
        self.assertIsNone(wl.remove_watch_stock(100, "005930"))

    def test_limit_does_not_overwrite_existing_stocks(self):
        for index in range(wl.WATCH_LIMIT):
            wl.add_watch_stock(100, quote(f"{index:06}", str(index)))
        with self.assertRaises(wl.WatchlistFullError):
            wl.add_watch_stock(100, quote("999999"))
        self.assertEqual(len(wl.get_watchlist(100)), wl.WATCH_LIMIT)

    def test_corrupt_file_is_preserved_instead_of_overwritten(self):
        self.path.write_text('{"100": "broken"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            wl.add_watch_stock(100, quote())
        self.assertEqual(json.loads(self.path.read_text()), {"100": "broken"})

    def test_one_quote_failure_does_not_hide_other_stock(self):
        wl.add_watch_stock(100, quote())
        wl.add_watch_stock(100, quote("000660", "SK하이닉스"))
        with patch.object(wl, "fetch_stock_quote", side_effect=lambda code: quote() if code == "005930" else (_ for _ in ()).throw(RuntimeError("offline"))):
            with self.assertLogs(wl.logger, level="WARNING"):
                text = wl.build_watchlist_message(100)
        self.assertIn("70,000원", text)
        self.assertIn("SK하이닉스", text)
        self.assertIn("데이터를 가져오지 못했어요", text)

    def test_empty_automatic_watchlist_is_silent(self):
        self.assertIsNone(wl.build_watchlist_message(100, True))
        self.assertIn("/watch add", wl.build_watchlist_message(100))


class StockCommandTests(unittest.IsolatedAsyncioTestCase):
    def update(self, chat_id):
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=SimpleNamespace(reply_text=AsyncMock()))

    async def test_private_add_passes_chat_id_and_stock_to_storage(self):
        update = self.update(100)
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main, "fetch_stock_quote", return_value=quote()), patch.object(main, "add_watch_stock", return_value=True) as add:
            await main.watch(update, SimpleNamespace(args=["add", "삼성전자"]))
        add.assert_called_once_with(100, quote())
        self.assertIn("추가했어요", update.message.reply_text.call_args.args[0])

    async def test_group_and_unknown_user_cannot_access_personal_watchlist(self):
        with patch.object(main, "ALLOWED_CHAT_IDS", {-200}), patch.object(main, "build_watchlist_message") as fetch:
            for chat_id in (-200, 300):
                await main.watch(self.update(chat_id), SimpleNamespace(args=[]))
            fetch.assert_not_called()

    async def test_stock_price_failure_returns_message_without_crashing(self):
        update = self.update(100)
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main, "fetch_stock_quote", side_effect=RuntimeError("offline")), self.assertLogs(main.logger, level="ERROR"):
            await main.stock(update, SimpleNamespace(args=["005930"]))
        self.assertEqual(update.message.reply_text.call_args.args[0], main.FETCH_FAILED_MESSAGE)

    async def test_morning_watchlist_is_sent_only_to_private_chat(self):
        for chat_id, expected in ((100, 2), (-200, 1)):
            context = SimpleNamespace(job=SimpleNamespace(chat_id=chat_id), bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
            with patch.object(main, "build_market_html", new=AsyncMock(return_value="market")), patch.object(main, "fetch_news", return_value=[]), patch.object(main, "build_watchlist_message", return_value="watch") as watch:
                await main.send_daily_briefing(context)
                self.assertEqual(context.bot.send_message.await_count, expected)
                if chat_id < 0:
                    watch.assert_not_called()

    async def test_watchlist_failure_does_not_prevent_market_briefing(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=100), bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main, "build_market_html", new=AsyncMock(return_value="market")), patch.object(main, "fetch_news", return_value=[]), patch.object(main, "build_watchlist_message", side_effect=RuntimeError("storage")), self.assertLogs(main.logger, level="ERROR"):
            await main.send_daily_briefing(context)
        context.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
