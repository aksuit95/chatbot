import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import calendar as cal
from bot import main


NOW = dt.datetime(2026, 11, 3, 23, 45, tzinfo=cal.SEOUL)


def event(minutes=30, event_id="meeting", **changes):
    values = dict(
        calendar_name="Calendar", title="Meeting", all_day=False,
        start=NOW + dt.timedelta(minutes=minutes),
        end=NOW + dt.timedelta(minutes=minutes + 60),
        calendar_id="calendar-1", event_id=event_id,
    )
    values.update(changes)
    return cal.CalendarEvent(**values)


class CalendarReminderTests(unittest.TestCase):
    def test_window_crosses_midnight_and_excludes_all_day_past_and_later(self):
        due = event()
        events = [event(31), event(-5), event(0), event(all_day=True), due, event(5)]
        with patch.object(cal, "fetch_events_between", return_value=events) as fetch:
            self.assertEqual(cal.fetch_reminder_events(NOW), [events[-1], due])
        lo, hi = fetch.call_args.args
        self.assertEqual(dt.datetime.fromisoformat(lo), NOW)
        self.assertEqual(dt.datetime.fromisoformat(hi), NOW + dt.timedelta(minutes=30, seconds=1))

    def test_cancelled_and_declined_events_are_excluded(self):
        self.assertIsNone(cal._to_calendar_event("Calendar", {"status": "cancelled"}))
        self.assertIsNone(cal._to_calendar_event("Calendar", {
            "attendees": [{"self": True, "responseStatus": "declined"}],
        }))

    def test_identity_distinguishes_instances_calendars_recipients_and_moves(self):
        first = event()
        key = cal.reminder_key(100, first)
        self.assertNotEqual(key, cal.reminder_key(101, first))
        self.assertNotEqual(key, cal.reminder_key(100, event(29)))
        self.assertNotEqual(key, cal.reminder_key(100, event(calendar_id="other")))
        self.assertNotEqual(key, cal.reminder_key(100, event(event_id="next-instance")))
        self.assertEqual(key, cal.reminder_key(100, event(title="New title")))

    def test_history_survives_reload_prunes_and_contains_no_event_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            with patch.object(cal, "reminder_history_path", return_value=path):
                key = cal.reminder_key(100, event())
                history = {key: event().start.timestamp(), "expired": NOW.timestamp() - 1}
                cal.save_reminder_history(history)
                self.assertEqual(cal.load_reminder_history(NOW), {key: history[key]})
                self.assertNotIn("Meeting", path.read_text())

    def test_original_morning_query_keeps_day_bounds(self):
        with patch.object(cal, "fetch_events_between", return_value=[]) as fetch:
            self.assertEqual(cal.fetch_today_events(NOW.date()), [])
        self.assertEqual(fetch.call_args.args, cal._day_bounds(NOW.date()))

    def test_google_ids_are_retained_for_recurrence_deduplication(self):
        parsed = cal._to_calendar_event("Calendar", {
            "id": "instance-1", "summary": "Meeting",
            "start": {"dateTime": event().start.isoformat()},
            "end": {"dateTime": event().end.isoformat()},
        }, "calendar-1")
        self.assertEqual(parsed.event_id, "instance-1")
        self.assertEqual(parsed.calendar_id, "calendar-1")


class ReminderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = patch.object(main.datetime, "datetime", wraps=dt.datetime)
        self.mock_clock = self.clock.start()
        self.mock_clock.now.return_value = NOW
        self.addCleanup(self.clock.stop)
        self.context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        self.chats = patch.object(main, "ALLOWED_CHAT_IDS", {100, -200})
        self.chats.start()
        self.addCleanup(self.chats.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = patch.object(cal, "reminder_history_path", return_value=Path(self.temp.name) / "history.json")
        self.path.start()
        self.addCleanup(self.path.stop)

    async def test_sends_only_once_to_private_chat_even_after_restart(self):
        with patch.object(main, "fetch_reminder_events", return_value=[event()]):
            await main.send_calendar_reminders(self.context)
            await main.send_calendar_reminders(self.context)
            self.context.bot_data = {}  # Simulate restart while preserving the history file.
            await main.send_calendar_reminders(self.context)
        self.context.bot.send_message.assert_awaited_once()
        sent = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], 100)
        self.assertIn("30분 후", sent["text"])
        self.assertIn("11월 4일", sent["text"])

    async def test_failed_send_retries_and_does_not_block_other_events(self):
        self.context.bot.send_message.side_effect = [RuntimeError("temporary"), None, None]
        with patch.object(main, "fetch_reminder_events", return_value=[event(), event(20, "other")]):
            with self.assertLogs(main.logger, level="ERROR"):
                await main.send_calendar_reminders(self.context)
            await main.send_calendar_reminders(self.context)
        self.assertEqual(self.context.bot.send_message.await_count, 3)

    async def test_fetch_failure_does_not_send_repeated_failure_messages(self):
        with patch.object(main, "fetch_reminder_events", return_value=None):
            await main.send_calendar_reminders(self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_event_that_starts_during_fetch_is_skipped(self):
        self.mock_clock.now.side_effect = [NOW, NOW + dt.timedelta(minutes=31)]
        with patch.object(main, "fetch_reminder_events", return_value=[event()]):
            await main.send_calendar_reminders(self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_no_private_chat_means_no_fetch_or_send(self):
        with patch.object(main, "ALLOWED_CHAT_IDS", {-200}), patch.object(main, "fetch_reminder_events") as fetch:
            await main.send_calendar_reminders(self.context)
            fetch.assert_not_called()
        self.context.bot.send_message.assert_not_awaited()

    async def test_scheduler_registers_one_minute_poll_without_overlapping_runs(self):
        app = SimpleNamespace(job_queue=Mock())
        main.schedule_calendar_reminders(app)
        args = app.job_queue.run_repeating.call_args.kwargs
        self.assertEqual(args["interval"], 60)
        self.assertEqual(args["job_kwargs"]["max_instances"], 1)
        self.assertTrue(args["job_kwargs"]["coalesce"])


if __name__ == "__main__":
    unittest.main()
