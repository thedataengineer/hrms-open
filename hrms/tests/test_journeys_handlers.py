# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Tests for the Journeys event handlers (hrms.hr.journeys.on_employee_after_insert,
on_employee_separation_on_submit, on_employee_promotion_on_submit, on_employee_transfer_on_submit): they
are plain functions, not wired into hooks.py by this worker (see the shared brief and the final report),
so they are called directly here exactly as the lead's hooks.py entries will call them: `handler(doc,
method=None)`.

All three handlers are exercised through a real, minimally-filled, actually-submitted document of the
matching type (Employee Separation / Promotion / Transfer) rather than a hand-built stand-in: Journey's
`triggered_by` is a Dynamic Link (`hrms/hr/doctype/journey/journey.json`), and Frappe validates a Dynamic
Link against a document that really exists on `insert()` — a fake name would make `start_journey` raise a
LinkValidationError (caught and logged by the handler's own try/except, so the failure would otherwise be
silent). Mandatory fields below are verified against each DocType's JSON:
- Employee Separation: employee, company, boarding_begins_on (its on_submit is inherited from
  EmployeeBoardingController, which creates a Project; it also needs a Holiday List it can resolve for the
  employee, hence `holiday_list` is set on that one fixture).
- Employee Promotion: employee, promotion_date only (`promotion_details` is not mandatory here).
- Employee Transfer: employee, transfer_date, transfer_details (this one **is** a mandatory table — at
  least one row is required, confirmed empirically against the real controller).
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from hrms.hr import journeys


class JourneysHandlersTestCase(unittest.TestCase):
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
		fields.setdefault("first_name", f"Journeys Handler Test {uid}")
		fields.setdefault("company", self.company)
		fields.setdefault("date_of_joining", nowdate())
		fields.setdefault("status", "Active")
		fields.setdefault("gender", "Female")
		fields.setdefault("date_of_birth", add_days(nowdate(), -365 * 25))
		return frappe.get_doc({"doctype": "Employee", **fields}).insert(ignore_permissions=True)

	def make_template(self, journey_type, trigger, steps=None, **fields):
		uid = self._uid()
		fields.setdefault("title", f"Journeys Handler Test Template {uid}")
		fields.setdefault("company", self.company)
		steps = steps if steps is not None else [{"title": "Step A", "owner_type": "HR", "action": "Task"}]
		return frappe.get_doc(
			{
				"doctype": "Journey Template",
				"journey_type": journey_type,
				"trigger": trigger,
				"steps": steps,
				**fields,
			}
		).insert(ignore_permissions=True)


class TestEmployeeJoinedHandler(JourneysHandlersTestCase):
	def test_starts_matching_onboarding_journey(self):
		template = self.make_template("Onboarding", "Employee Joined")
		employee = self.make_employee()

		journeys.on_employee_after_insert(employee)

		self.assertTrue(
			frappe.db.exists("Journey", {"journey_template": template.name, "employee": employee.name})
		)

	def test_is_idempotent_across_repeated_calls(self):
		self.make_template("Onboarding", "Employee Joined")
		employee = self.make_employee()

		journeys.on_employee_after_insert(employee)
		journeys.on_employee_after_insert(employee)

		self.assertEqual(frappe.db.count("Journey", {"employee": employee.name}), 1)

	def test_disabled_template_does_not_start(self):
		self.make_template("Onboarding", "Employee Joined", disabled=1)
		employee = self.make_employee()

		journeys.on_employee_after_insert(employee)

		self.assertEqual(frappe.db.count("Journey", {"employee": employee.name}), 0)

	def test_wrong_trigger_does_not_start(self):
		self.make_template("Onboarding", "Manual")
		employee = self.make_employee()

		journeys.on_employee_after_insert(employee)

		self.assertEqual(frappe.db.count("Journey", {"employee": employee.name}), 0)

	def test_department_filter_excludes_non_matching_employee(self):
		department = frappe.get_all(
			"Department", filters={"company": self.company, "disabled": 0}, limit_page_length=1
		)
		if not department:
			self.skipTest("no Department fixture available on this site for the test company")
		self.make_template("Onboarding", "Employee Joined", department=department[0].name)
		employee = self.make_employee()  # no department set: cannot match a department-scoped template

		journeys.on_employee_after_insert(employee)

		self.assertEqual(frappe.db.count("Journey", {"employee": employee.name}), 0)

	def test_a_broken_template_does_not_block_the_caller(self):
		"""A template whose steps fail validation (e.g. a cyclical dependency, forced past validate())
		must never prevent the Employee record itself from being usable — the handler logs and swallows it."""
		# the employee first: hooks.py runs this handler on every Employee insert, and a template made
		# beforehand would already have started a (working) journey at that moment
		employee = self.make_employee()
		template = self.make_template(
			"Onboarding",
			"Employee Joined",
			steps=[{"title": "Step A", "owner_type": "HR", "action": "Task"}],
		)

		with patch.object(journeys, "start_journey", side_effect=RuntimeError("boom")):
			journeys.on_employee_after_insert(employee)  # must not raise

		self.assertEqual(frappe.db.count("Journey", {"employee": employee.name}), 0)
		del template  # unused beyond triggering a match; keeps the fixture's intent obvious


class TestEmployeeSeparationHandler(JourneysHandlersTestCase):
	def test_submitting_a_separation_starts_the_offboarding_journey(self):
		template = self.make_template("Offboarding", "Employee Separation")
		# EmployeeBoardingController.on_submit needs a Holiday List it can resolve for the employee; this
		# fork resolves it through a submitted "Holiday List Assignment" (verified in
		# hrms/utils/holiday_list.py: get_holiday_list_for_employee), not a plain Employee.holiday_list field
		holiday_lists = frappe.get_all(
			"Holiday List",
			filters={"from_date": ("<=", nowdate()), "to_date": (">=", nowdate())},
			fields=["name", "from_date"],
			limit_page_length=1,
		)
		if not holiday_lists:
			self.skipTest("no Holiday List covering today is available on this site")
		employee = self.make_employee()
		frappe.get_doc(
			{
				"doctype": "Holiday List Assignment",
				"applicable_for": "Employee",
				"assigned_to": employee.name,
				"holiday_list": holiday_lists[0].name,
				"from_date": holiday_lists[0].from_date,
			}
		).insert(ignore_permissions=True).submit()

		separation = frappe.get_doc(
			{
				"doctype": "Employee Separation",
				"employee": employee.name,
				"company": self.company,
				"boarding_begins_on": nowdate(),
				"resignation_letter_date": nowdate(),
			}
		).insert(ignore_permissions=True)
		separation.submit()

		journeys.on_employee_separation_on_submit(separation)

		self.assertTrue(
			frappe.db.exists(
				"Journey",
				{
					"journey_template": template.name,
					"employee": employee.name,
					"triggered_by_doctype": "Employee Separation",
					"triggered_by": separation.name,
				},
			)
		)


class TestEmployeePromotionHandler(JourneysHandlersTestCase):
	def test_starts_matching_promotion_journey(self):
		template = self.make_template("Promotion", "Employee Promotion")
		employee = self.make_employee()

		promotion = frappe.get_doc(
			{"doctype": "Employee Promotion", "employee": employee.name, "promotion_date": nowdate()}
		).insert(ignore_permissions=True)
		promotion.submit()

		journeys.on_employee_promotion_on_submit(promotion)

		self.assertTrue(
			frappe.db.exists(
				"Journey",
				{
					"journey_template": template.name,
					"employee": employee.name,
					"triggered_by_doctype": "Employee Promotion",
					"triggered_by": promotion.name,
				},
			)
		)


class TestEmployeeTransferHandler(JourneysHandlersTestCase):
	def _submit_transfer(self, employee):
		transfer = frappe.get_doc(
			{
				"doctype": "Employee Transfer",
				"employee": employee.name,
				"transfer_date": nowdate(),
				"company": self.company,
				# transfer_details is a mandatory table; an empty-value row is enough to satisfy it and
				# is a no-op for update_employee_work_history (it only acts on non-empty details)
				"transfer_details": [
					{"property": "Department", "fieldname": "department", "current": "", "new": ""}
				],
			}
		).insert(ignore_permissions=True)
		transfer.submit()
		return transfer

	def test_starts_matching_transfer_journey(self):
		template = self.make_template("Transfer", "Employee Transfer")
		employee = self.make_employee()

		transfer = self._submit_transfer(employee)
		journeys.on_employee_transfer_on_submit(transfer)

		self.assertTrue(
			frappe.db.exists(
				"Journey",
				{
					"journey_template": template.name,
					"employee": employee.name,
					"triggered_by_doctype": "Employee Transfer",
					"triggered_by": transfer.name,
				},
			)
		)

	def test_missing_employee_does_not_raise(self):
		fake_transfer = frappe._dict(doctype="Employee Transfer", name="HR-EMP-TRN-TEST-0002", employee=None)
		journeys.on_employee_transfer_on_submit(fake_transfer)  # must not raise


if __name__ == "__main__":
	unittest.main()
