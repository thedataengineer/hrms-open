# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Tests for the daily overdue-reminder job (hrms.hr.journeys.send_overdue_reminders / daily): the
per-template `send_reminders` gate, the once-a-day watermark, and the batch cap. Plain unittest, rolled
back; see test_journeys_engine.py for the field-verification note and the no-HRMSTestSuite rule."""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate

from hrms.hr import journeys


class JourneysDailyTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.get_all("Company", pluck="name", limit_page_length=1)[0]
		)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		self._uid_counter = 0
		self._commit_patcher = patch.object(
			frappe.db, "commit", side_effect=AssertionError("test tried to commit")
		)
		self._commit_patcher.start()

	def tearDown(self):
		self._commit_patcher.stop()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = False

	def _uid(self) -> str:
		self._uid_counter += 1
		return f"{self._uid_counter}{frappe.generate_hash(length=6)}"

	def make_employee(self, **fields):
		uid = self._uid()
		fields.setdefault("first_name", f"Journeys Daily Test {uid}")
		fields.setdefault("company", self.company)
		fields.setdefault("date_of_joining", nowdate())
		fields.setdefault("status", "Active")
		fields.setdefault("gender", "Female")
		fields.setdefault("date_of_birth", add_days(nowdate(), -365 * 25))
		return frappe.get_doc({"doctype": "Employee", **fields}).insert(ignore_permissions=True)

	def make_user(self, **fields):
		uid = self._uid()
		fields.setdefault("email", f"journeys-daily-test-{uid}@example.com")
		fields.setdefault("first_name", f"Journeys Daily User {uid}")
		fields.setdefault("send_welcome_email", 0)
		doc = frappe.get_doc({"doctype": "User", **fields})
		doc.flags.no_welcome_mail = True
		doc.insert(ignore_permissions=True)
		return doc

	def make_template(self, steps, send_reminders=1, **fields):
		uid = self._uid()
		fields.setdefault("title", f"Journeys Daily Test Template {uid}")
		fields.setdefault("journey_type", "Custom")
		fields.setdefault("trigger", "Manual")
		fields.setdefault("company", self.company)
		fields["send_reminders"] = send_reminders
		return frappe.get_doc({"doctype": "Journey Template", "steps": steps, **fields}).insert(
			ignore_permissions=True
		)

	def start_overdue_journey(self, owner, send_reminders=1, days_overdue=5):
		employee = self.make_employee(user_id=owner.name)
		template = self.make_template(
			[
				{
					"title": "Overdue Step",
					"owner_type": "Employee",
					"due_offset_days": -days_overdue,
					"action": "Task",
				}
			],
			send_reminders=send_reminders,
		)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		journey = frappe.get_doc("Journey", journey_name)
		self.assertEqual(journey.tasks[0].status, "Open")
		return journey


class TestSendOverdueReminders(JourneysDailyTestCase):
	def test_overdue_open_task_gets_one_notification(self):
		owner = self.make_user()
		journey = self.start_overdue_journey(owner)

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 1)

		logs = frappe.get_all(
			"Notification Log",
			filters={"for_user": owner.name, "document_type": "Journey", "document_name": journey.name},
			fields=["type"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].type, "Alert")

		task = frappe.db.get_value("Journey Task", journey.tasks[0].name, "last_reminder_sent_on")
		self.assertEqual(getdate(task), getdate())

	def test_no_reminder_twice_in_one_day(self):
		owner = self.make_user()
		self.start_overdue_journey(owner)

		first = journeys.send_overdue_reminders()
		second = journeys.send_overdue_reminders()
		self.assertEqual(first, 1)
		self.assertEqual(second, 0)

		count = frappe.db.count("Notification Log", {"for_user": owner.name, "document_type": "Journey"})
		self.assertEqual(count, 1)

	def test_watermark_from_yesterday_allows_a_new_reminder_today(self):
		owner = self.make_user()
		journey = self.start_overdue_journey(owner)
		frappe.db.set_value(
			"Journey Task", journey.tasks[0].name, "last_reminder_sent_on", add_days(nowdate(), -1)
		)

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 1)

	def test_template_with_reminders_off_is_skipped(self):
		owner = self.make_user()
		self.start_overdue_journey(owner, send_reminders=0)

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 0)
		self.assertEqual(
			frappe.db.count("Notification Log", {"for_user": owner.name, "document_type": "Journey"}), 0
		)

	def test_not_yet_due_task_is_skipped(self):
		owner = self.make_user()
		self.start_overdue_journey(owner, days_overdue=-5)  # due 5 days from now, not overdue

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 0)

	def test_blocked_task_is_never_reminded(self):
		owner = self.make_user()
		employee = self.make_employee(user_id=owner.name)
		template = self.make_template(
			[
				{"title": "Step A", "owner_type": "Employee", "due_offset_days": -5, "action": "Task"},
				{
					"title": "Step B",
					"owner_type": "Employee",
					"due_offset_days": -5,
					"depends_on": "Step A",
					"action": "Task",
				},
			]
		)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		journey = frappe.get_doc("Journey", journey_name)
		step_b = next(t for t in journey.tasks if t.title == "Step B")
		self.assertEqual(step_b.status, "Blocked")

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 1)  # only Step A, which is Open

	def test_cancelled_journey_is_never_reminded(self):
		owner = self.make_user()
		journey = self.start_overdue_journey(owner)
		journeys.cancel_journey(journey.name)

		sent = journeys.send_overdue_reminders()
		self.assertEqual(sent, 0)

	def test_batch_cap_leaves_the_rest_for_the_next_run(self):
		owners = [self.make_user() for _ in range(3)]
		journeys_started = [self.start_overdue_journey(owner) for owner in owners]

		first_run = journeys.send_overdue_reminders(batch_size=2)
		self.assertEqual(first_run, 2)

		reminded = sum(
			1
			for journey in journeys_started
			if frappe.db.get_value("Journey Task", journey.tasks[0].name, "last_reminder_sent_on")
		)
		self.assertEqual(reminded, 2)

		second_run = journeys.send_overdue_reminders(batch_size=2)
		self.assertEqual(second_run, 1)

		reminded_after = sum(
			1
			for journey in journeys_started
			if frappe.db.get_value("Journey Task", journey.tasks[0].name, "last_reminder_sent_on")
		)
		self.assertEqual(reminded_after, 3)

	def test_daily_entrypoint_delegates_to_send_overdue_reminders(self):
		owner = self.make_user()
		self.start_overdue_journey(owner)

		journeys.daily()

		count = frappe.db.count("Notification Log", {"for_user": owner.name, "document_type": "Journey"})
		self.assertEqual(count, 1)


if __name__ == "__main__":
	unittest.main()
