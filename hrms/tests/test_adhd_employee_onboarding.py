# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Server-side tests for the Focus "Onboard New Employee" wizard (ADHD-014).

Plain unittest, no HRMSTestSuite/ERPNextTestSuite and no import of hrms.tests.utils or erpnext.tests.utils
(both bootstrap and COMMIT a large amount of fixture data into whatever site they run against). Every record
this suite needs -- a Company, a Salary Structure, a Leave Policy -- is built inside the test itself, mail is
muted, and frappe.db.commit is patched to raise so nothing here can ever persist. Run with a copy of the
scratchpad's run_tests.py-style runner, which rolls the whole transaction back and prints "site data
unchanged". The dev site's demo company ("Yadavilli Solutions") has no employees and is never assumed to
exist or referenced by name.

Doctype choices (checked against the real HRMS / ERPNext source, see hrms/hr/services/adhd_employee_onboarding.py
for the full note with file references):
  * Employee Education is the `education` child table of Employee (istable=1 in employee_education.json) --
    never created as its own document.
  * Address is a real, separately-created document linked to the Employee via its `links` Dynamic Link table.
  * Salary Structure Assignment is a real, submittable document.
  * Leave is granted through a Leave Policy Assignment (submitted), whose own on_submit
    (LeavePolicyAssignment.grant_leave_alloc_for_employee) creates the Leave Allocation(s) -- a raw,
    hand-built Leave Allocation is never inserted directly.
"""

import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from hrms.hr.services import adhd_employee_onboarding as service

SOURCE_FILE = service.__file__


def _read_source() -> str:
	with open(SOURCE_FILE) as handle:
		return handle.read()


class EmployeeOnboardingWizardTestCase(unittest.TestCase):
	"""Builds one private, disposable Company (+ a submitted Salary Structure, Leave Type-backed Leave Policy,
	Department, Country-linked Gender) per test class run, and rolls every bit of it back in tearDown."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		cls._commit_patcher = patch.object(
			frappe.db,
			"commit",
			side_effect=RuntimeError("frappe.db.commit() must never run in this test suite"),
		)
		cls._commit_patcher.start()

		suffix = frappe.generate_hash(length=6)
		cls.company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": f"Onboarding Wizard Test Co {suffix}",
				"abbr": f"OW{suffix[:3]}".upper(),
				"default_currency": "USD",
				"country": "United States",
			}
		).insert()

		cls.other_company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": f"Onboarding Wizard Other Co {suffix}",
				"abbr": f"OX{suffix[:3]}".upper(),
				"default_currency": "USD",
				"country": "United States",
			}
		).insert()

		cls.department = frappe.get_all(
			"Department", filters={"company": cls.company.name}, pluck="name", limit=1
		)
		cls.department = cls.department[0] if cls.department else None

		earning_component = frappe.get_all(
			"Salary Component", filters={"type": "Earning"}, pluck="name", limit=1
		)
		cls.salary_structure = frappe.get_doc(
			{
				"doctype": "Salary Structure",
				"name": f"Onboarding Wizard Test SS {suffix}",
				"company": cls.company.name,
				"payroll_frequency": "Monthly",
				"currency": "USD",
				"is_active": "Yes",
				"earnings": [{"salary_component": earning_component[0], "amount": 1000}]
				if earning_component
				else [],
			}
		)
		cls.salary_structure.insert()
		cls.salary_structure.submit()

		# a draft structure (never submitted) to prove the wizard refuses it
		cls.draft_salary_structure = frappe.get_doc(
			{
				"doctype": "Salary Structure",
				"name": f"Onboarding Wizard Draft SS {suffix}",
				"company": cls.company.name,
				"payroll_frequency": "Monthly",
				"currency": "USD",
				"is_active": "Yes",
			}
		).insert()

		leave_type = frappe.get_all("Leave Type", filters={"is_lwp": 0}, pluck="name", limit=1)[0]
		cls.leave_type = leave_type
		cls.leave_policy = frappe.get_doc(
			{
				"doctype": "Leave Policy",
				"title": f"Onboarding Wizard Test LP {suffix}",
				"leave_policy_details": [{"leave_type": leave_type, "annual_allocation": 12}],
			}
		)
		cls.leave_policy.insert()
		cls.leave_policy.submit()

		cls.gender = frappe.get_all("Gender", pluck="name", limit=1)[0]
		cls.suffix = suffix

	@classmethod
	def tearDownClass(cls):
		cls._commit_patcher.stop()
		frappe.db.rollback()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ---- helpers -------------------------------------------------------------------------------------

	def _make_user(self, email, roles):
		if frappe.db.exists("User", email):
			return frappe.get_doc("User", email)
		return frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"roles": [{"role": role} for role in roles],
			}
		).insert()

	def _payload(self, **overrides):
		base = {
			"who": {
				"first_name": "Ada",
				"last_name": "Lovelace",
				"gender": self.gender,
				"date_of_birth": "1990-01-01",
			},
			"job": {"company": self.company.name, "date_of_joining": "2026-01-05"},
			"education": [{"qualification": "BSc", "school_univ": "MIT", "year_of_passing": 2012}],
			"address": {
				"address_type": "Office",
				"address_line1": "1 Main St",
				"city": "Austin",
				"country": "United States",
			},
			"salary": {
				"salary_structure": self.salary_structure.name,
				"from_date": "2026-01-05",
				"base": 5000,
			},
			"leave": {"leave_policy": self.leave_policy.name, "assignment_based_on": "Joining Date"},
			"skipped": [],
			"options": {"submit": True},
		}
		base.update(overrides)
		return base

	def _counts(self):
		return {
			"Employee": frappe.db.count("Employee", {"company": self.company.name}),
			"Address": frappe.db.count("Address"),
			"Salary Structure Assignment": frappe.db.count(
				"Salary Structure Assignment", {"company": self.company.name}
			),
			"Leave Policy Assignment": frappe.db.count("Leave Policy Assignment"),
			"Leave Allocation": frappe.db.count("Leave Allocation"),
			"ToDo": frappe.db.count("ToDo"),
		}

	# ---- the happy path, and what it actually writes --------------------------------------------------

	def test_happy_path_creates_employee_address_salary_and_leave_via_leave_policy_assignment(self):
		payload = self._payload()
		before = self._counts()

		result = service.create(json.dumps(payload))

		self.assertTrue(result["ok"], result)
		employee = frappe.get_doc("Employee", result["employee"])
		self.assertEqual(employee.employee_name, "Ada Lovelace")
		self.assertEqual(employee.company, self.company.name)

		# Employee Education landed as a CHILD ROW of Employee, never as its own document: the row exists
		# (child rows are real table rows) but only as part of the Employee -- istable=1, parented to it,
		# with no permission entries or route of its own.
		self.assertEqual(len(employee.education), 1)
		self.assertEqual(employee.education[0].qualification, "BSc")
		self.assertEqual(employee.education[0].parenttype, "Employee")
		self.assertEqual(employee.education[0].parent, employee.name)
		self.assertTrue(frappe.get_meta("Employee Education").istable)

		# Address exists and is linked to the employee via its own `links` Dynamic Link table.
		address_doc = next(d for d in result["documents"] if d["doctype"] == "Address")
		address = frappe.get_doc("Address", address_doc["name"])
		self.assertTrue(
			any(link.link_doctype == "Employee" and link.link_name == employee.name for link in address.links)
		)

		# Salary Structure Assignment was submitted (options.submit = True).
		ssa_doc = next(d for d in result["documents"] if d["doctype"] == "Salary Structure Assignment")
		ssa = frappe.get_doc("Salary Structure Assignment", ssa_doc["name"])
		self.assertEqual(ssa.docstatus, 1)
		self.assertEqual(ssa.employee, employee.name)

		# Leave: a Leave Policy Assignment was submitted, and ITS OWN on_submit created the Leave Allocation --
		# nothing here builds a Leave Allocation by hand.
		lpa_doc = next(d for d in result["documents"] if d["doctype"] == "Leave Policy Assignment")
		lpa = frappe.get_doc("Leave Policy Assignment", lpa_doc["name"])
		self.assertEqual(lpa.docstatus, 1)
		allocation_doc = next(d for d in result["documents"] if d["doctype"] == "Leave Allocation")
		allocation = frappe.get_doc("Leave Allocation", allocation_doc["name"])
		self.assertEqual(allocation.leave_policy_assignment, lpa.name)
		self.assertEqual(allocation.employee, employee.name)
		self.assertEqual(allocation.docstatus, 1)

		after = self._counts()
		self.assertEqual(after["Employee"] - before["Employee"], 1)
		self.assertEqual(after["Address"] - before["Address"], 1)
		self.assertEqual(after["Salary Structure Assignment"] - before["Salary Structure Assignment"], 1)
		self.assertEqual(after["Leave Policy Assignment"] - before["Leave Policy Assignment"], 1)
		self.assertGreaterEqual(after["Leave Allocation"] - before["Leave Allocation"], 1)

	def test_the_service_never_bypasses_permissions_with_ignore_permissions(self):
		source = _read_source()
		self.assertNotIn("ignore_permissions=True", source)
		self.assertNotIn("ignore_permissions = True", source)

	def test_never_builds_a_leave_allocation_document_directly(self):
		source = _read_source()
		insert_leave_start = source.index("def _insert_leave(")
		insert_leave_end = source.index("def _insert_reminders(")
		body = source[insert_leave_start:insert_leave_end]
		self.assertNotIn('"doctype": "Leave Allocation"', body)
		self.assertIn('"doctype": "Leave Policy Assignment"', body)

	# ---- atomicity: the whole point of the ticket's "one server call" rule -----------------------------

	def test_atomicity_a_forced_failure_on_the_last_step_leaves_nothing_behind(self):
		payload = self._payload(who={**self._payload()["who"], "first_name": "Grace", "last_name": "Hopper"})
		before = self._counts()

		original_insert_leave = service._insert_leave

		def boom(*args, **kwargs):
			raise frappe.ValidationError("Simulated failure for the atomicity test")

		service._insert_leave = boom
		try:
			result = service.create(json.dumps(payload))
		finally:
			service._insert_leave = original_insert_leave

		self.assertFalse(result["ok"])
		self.assertEqual(result["step"], "leave")
		self.assertIn("leave", result["message"].lower())
		self.assertEqual(self._counts(), before, "a failed create must leave the site exactly as it was")

		# and the transaction is not left broken: a normal create right after still works
		result2 = service.create(json.dumps(payload))
		self.assertTrue(result2["ok"], result2)
		self.assertEqual(self._counts()["Employee"], before["Employee"] + 1)

	def test_atomicity_a_forced_failure_on_the_first_step_also_leaves_nothing_behind(self):
		payload = self._payload(who={**self._payload()["who"], "first_name": "Alan", "last_name": "Turing"})
		before = self._counts()

		original_insert_employee = service._insert_employee

		def boom(*args, **kwargs):
			raise frappe.ValidationError("Simulated failure on the very first step")

		service._insert_employee = boom
		try:
			result = service.create(json.dumps(payload))
		finally:
			service._insert_employee = original_insert_employee

		self.assertFalse(result["ok"])
		self.assertEqual(result["step"], "employee")
		self.assertEqual(self._counts(), before)

	# ---- validation: names the field, writes nothing --------------------------------------------------

	def test_missing_required_field_is_named_and_nothing_is_written(self):
		payload = self._payload()
		payload["who"] = {
			"first_name": "No",
			"last_name": "Gender",
			"date_of_birth": "1990-01-01",
		}  # gender missing
		before = self._counts()

		checked = service.check(json.dumps(payload))
		self.assertFalse(checked["ok"])
		self.assertTrue(any(e["field"] == "gender" for e in checked["errors"]))

		result = service.create(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertEqual(self._counts(), before)

	def test_a_value_with_angle_brackets_is_rejected_not_silently_stripped(self):
		payload = self._payload()
		payload["who"]["first_name"] = "<b>Evil</b>"
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "first_name" for e in result["errors"]))
		self.assertEqual(frappe.db.count("Employee", {"first_name": "<b>Evil</b>"}), 0)

	def test_negative_base_pay_is_rejected(self):
		payload = self._payload()
		payload["salary"]["base"] = -100
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "base" for e in result["errors"]))

	def test_joining_date_far_in_the_future_is_rejected(self):
		payload = self._payload()
		payload["job"]["date_of_joining"] = "2099-01-01"
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "date_of_joining" for e in result["errors"]))

	def test_check_step_reports_only_the_requested_steps_errors(self):
		payload = self._payload()
		payload["who"] = {}  # broken
		payload["salary"]["base"] = -5  # also broken
		result = service.check(json.dumps(payload), step="salary")
		self.assertFalse(result["ok"])
		self.assertTrue(all(e["step"] == "salary" for e in result["errors"]))
		self.assertTrue(any(e["field"] == "base" for e in result["errors"]))

	def test_who_and_job_cannot_be_skipped(self):
		payload = self._payload(skipped=["who"])
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any("not a step that can be skipped" in e["message"] for e in result["errors"]))

	# ---- company scoping -------------------------------------------------------------------------------

	def test_department_from_a_different_company_is_rejected(self):
		other_department = frappe.get_all(
			"Department", filters={"company": self.other_company.name}, pluck="name", limit=1
		)
		if not other_department:
			self.skipTest("no seeded department for the throwaway second company on this site")
		payload = self._payload()
		payload["job"]["department"] = other_department[0]
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "department" for e in result["errors"]))

	def test_salary_structure_from_a_different_company_is_rejected(self):
		other_structure = frappe.get_doc(
			{
				"doctype": "Salary Structure",
				"name": f"Onboarding Wizard Other Co SS {self.suffix}",
				"company": self.other_company.name,
				"payroll_frequency": "Monthly",
				"currency": "USD",
				"is_active": "Yes",
			}
		).insert()
		other_structure.submit()
		payload = self._payload()
		payload["salary"]["salary_structure"] = other_structure.name
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "salary_structure" for e in result["errors"]))

	def test_unsubmitted_salary_structure_is_rejected(self):
		payload = self._payload()
		payload["salary"]["salary_structure"] = self.draft_salary_structure.name
		result = service.check(json.dumps(payload))
		self.assertFalse(result["ok"])
		self.assertTrue(any(e["field"] == "salary_structure" for e in result["errors"]))

	# ---- permissions -------------------------------------------------------------------------------------

	def test_a_user_without_create_permission_is_refused_and_nothing_is_written(self):
		user = self._make_user(f"onb.noperm.{self.suffix}@example.com", ["Employee"])
		before = self._counts()
		frappe.set_user(user.name)
		try:
			checked = service.check(json.dumps(self._payload()))
			self.assertFalse(checked["ok"])
			self.assertTrue(any("permission" in e["message"].lower() for e in checked["errors"]))

			created = service.create(json.dumps(self._payload()))
			self.assertFalse(created["ok"])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._counts(), before)

	def test_an_hr_user_can_complete_the_whole_flow(self):
		user = self._make_user(f"onb.hruser.{self.suffix}@example.com", ["HR User"])
		frappe.set_user(user.name)
		try:
			payload = self._payload(
				who={**self._payload()["who"], "first_name": "Hedy", "last_name": "Lamarr"}
			)
			result = service.create(json.dumps(payload))
			self.assertTrue(result["ok"], result)
		finally:
			frappe.set_user("Administrator")

	# ---- duplicate protection, skip -> reminder, leave period based assignment ---------------------------

	def test_duplicate_employee_answers_are_flagged_before_creating_a_second_record(self):
		payload = self._payload(
			who={**self._payload()["who"], "first_name": "Katherine", "last_name": "Johnson"}
		)
		first = service.create(json.dumps(payload))
		self.assertTrue(first["ok"], first)

		dup = service.check(json.dumps(payload))
		self.assertFalse(dup["ok"])
		self.assertTrue(any("already exists" in e["message"] for e in dup["errors"]))

	def test_skipping_optional_steps_creates_a_todo_reminder_for_each_one(self):
		payload = self._payload(
			who={**self._payload()["who"], "first_name": "Margaret", "last_name": "Hamilton"},
			education=[],
			address={},
			salary={},
			leave={},
			skipped=["education", "address", "salary", "leave"],
		)
		before = self._counts()
		result = service.create(json.dumps(payload))
		self.assertTrue(result["ok"], result)
		self.assertEqual(len(result["reminders"]), 4)

		after = self._counts()
		self.assertEqual(after["ToDo"] - before["ToDo"], 4)
		self.assertEqual(after["Salary Structure Assignment"], before["Salary Structure Assignment"])
		self.assertEqual(after["Leave Policy Assignment"], before["Leave Policy Assignment"])

		due = add_days(today(), service.REMINDER_DAYS)
		for reminder in result["reminders"]:
			todo = frappe.get_doc("ToDo", reminder["name"])
			self.assertEqual(str(todo.date), str(due))
			self.assertEqual(todo.reference_type, "Employee")
			self.assertEqual(todo.reference_name, result["employee"])
			self.assertIn(result["employee_name"], todo.description)

	def test_leave_period_based_assignment_uses_the_periods_own_dates(self):
		leave_period = frappe.get_doc(
			{
				"doctype": "Leave Period",
				"from_date": "2026-01-01",
				"to_date": "2026-12-31",
				"is_active": 1,
				"company": self.company.name,
			}
		).insert()
		payload = self._payload(
			who={**self._payload()["who"], "first_name": "Radia", "last_name": "Perlman"},
			leave={
				"leave_policy": self.leave_policy.name,
				"assignment_based_on": "Leave Period",
				"leave_period": leave_period.name,
			},
		)
		result = service.create(json.dumps(payload))
		self.assertTrue(result["ok"], result)
		lpa_doc = next(d for d in result["documents"] if d["doctype"] == "Leave Policy Assignment")
		lpa = frappe.get_doc("Leave Policy Assignment", lpa_doc["name"])
		self.assertEqual(str(lpa.effective_from), "2026-01-01")
		self.assertEqual(str(lpa.effective_to), "2026-12-31")

	# ---- type annotations: hooks.py sets require_type_annotated_api_methods for hrms ---------------------

	def test_whitelisted_endpoints_are_fully_type_annotated_and_callable_the_way_a_browser_calls_them(self):
		from frappe.utils.typing_validations import validate_argument_types

		from hrms.hr.page.employee_onboarding_wizard import employee_onboarding_wizard as page

		for fn in (page.get_context, page.check_step, page.create_employee_onboarding):
			with self.subTest(fn=fn.__name__):
				params = [p for p in fn.__code__.co_varnames[: fn.__code__.co_argcount]]
				for name in params:
					self.assertIn(
						name,
						fn.__annotations__,
						f"{fn.__name__}'s argument {name!r} has no type annotation",
					)

		# call check_step exactly the way a browser would: every value sent as a string
		wrapped = validate_argument_types(page.check_step, apply_condition=lambda: True, force_types=True)
		result = wrapped(payload=json.dumps(self._payload()), step="who")
		self.assertIsInstance(result, dict)


if __name__ == "__main__":
	unittest.main()
