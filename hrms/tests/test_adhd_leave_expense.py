# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# Server side of ADHD-058's "Balance at a Glance" card (hrms/hr/services/adhd_leave_balance.py). ADHD-059 has
# no server code of its own: the client uses the already permission-checked `frappe.client.get_list`, whose
# access control is verified once here too (see test_file_list_is_scoped_to_documents_the_caller_may_read).
#
# Plain unittest, rolled back, and deliberately without hrms.tests.utils / erpnext.tests.utils (importing
# either bootstraps and commits a large amount of fixture data into whatever site this runs on). Every test
# also patches frappe.db.commit to raise, so a test can never leave a row behind even if a bug tried to.
#
# Needs a site with a Company and a Fiscal Year covering today (the dev site has one: see
# /Users/yakarteek/.claude/projects/.../memory/local-erpnext-instance.md). Field names and behaviour were
# checked against:
#   - hrms/hr/doctype/leave_application/leave_application.py (get_leave_glance wraps get_leave_details,
#     get_leave_balance_on, get_number_of_leave_days, validate_leave_access, is_lwp)
#   - hrms/hr/doctype/leave_type/leave_type.json (allow_negative, is_lwp)
#   - hrms/utils/holiday_list.py (a Leave Application cannot submit without a Holiday List Assignment)
#   - erpnext/setup/doctype/employee/employee.json (Employee permissions: role "Employee" reads all records,
#     so the "cannot read" test uses a user with no roles at all)

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from hrms.hr.services import adhd_leave_balance as glance

DENIED = {"available": False, "reason": "not_permitted"}


class TestAdhdLeaveBalance(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.get_all("Company", pluck="name", limit_page_length=1)[0]
		)
		cls.year_start = getdate(today()).replace(month=1, day=1)
		cls.year_end = getdate(today()).replace(month=12, day=31)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		# a bug that tried to persist test data into the real site must fail the test loudly, not silently
		self._commit_patch = patch.object(
			frappe.db, "commit", side_effect=AssertionError("test tried to commit to the database")
		)
		self._commit_patch.start()

	def tearDown(self):
		self._commit_patch.stop()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = False

	# --- fixtures ---

	def make_employee(self, suffix):
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "AdhdGlance",
				"last_name": suffix,
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": self.company,
				"status": "Active",
			}
		).insert()

	def make_holiday_list_assignment(self, employee):
		holiday_list = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": f"ADHD Glance HL {employee.name}",
				"from_date": self.year_start,
				"to_date": self.year_end,
			}
		).insert()
		assignment = frappe.get_doc(
			{
				"doctype": "Holiday List Assignment",
				"holiday_list": holiday_list.name,
				"applicable_for": "Employee",
				"assigned_to": employee.name,
				"from_date": self.year_start,
			}
		).insert()
		assignment.submit()
		return holiday_list

	def make_leave_type(self, name, **fields):
		return frappe.get_doc(
			{"doctype": "Leave Type", "leave_type_name": name, "include_holiday": 1, **fields}
		).insert()

	def make_allocation(self, employee, leave_type, days=10):
		allocation = frappe.get_doc(
			{
				"doctype": "Leave Allocation",
				"employee": getattr(employee, "name", employee),
				"leave_type": leave_type,
				"from_date": self.year_start,
				"to_date": self.year_end,
				"new_leaves_allocated": days,
				"total_leaves_allocated": days,
				"company": self.company,
			}
		).insert()
		allocation.submit()
		return allocation

	def make_application(self, employee, leave_type, from_date, to_date, submit=False):
		application = frappe.get_doc(
			{
				"doctype": "Leave Application",
				"employee": employee,
				"leave_type": leave_type,
				"from_date": from_date,
				"to_date": to_date,
				"company": self.company,
				"leave_approver": "Administrator",
				"status": "Approved",
			}
		).insert()
		if submit:
			application.submit()
		return application

	# --- get_leave_balance_on / get_leave_details are reused, not re-derived ---

	def test_matches_get_leave_details_and_get_leave_balance_on(self):
		employee = self.make_employee("Reuse")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Reuse")
		self.make_allocation(employee, leave_type.name, days=10)

		result = glance.get_leave_glance(employee.name, leave_type.name, today(), add_days(today(), 2))

		details = glance.get_leave_details(employee.name, getdate(today()))
		entry = details["leave_allocation"][leave_type.name]
		self.assertEqual(result["total_leaves"], entry["total_leaves"])
		self.assertEqual(result["used"], entry["leaves_taken"])
		self.assertEqual(result["remaining"], entry["remaining_leaves"])

		balance = glance.get_leave_balance_on(
			employee.name,
			leave_type.name,
			today(),
			add_days(today(), 2),
			consider_all_leaves_in_the_allocation_period=True,
			for_consumption=True,
		)
		self.assertEqual(result["usable"], balance["leave_balance_for_consumption"])
		self.assertEqual(result["state"], "ok")
		self.assertTrue(result["available"])

	def test_days_match_get_number_of_leave_days_with_a_half_day(self):
		employee = self.make_employee("HalfDay")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance HalfDay")
		self.make_allocation(employee, leave_type.name, days=10)

		expected = glance.get_number_of_leave_days(
			employee.name, leave_type.name, today(), add_days(today(), 1), half_day=1, half_day_date=today()
		)
		result = glance.get_leave_glance(
			employee.name,
			leave_type.name,
			today(),
			add_days(today(), 1),
			half_day=1,
			half_day_date=today(),
		)
		self.assertEqual(result["days"], expected)

	def test_leave_type_without_allocation_is_no_allocation_when_negative_not_allowed(self):
		employee = self.make_employee("NoAlloc")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance No Allocation")

		result = glance.get_leave_glance(employee.name, leave_type.name, today(), add_days(today(), 2))
		self.assertEqual(result["state"], "no_allocation")
		self.assertFalse(result["allocated"])
		self.assertEqual(result["total_leaves"], 0.0)

	def test_allow_negative_leave_type_warns_instead_of_blocking(self):
		employee = self.make_employee("Negative")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Negative", allow_negative=1)
		self.make_allocation(employee, leave_type.name, days=2)

		result = glance.get_leave_glance(employee.name, leave_type.name, today(), add_days(today(), 5))
		self.assertEqual(result["state"], "negative_allowed")
		self.assertTrue(result["allow_negative"])
		self.assertEqual(result["days"], 6.0)
		# HRMS's own check (validate_balance_leaves) would only msgprint a warning here, never block the save
		self.assertLess(result["usable"], result["days"])

	def test_leave_without_pay_tracks_no_balance(self):
		employee = self.make_employee("LWP")
		self.make_holiday_list_assignment(employee)
		lwp = frappe.db.get_value("Leave Type", {"is_lwp": 1}, "name")
		self.assertTrue(lwp, "the site must ship a Leave Without Pay type")

		result = glance.get_leave_glance(employee.name, lwp, today(), add_days(today(), 2))
		self.assertEqual(result["state"], "lwp")
		self.assertTrue(result["is_lwp"])
		self.assertIsNone(result["usable"])

	def test_new_unsaved_form_has_no_leave_application_name(self):
		employee = self.make_employee("Unsaved")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Unsaved")
		self.make_allocation(employee, leave_type.name, days=10)

		result = glance.get_leave_glance(
			employee.name, leave_type.name, today(), add_days(today(), 1), leave_application=None
		)
		self.assertEqual(result["state"], "ok")
		self.assertEqual(result["days"], 2.0)

	def test_submitted_application_is_recorded_and_counted_no_warning_state(self):
		employee = self.make_employee("Submitted")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Submitted")
		self.make_allocation(employee, leave_type.name, days=10)
		application = self.make_application(
			employee.name, leave_type.name, today(), add_days(today(), 1), submit=True
		)

		result = glance.get_leave_glance(
			employee.name,
			leave_type.name,
			today(),
			add_days(today(), 1),
			leave_application=application.name,
		)
		self.assertEqual(result["state"], "recorded")
		self.assertTrue(result["counted"])
		self.assertEqual(result["days"], application.total_leave_days)
		# a submitted application is read-only: nothing here is a warning about it
		self.assertNotIn(result["state"], (glance.STATE_SHORT, glance.STATE_NEGATIVE_ALLOWED))

	def test_cancelled_application_is_recorded_but_not_counted(self):
		employee = self.make_employee("Cancelled")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Cancelled")
		self.make_allocation(employee, leave_type.name, days=10)
		application = self.make_application(
			employee.name, leave_type.name, today(), add_days(today(), 1), submit=True
		)
		application.cancel()

		result = glance.get_leave_glance(
			employee.name,
			leave_type.name,
			today(),
			add_days(today(), 1),
			leave_application=application.name,
		)
		self.assertEqual(result["state"], "recorded")
		self.assertFalse(result["counted"])
		self.assertEqual(result["remaining"], 10.0)  # the cancelled days were given back

	def test_employee_the_caller_cannot_read_returns_unavailable_with_no_numbers(self):
		employee = self.make_employee("Denied")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Denied")
		self.make_allocation(employee, leave_type.name, days=10)

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "adhd-glance-denied@example.com",
				"first_name": "Denied",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		# another app on the bench may give every new user a role of its own (Suite adds "Suite User"); what
		# this test needs is that none of them lets the person read an Employee
		self.assertFalse(frappe.has_permission("Employee", "read", user=user.name))

		frappe.set_user(user.name)
		try:
			result = glance.get_leave_glance(employee.name, leave_type.name, today(), add_days(today(), 1))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result, DENIED)

	def test_leave_type_permission_alone_does_not_bypass_the_employee_specific_gate(self):
		# On this site, reading Leave Type and reading any Employee happen to require the same roles, so the
		# test above cannot tell which of the two checks in get_leave_glance actually did the denying. Force
		# Leave Type reads open (as some other site's role setup might) and prove the employee-specific gate
		# (_can_read / validate_leave_access) still denies on its own.
		employee = self.make_employee("LeaveTypeOnly")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance LeaveTypeOnly")
		self.make_allocation(employee, leave_type.name, days=10)

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "adhd-glance-leavetype-only@example.com",
				"first_name": "LeaveTypeOnly",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

		real_has_permission = frappe.has_permission

		def leave_type_always_readable(doctype, *args, **kwargs):
			if doctype == "Leave Type":
				return True
			return real_has_permission(doctype, *args, **kwargs)

		frappe.set_user(user.name)
		try:
			with patch("frappe.has_permission", side_effect=leave_type_always_readable):
				result = glance.get_leave_glance(
					employee.name, leave_type.name, today(), add_days(today(), 1)
				)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result, DENIED)

	def test_unknown_employee_is_also_denied_not_distinguishable_from_no_access(self):
		result = glance.get_leave_glance(
			"ADHD-GLANCE-DOES-NOT-EXIST", "Casual Leave", today(), add_days(today(), 1)
		)
		self.assertEqual(result, DENIED)

	def test_overlong_employee_name_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			glance.get_leave_glance("x" * 200, "Casual Leave", today())

	def test_blank_from_date_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			glance.get_leave_glance("HR-EMP-00001", "Casual Leave", "")

	def test_leave_application_belonging_to_a_different_employee_does_not_widen_access(self):
		employee_a = self.make_employee("OwnerA")
		employee_b = self.make_employee("OwnerB")
		self.make_holiday_list_assignment(employee_a)
		self.make_holiday_list_assignment(employee_b)
		leave_type = self.make_leave_type("ADHD Glance Owner")
		self.make_allocation(employee_a, leave_type.name, days=10)
		self.make_allocation(employee_b, leave_type.name, days=10)
		application_b = self.make_application(
			employee_b.name, leave_type.name, today(), add_days(today(), 1), submit=True
		)

		# asking about employee_a while passing employee_b's leave_application name must not borrow its record
		result = glance.get_leave_glance(
			employee_a.name,
			leave_type.name,
			today(),
			add_days(today(), 1),
			leave_application=application_b.name,
		)
		self.assertEqual(result["state"], "ok")  # employee_a's own draft-style figures, not employee_b's

	def test_whitelisted_signature_accepts_the_browser_s_string_arguments(self):
		# hooks.py sets require_type_annotated_api_methods: every argument must carry a type, and a browser
		# sends everything as a string. Call it the way the browser would.
		from frappe.utils.typing_validations import validate_argument_types

		employee = self.make_employee("Typed")
		self.make_holiday_list_assignment(employee)
		leave_type = self.make_leave_type("ADHD Glance Typed")
		self.make_allocation(employee, leave_type.name, days=10)

		wrapped = validate_argument_types(
			glance.get_leave_glance.__wrapped__, apply_condition=lambda: True, force_types=True
		)
		result = wrapped(
			employee=employee.name,
			leave_type=leave_type.name,
			from_date=today(),
			to_date=add_days(today(), 1),
			half_day="0",
			half_day_date="",
			leave_application="",
		)
		self.assertEqual(result["state"], "ok")

	# --- ADHD-059 has no server file of its own: the client calls frappe.client.get_list directly. Prove that
	# stays permission-checked, since that is the whole reason a bespoke wrapper was judged unnecessary. ---

	def test_file_list_is_scoped_to_documents_the_caller_may_read(self):
		employee = self.make_employee("FileScope")
		claim = frappe.get_doc(
			{
				"doctype": "Expense Claim",
				"employee": employee.name,
				"company": self.company,
				"posting_date": today(),
				"currency": frappe.get_cached_value("Company", self.company, "default_currency"),
				"exchange_rate": 1,
				"expenses": [
					{
						"expense_date": today(),
						"expense_type": self._expense_type(),
						"amount": 10,
						"sanctioned_amount": 10,
					}
				],
			}
		).insert()
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "receipt.txt",
				"content": "not a real receipt",
				"attached_to_doctype": "Expense Claim",
				"attached_to_name": claim.name,
				"is_private": 1,
			}
		).insert()

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "adhd-glance-filescope@example.com",
				"first_name": "FileScope",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

		frappe.set_user(user.name)
		try:
			files = frappe.client.get_list(
				doctype="File",
				filters={"attached_to_doctype": "Expense Claim", "attached_to_name": claim.name},
				fields=["name"],
				limit_page_length=100,
			)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(files, [], "a user who cannot read the claim must not see its receipts either")
		self.assertTrue(
			frappe.db.exists("File", file_doc.name)
		)  # the file itself is real, just hidden from them

	def _expense_type(self):
		account = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		return (
			frappe.get_doc(
				{
					"doctype": "Expense Claim Type",
					"expense_type": "ADHD Glance Test Type",
					"accounts": [{"company": self.company, "default_account": account}],
				}
			)
			.insert()
			.name
		)


if __name__ == "__main__":
	unittest.main()
