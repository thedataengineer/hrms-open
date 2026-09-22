# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What Hubble hands RTB's Focus inbox (hrms/hr/focus.py). Plain unittest, rolled back, without
hrms.tests.utils or erpnext.tests.utils (importing either bootstraps and commits fixtures into the site).
Rows are made with the doctype's own validation switched off: the inbox reads only status, approver and
date columns, and a real Leave Application or Interview would need allocations, holiday lists and
appraisal chains that have nothing to do with what is read here."""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from hrms.hr import focus


class TestFocusUrgentItems(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		self.addCleanup(self._undo)
		no_commit = patch.object(frappe.db, "commit", side_effect=AssertionError("a test must not commit"))
		no_commit.start()
		self.addCleanup(no_commit.stop)
		self.today = nowdate()
		self.company = frappe.db.get_single_value("Global Defaults", "default_company")
		self.approver = self._make_user("focus-approver@example.com", "Approver", roles=["HR User"])
		self.other = self._make_user("focus-other@example.com", "Other", roles=["HR User"])
		self.nobody = self._make_user("focus-nobody@example.com", "Nobody", roles=[])
		self.employee = self._make_employee("Ash")

	def _undo(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")

	def _make_user(self, email, first_name, roles):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name,
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		)
		return user.insert(ignore_permissions=True)

	def _make_employee(self, first_name):
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": first_name,
				"company": self.company,
				"gender": "Female",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

	def _row(self, doctype, submit=False, **fields):
		doc = frappe.get_doc({"doctype": doctype, **fields})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		if submit:
			frappe.db.set_value(doctype, doc.name, "docstatus", 1, update_modified=False)
		return doc

	def _names(self, user):
		frappe.set_user(user)
		try:
			return [(row["doctype"], row["name"]) for row in focus.urgent_items(user, self.today)]
		finally:
			frappe.set_user("Administrator")

	def test_the_hook_is_registered(self):
		self.assertIn("hrms.hr.focus.urgent_items", frappe.get_hooks("focus_urgent_items"))

	def test_an_open_leave_that_has_started_waits_on_its_approver_only(self):
		leave = self._row(
			"Leave Application",
			employee=self.employee.name,
			employee_name="Ash",
			leave_type="Casual Leave",
			from_date=add_days(self.today, -1),
			to_date=self.today,
			status="Open",
			leave_approver=self.approver.name,
			company=self.company,
		)
		self.assertIn(("Leave Application", leave.name), self._names(self.approver.name))
		self.assertNotIn(("Leave Application", leave.name), self._names(self.other.name))

		frappe.set_user(self.approver.name)
		row = next(r for r in focus.urgent_items(self.approver.name, self.today) if r["name"] == leave.name)
		frappe.set_user("Administrator")
		self.assertEqual(row["title"], "Casual Leave: Ash")
		self.assertEqual(row["counterparty"], "Ash")
		self.assertEqual(str(row["due_date"]), add_days(self.today, -1))

	def test_a_leave_starting_later_or_already_decided_is_not_urgent(self):
		later = self._row(
			"Leave Application",
			employee=self.employee.name,
			leave_type="Casual Leave",
			from_date=add_days(self.today, 3),
			to_date=add_days(self.today, 3),
			status="Open",
			leave_approver=self.approver.name,
			company=self.company,
		)
		approved = self._row(
			"Leave Application",
			employee=self.employee.name,
			leave_type="Casual Leave",
			from_date=add_days(self.today, -2),
			to_date=self.today,
			status="Approved",
			leave_approver=self.approver.name,
			company=self.company,
		)
		names = self._names(self.approver.name)
		self.assertNotIn(("Leave Application", later.name), names)
		self.assertNotIn(("Leave Application", approved.name), names)

	def test_a_draft_claim_waits_on_its_approver(self):
		claim = self._row(
			"Expense Claim",
			employee=self.employee.name,
			employee_name="Ash",
			posting_date=self.today,
			approval_status="Draft",
			status="Draft",
			expense_approver=self.approver.name,
			company=self.company,
		)
		self.assertIn(("Expense Claim", claim.name), self._names(self.approver.name))
		self.assertNotIn(("Expense Claim", claim.name), self._names(self.other.name))

	def test_a_pending_interview_today_waits_on_each_of_its_interviewers(self):
		interview = self._row(
			"Interview",
			submit=True,
			job_applicant="Bay",
			status="Pending",
			scheduled_on=self.today,
			interview_details=[{"interviewer": self.approver.name}, {"interviewer": self.other.name}],
		)
		for user in (self.approver.name, self.other.name):
			names = self._names(user)
			self.assertIn(("Interview", interview.name), names)
			self.assertEqual(names.count(("Interview", interview.name)), 1)
		self.assertNotIn(("Interview", interview.name), self._names(self.nobody.name))

	def test_someone_with_no_hr_role_and_nothing_waiting_gets_an_empty_list_not_an_error(self):
		# Hubble lets an approver read what waits on them whatever their roles; a person with no roles
		# and nothing waiting must simply get nothing, whichever doctype refuses them
		self._row(
			"Leave Application",
			employee=self.employee.name,
			leave_type="Casual Leave",
			from_date=self.today,
			to_date=self.today,
			status="Open",
			leave_approver=self.approver.name,
			company=self.company,
		)
		self.assertEqual(self._names(self.nobody.name), [])

	def test_every_row_has_the_shape_the_inbox_expects(self):
		self._row(
			"Leave Application",
			employee=self.employee.name,
			leave_type="Casual Leave",
			from_date=self.today,
			to_date=self.today,
			status="Open",
			leave_approver=self.approver.name,
			company=self.company,
		)
		frappe.set_user(self.approver.name)
		rows = focus.urgent_items(self.approver.name, self.today)
		frappe.set_user("Administrator")
		self.assertTrue(rows)
		for row in rows:
			self.assertEqual(set(row), {"doctype", "name", "title", "counterparty", "due_date"})
			for key in ("doctype", "name", "title"):
				self.assertIsInstance(row[key], str)
			self.assertNotIn("ADHD", row["title"])


if __name__ == "__main__":
	unittest.main()
