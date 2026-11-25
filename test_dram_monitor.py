import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bs4 import BeautifulSoup
from bot import dram_monitor as dm
from bot import main

DAY = dt.date(2026, 11, 25)
AS_OF = "2026-11-25T12:00:00+09:00"


def price(value="60"):
    return {"as_of": AS_OF, "prices": {dm.PRODUCTS[0]: value, dm.PRODUCTS[1]: "40", dm.PRODUCTS[2]: "20"}}


class MonitorTests(unittest.TestCase):
    def test_first_run_and_unchanged(self):
        article = {"url": "https://www.trendforce.com/news/2026/11/25/dram/", "title": "DRAM market update", "date": "2026-11-25"}
        message, saved = dm.make_update(DAY, {}, price(), [article])
        self.assertIn("최초 기준", message)
        self.assertIn("US$60", message)
        self.assertIn(article["url"], message)
        self.assertIsNone(dm.make_update(DAY, saved, price(), [article])[0])

    def test_percent_compares_previous_observation_not_other_market(self):
        previous = {"prices": price()["prices"], "seen_urls": []}
        message, _ = dm.make_update(DAY, previous, price("66"), [])
        self.assertIn("+10.00%", message)
        self.assertIn("현물", message)
        self.assertNotIn("DDR4 16Gb", message)

    def test_failure_does_not_overwrite_last_price_and_is_not_repeated_same_day(self):
        previous = {"prices": price()["prices"], "seen_urls": []}
        message, saved = dm.make_update(DAY, previous, None, [])
        self.assertIn("조회 실패", message)
        self.assertEqual(saved["prices"], previous["prices"])
        self.assertIsNone(dm.make_update(DAY, saved, None, [])[0])
        self.assertIn("조회 실패", dm.make_update(DAY + dt.timedelta(days=1), saved, None, [])[0])

    def test_inaccessible_price_and_invalid_structure_fail_closed(self):
        with patch.object(dm, "_get", return_value=BeautifulSoup("<html>error</html>", "html.parser")):
            with self.assertRaises(ValueError): dm.fetch_prices()

    def test_both_news_types_deduplicate_and_filter_unrelated(self):
        def soup(url, **kwargs):
            if "presscenter" in url:
                return BeautifulSoup('<a class="title-link" href="/presscenter/news/20261125-1.html">DRAM Contract Prices Rise in Latest Report</a><a class="title-link" href="/presscenter/news/20261125-2.html">NAND Flash Prices Fall in Latest Report</a>', "html.parser")
            return BeautifulSoup('<a href="https://www.trendforce.com/news/2026/11/25/report-memory-spot/">[Insights] Memory Spot Price Update: NAND and DRAM Report</a>', "html.parser")
        with patch.object(dm, "_get", side_effect=soup):
            result = dm.fetch_articles(DAY)
        self.assertEqual(len(result), 2)
        self.assertTrue(any("presscenter" in item["url"] for item in result))
        self.assertTrue(any("/news/2026/" in item["url"] for item in result))

    def test_storage_atomic_and_per_chat(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(dm, "state_path", side_effect=lambda chat_id: Path(directory) / f"{chat_id}.json"):
            dm.save_state(1, {"prices": price()["prices"], "seen_urls": []})
            self.assertEqual(dm.load_state(1)["prices"], price()["prices"])
            self.assertEqual(dm.load_state(2), {})

    def test_corrupt_saved_price_is_not_used_as_comparison(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(dm, "state_path", return_value=Path(directory) / "state.json"):
            dm.save_state(1, {"prices": {"DDR": "0"}, "seen_urls": []})
            with self.assertRaises(ValueError): dm.load_state(1)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_saves_after_delivery(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=100), bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main, "load_dram_state", return_value={}), patch.object(main, "fetch_dram_prices", return_value=price()), patch.object(main, "fetch_dram_articles", return_value=[]), patch.object(main, "save_dram_state") as save:
            await main.send_dram_update(context)
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(save.call_args.args[0], 100)
    async def test_send_failure_does_not_record_unsent_changes(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=100), bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline"))))
        with patch.object(main, "load_dram_state", return_value={}), patch.object(main, "fetch_dram_prices", return_value=price()), patch.object(main, "fetch_dram_articles", return_value=[]), patch.object(main, "save_dram_state") as save:
            with self.assertRaises(RuntimeError): await main.send_dram_update(context)
            save.assert_not_called()

    async def test_private_chat_only(self):
        context = SimpleNamespace(job=SimpleNamespace(chat_id=-123), bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main, "load_dram_state") as load:
            await main.send_dram_update(context)
            load.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_schedule_weekday_times(self):
        queue = Mock()
        with patch.object(main, "ALLOWED_CHAT_IDS", {100, -123}):
            main.schedule_dram_updates(SimpleNamespace(job_queue=queue))
        calls = queue.run_daily.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([(c.kwargs["time"].hour, c.kwargs["time"].minute) for c in calls], [(8, 30), (16, 0)])
        self.assertTrue(all(c.kwargs["chat_id"] == 100 and c.kwargs["days"] == main.WEEKDAYS for c in calls))


if __name__ == "__main__": unittest.main()
