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

    async def test_three_failures_send_one_daily_warning_and_recovery_resets_count(self):
        with patch.object(main, "fetch_reminder_events", return_value=None):
            for _ in range(4):
                await main.send_calendar_reminders(self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.context.bot.send_message.call_args.kwargs["chat_id"], 100)
        self.assertEqual(self.context.bot_data[main.CALENDAR_HEALTH_KEY]["failures"], 4)
        with patch.object(main, "fetch_reminder_events", return_value=[]):
            await main.send_calendar_reminders(self.context)
        self.assertTrue(self.context.bot_data[main.CALENDAR_HEALTH_KEY]["ok"])
        self.assertEqual(self.context.bot_data[main.CALENDAR_HEALTH_KEY]["failures"], 0)

    async def test_warning_can_be_sent_on_next_day(self):
        with patch.object(main, "fetch_reminder_events", return_value=None):
            for _ in range(3):
                await main.send_calendar_reminders(self.context)
            self.mock_clock.now.return_value = NOW + dt.timedelta(days=1)
            await main.send_calendar_reminders(self.context)
        self.assertEqual(self.context.bot.send_message.await_count, 2)

    async def test_failed_warning_retries_without_marking_as_sent(self):
        self.context.bot.send_message.side_effect = [RuntimeError("temporary"), None]
        with patch.object(main, "fetch_reminder_events", return_value=None):
            for _ in range(2):
                await main.send_calendar_reminders(self.context)
            with self.assertLogs(main.logger, level="ERROR"):
                await main.send_calendar_reminders(self.context)
            await main.send_calendar_reminders(self.context)
            await main.send_calendar_reminders(self.context)
        self.assertEqual(self.context.bot.send_message.await_count, 2)


class CalendarCommandTests(unittest.IsolatedAsyncioTestCase):
    def update(self, chat_id):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )

    async def test_private_schedule_returns_today_and_splits_long_messages(self):
        update = self.update(100)
        text = "일정\n" * 1500
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main, "build_today_message", return_value=text):
            await main.schedule(update, SimpleNamespace())
        calls = update.message.reply_text.call_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in calls))
        self.assertEqual("".join(call.args[0] for call in calls), text)

    async def test_group_commands_never_fetch_or_disclose_personal_data(self):
        update = self.update(-200)
        with patch.object(main, "ALLOWED_CHAT_IDS", {-200}), patch.object(main, "build_today_message") as fetch:
            await main.schedule(update, SimpleNamespace())
            await main.status(update, SimpleNamespace())
            fetch.assert_not_called()
        self.assertEqual(update.message.reply_text.await_count, 2)
        self.assertIn("개인 대화", update.message.reply_text.call_args.args[0])

    async def test_unknown_user_is_ignored(self):
        update = self.update(300)
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main, "build_today_message") as fetch:
            await main.schedule(update, SimpleNamespace())
            await main.status(update, SimpleNamespace())
            fetch.assert_not_called()
        update.message.reply_text.assert_not_awaited()

    async def test_schedule_exception_returns_failure_without_crashing(self):
        update = self.update(100)
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main, "build_today_message", side_effect=RuntimeError("offline")), self.assertLogs(main.logger, level="ERROR"):
            await main.schedule(update, SimpleNamespace())
        self.assertEqual(update.message.reply_text.call_args.args[0], main.CALENDAR_FETCH_FAILED_MESSAGE)

    async def test_status_shows_real_next_run_failed_query_and_staleness(self):
        update = self.update(100)
        queue = SimpleNamespace(
            scheduler=SimpleNamespace(running=True),
            get_jobs_by_name=lambda name: (
                [SimpleNamespace(chat_id=100, next_t=NOW + dt.timedelta(hours=1))]
            ),
        )
        context = SimpleNamespace(job_queue=queue, bot_data={main.CALENDAR_HEALTH_KEY: {
            "checked_at": NOW - dt.timedelta(minutes=5), "ok": False, "failures": 3,
            "delivery_failed": True,
        }})
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}), patch.object(main.datetime, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = NOW
            await main.status(update, context)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("연속 3회", text)
        self.assertIn("지연", text)
        self.assertIn("11/04 00:45", text)
        self.assertIn("재시도", text)

    async def test_status_handles_missing_scheduler_and_no_check_yet(self):
        update = self.update(100)
        with patch.object(main, "ALLOWED_CHAT_IDS", {100}):
            await main.status(update, SimpleNamespace(job_queue=None, bot_data={}))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("아직 확인 전", text)
        self.assertIn("예약 기능 꺼짐", text)

    async def test_commands_are_registered_and_menu_failure_does_not_stop_bot(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
        await main.register_commands(app)
        commands = app.bot.set_my_commands.call_args.args[0]
        self.assertIn("schedule", [item.command for item in commands])
        self.assertIn("status", [item.command for item in commands])
        app.bot.set_my_commands.side_effect = RuntimeError("temporary")
        with self.assertLogs(main.logger, level="ERROR"):
            await main.register_commands(app)


if __name__ == "__main__":
    unittest.main()
