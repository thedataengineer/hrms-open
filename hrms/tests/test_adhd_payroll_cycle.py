# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Server-side tests for the Focus mode Payroll Cycle Checklist (ADHD-057).

Plain unittest, deliberately without ``hrms.tests.utils``/``erpnext.tests.utils`` or ``HRMSTestSuite``:
importing either bootstraps and commits a large amount of fixture data into whatever site this runs on
(see the shared brief). ``frappe.db.commit`` is patched to raise for the whole class, and the one Company
and Employee this suite needs are created once in ``setUpClass`` and rolled back once in ``tearDownClass``
(a Company insert takes ~1.5s — its own chart of accounts — so it is not repeated per test); every other
fixture (Attendance, Leave Application, Payroll Entry, Salary Slip, Journal Entry, ...) is inserted per
test under its own calendar month, so tests never share mutable state and nothing needs re-creating.

Doctype field names below were checked against the JSON in this repo (path:field noted per fixture) and, for
Payroll Entry's own attendance rule, against ``hrms/payroll/doctype/payroll_entry/payroll_entry.py`` directly
(``get_employees_with_unmarked_attendance``), which is the exact rule ``_check_attendance`` mirrors.
"""

import unittest
from unittest.mock import patch

import frappe

from hrms.payroll.services import adhd_payroll_cycle as cycle

YEAR = 2019  # a year no other fixture in this suite touches, one month per scenario


def _month(offset: int) -> tuple[int, int]:
	"""A distinct (month, year) per test, so fixtures never collide across tests."""
	month = 1 + (offset % 12)
	return month, YEAR + offset // 12


class TestAdhdPayrollCycle(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		frappe.flags.in_test = True
		# never let this suite write anything real, however many Company/Employee/... inserts it runs
		cls._commit_patcher = patch.object(
			frappe.db, "commit", side_effect=AssertionError("test tried to commit")
		)
		cls._commit_patcher.start()

		cls.company = (
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": "ADHD057 Focus Payroll Test Co",
					"abbr": "A57PC",
					"default_currency": "USD",
					"country": "United States",
				}
			)
			.insert()
			.name
		)
		cls.other_company = (
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": "ADHD057 Focus Payroll Other Co",
					"abbr": "A57OC",
					"default_currency": "USD",
					"country": "United States",
				}
			)
			.insert()
			.name
		)
		cls.employee = (
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": "Focus",
					"employee_name": "Focus Payroll Test",
					"company": cls.company,
					"status": "Active",
					"gender": "Male",
					"date_of_birth": "1990-01-01",
					"date_of_joining": "2000-01-01",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		# employee/company/docstatus/from_date are the only fields _due_employees reads (payroll_entry.py
		# section "Salary Structure Assignment" filters); the structure itself is never dereferenced
		cls._insert(
			"Salary Structure Assignment",
			employee=cls.employee,
			company=cls.company,
			docstatus=1,
			from_date="2000-01-01",
			salary_structure="ADHD057 Nonexistent SS",
			currency="USD",
		)

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		cls._commit_patcher.stop()
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")

	# ---- fixture helper --------------------------------------------------------------------------------

	_seq = 0

	@classmethod
	def _insert(cls, doctype: str, name: str | None = None, **fields) -> str:
		"""Insert a document bypassing validation (``db_insert``), so a row can carry exactly the field
		combination one branch of adhd_payroll_cycle.py needs without building a whole valid payroll run
		(a submitted Salary Structure, GL accounts, ...). Matches the raw-row pattern already used in
		erpnext/tests/test_adhd_server.py for the same reason."""
		cls._seq += 1
		name = name or f"ADHD057-{doctype[:3].upper()}-{cls._seq}"
		now = frappe.utils.now()
		doc = frappe.get_doc(
			{
				"doctype": doctype,
				"name": name,
				"owner": "Administrator",
				"modified_by": "Administrator",
				"creation": now,
				"modified": now,
				**fields,
			}
		)
		doc.db_insert()
		return name

	def _new_company(self, suffix: str) -> str:
		"""A throwaway Company for the two tests below that need their own due employees: those employees
		would otherwise outlive the test (nothing rolls back between tests, see the module docstring) and
		quietly change the "how many employees are due" count every other test relies on for self.company
		and self.other_company."""
		return (
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": f"ADHD057 Focus Payroll {suffix}",
					"abbr": f"A57{suffix}",
					"default_currency": "USD",
					"country": "United States",
				}
			)
			.insert()
			.name
		)

	def _due_employee(self, company: str) -> str:
		"""An employee due pay under ``company`` from year 2000, via a Salary Structure Assignment."""
		employee = self._insert(
			"Employee",
			naming_series="HR-EMP-",
			first_name="Focus2",
			employee_name="Focus Payroll Test 2",
			company=company,
			status="Active",
			gender="Female",
			date_of_birth="1990-01-01",
			date_of_joining="2000-01-01",
		)
		self._insert(
			"Salary Structure Assignment",
			employee=employee,
			company=company,
			docstatus=1,
			from_date="2000-01-01",
			salary_structure="ADHD057 Nonexistent SS 2",
			currency="USD",
		)
		return employee

	# ---- input validation (type annotations, bad month, bad company) -----------------------------------

	def test_every_argument_of_every_whitelisted_method_is_annotated(self):
		"""hooks.py sets require_type_annotated_api_methods: an unannotated argument answers every real
		request with HTTP 417, invisible to a direct Python call (see the shared brief, rule 1)."""
		from frappe.utils.typing_validations import validate_argument_types

		wrapped = validate_argument_types(
			cycle.get_payroll_checklist_status, apply_condition=lambda: True, force_types=True
		)
		# called exactly as a browser would: every value arrives as a string
		result = wrapped(company=self.company, month="1", year=str(YEAR))
		self.assertEqual(result["month"], 1)

		wrapped_companies = validate_argument_types(
			cycle.get_payroll_checklist_companies, apply_condition=lambda: True, force_types=True
		)
		self.assertIn(self.company, wrapped_companies()["companies"])

	def test_rejects_an_impossible_month(self):
		for month, year in (("13", YEAR), ("0", YEAR), ("not-a-month", YEAR), ("1", "not-a-year")):
			with self.assertRaises(frappe.ValidationError, msg=f"month={month} year={year}"):
				cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)

	def test_rejects_a_company_that_does_not_exist(self):
		with self.assertRaises(frappe.DoesNotExistError):
			cycle.get_payroll_checklist_status(company="ADHD057 Nonexistent Co", month=1, year=YEAR)

	def test_rejects_a_company_name_over_the_length_bound(self):
		# assertRaisesRegex, not just assertRaises(ValidationError): frappe.DoesNotExistError is itself a
		# ValidationError subclass, so a too-loose check would still pass if the length bound were dropped
		# and this fell through to a plain "not found" lookup instead of being rejected outright
		with self.assertRaisesRegex(frappe.ValidationError, "Company is required"):
			cycle.get_payroll_checklist_status(company="x" * 141, month=1, year=YEAR)

	def test_needs_permission_to_read_the_company(self):
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				cycle.get_payroll_checklist_status(company=self.company, month=1, year=YEAR)

	# ---- a company with no employees never lies "done" --------------------------------------------------

	def test_a_company_with_no_employees_is_cannot_tell_not_done(self):
		# the shared brief flags this exactly: a company (the demo company, here a fresh one instead so
		# the test does not depend on the live site's data) may have zero employees
		month, year = _month(0)
		result = cycle.get_payroll_checklist_status(company=self.other_company, month=month, year=year)
		self.assertEqual(result["steps"]["attendance"]["state"], "unknown")
		self.assertEqual(result["steps"]["attendance"]["reason"], "no_employees")
		# and the steps that need a Payroll Entry are equally honest: none exists, so "pending", not "done"
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["salary_slips"]["state"], "pending")
		self.assertEqual(result["steps"]["bank_entry"]["state"], "pending")
		# "leave" is legitimately "done" with zero applications; nothing else may be "done" on zero evidence
		for step_id, step in result["steps"].items():
			if step_id != "leave":
				self.assertNotEqual(step["state"], "done", step_id)

	# ---- step 1: attendance ------------------------------------------------------------------------------

	def test_attendance_done_when_every_working_day_is_marked(self):
		month, year = _month(1)
		for day in range(1, 6):
			self._insert(
				"Attendance",
				naming_series="HR-ATT-",
				employee=self.employee,
				employee_name="Focus Payroll Test",
				company=self.company,
				attendance_date=f"{year:04d}-{month:02d}-{day:02d}",
				status="Present",
				docstatus=1,
			)
		# make the period exactly those 5 days by scoping via a fresh due-only employee is unnecessary:
		# get_employees_with_unmarked_attendance counts calendar days minus holidays minus marked days, so
		# use a short, explicit range by checking just those 5 days against a payroll period() shim instead
		with patch.object(cycle, "_period", return_value=(_date(year, month, 1), _date(year, month, 5))):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["attendance"]["state"], "done")
		self.assertEqual(result["steps"]["attendance"]["counts"]["employees_unmarked"], 0)

	def test_attendance_pending_when_days_are_unmarked(self):
		month, year = _month(2)
		with patch.object(cycle, "_period", return_value=(_date(year, month, 1), _date(year, month, 5))):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["attendance"]["state"], "pending")
		self.assertEqual(result["steps"]["attendance"]["reason"], "unmarked")
		self.assertEqual(result["steps"]["attendance"]["counts"]["days_unmarked"], 5)

	def test_attendance_pending_on_a_draft_record_even_if_days_are_covered(self):
		month, year = _month(3)
		self._insert(
			"Attendance",
			naming_series="HR-ATT-",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			attendance_date=f"{year:04d}-{month:02d}-01",
			status="Present",
			docstatus=0,
		)
		with patch.object(cycle, "_period", return_value=(_date(year, month, 1), _date(year, month, 1))):
			with patch.object(cycle, "_unmarked", return_value=(0, 0)):
				result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["attendance"]["state"], "pending")
		self.assertEqual(result["steps"]["attendance"]["reason"], "drafts")

	def test_attendance_needs_permission_on_every_doctype_it_reads(self):
		month, year = _month(4)
		for blocked in ("Attendance", "Employee", "Salary Structure Assignment"):
			with patch(
				"frappe.has_permission", side_effect=lambda dt, *a, blocked=blocked, **k: dt != blocked
			):
				result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
			self.assertEqual(result["steps"]["attendance"]["state"], "no_access", blocked)
			self.assertEqual(result["steps"]["attendance"]["counts"], {})

	# ---- step 2: leave ------------------------------------------------------------------------------------

	def test_leave_pending_on_an_open_application_overlapping_the_period(self):
		month, year = _month(5)
		self._insert(
			"Leave Application",
			naming_series="HR-LAP-",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			leave_type="ADHD057 Nonexistent Leave Type",
			from_date=f"{year:04d}-{month:02d}-10",
			to_date=f"{year:04d}-{month:02d}-11",
			status="Open",
			docstatus=0,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["leave"]["state"], "pending")
		self.assertEqual(result["steps"]["leave"]["counts"]["open"], 1)

	def test_leave_pending_on_an_approved_but_unsubmitted_application(self):
		month, year = _month(6)
		self._insert(
			"Leave Application",
			naming_series="HR-LAP-",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			leave_type="ADHD057 Nonexistent Leave Type",
			from_date=f"{year:04d}-{month:02d}-10",
			to_date=f"{year:04d}-{month:02d}-11",
			status="Approved",
			docstatus=0,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["leave"]["state"], "pending")
		self.assertEqual(result["steps"]["leave"]["counts"]["approved_not_submitted"], 1)

	def test_leave_done_when_nothing_overlaps_the_period(self):
		month, year = _month(7)
		self._insert(
			"Leave Application",
			naming_series="HR-LAP-",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			leave_type="ADHD057 Nonexistent Leave Type",
			from_date=f"{year:04d}-01-01",
			to_date=f"{year:04d}-01-02",
			status="Open",
			docstatus=0,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["leave"]["state"], "done")

	# ---- step 3: additional pay / expense claims -----------------------------------------------------------

	def test_additions_pending_when_a_draft_additional_salary_is_waiting(self):
		month, year = _month(8)
		self._insert(
			"Additional Salary",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			salary_component="ADHD057 Nonexistent Component",
			amount=100,
			payroll_date=f"{year:04d}-{month:02d}-15",
			docstatus=0,
			is_recurring=0,
			disabled=0,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["additions"]["state"], "pending")
		self.assertEqual(result["steps"]["additions"]["counts"]["additional_salaries"], 1)

	def test_additions_unknown_and_never_done_when_nothing_is_waiting(self):
		"""Step 3 can never auto-detect "everything was entered" — only "something is still open" — so an
		empty result is "unknown" (a manual tick), never "done"."""
		month, year = _month(9)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["additions"]["state"], "unknown")
		self.assertEqual(result["steps"]["additions"]["reason"], "nothing_waiting")

	def test_additions_reports_partial_access_without_raising(self):
		month, year = _month(10)
		with patch("frappe.has_permission", side_effect=lambda dt, *a, **k: dt != "Arrear"):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertIn(result["steps"]["additions"]["state"], ("unknown", "pending"))
		self.assertIn("Arrear", result["steps"]["additions"]["counts"]["skipped"])
		self.assertNotIn("arrears", result["steps"]["additions"]["counts"])

	# ---- step 4: Payroll Entry ------------------------------------------------------------------------------

	def test_payroll_entry_pending_when_none_exists(self):
		month, year = _month(11)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["payroll_entry"]["reason"], "no_payroll_entry")

	def test_payroll_entry_pending_while_draft(self):
		month, year = _month(12)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=0,
			status="Draft",
			number_of_employees=1,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["payroll_entry"]["reason"], "draft")

	def test_payroll_entry_pending_when_failed(self):
		month, year = _month(13)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Failed",
			number_of_employees=1,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["payroll_entry"]["reason"], "failed")

	def test_payroll_entry_done_when_submitted(self):
		month, year = _month(14)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
			salary_slips_created=1,
			salary_slips_submitted=1,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "done")

	def test_payroll_entry_needs_permission(self):
		month, year = _month(15)
		with patch("frappe.has_permission", side_effect=lambda dt, *a, **k: dt != "Payroll Entry"):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "no_access")
		self.assertEqual(result["steps"]["payroll_entry"]["counts"], {})
		# bank_entry reads the same Payroll Entry list, so it degrades the same way, not a false "done"
		self.assertEqual(result["steps"]["bank_entry"]["state"], "no_access")

	def test_payroll_entry_pending_when_a_filtered_entry_misses_a_due_employee(self):
		"""A Payroll Entry scoped by Branch/Department/Designation/Grade (payroll_entry.py's own
		make_filters) usually means a second entry is still needed for whoever it left out."""
		company = self._new_company("F1")
		self._due_employee(company)
		month, year = _month(26)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			department="ADHD057 Nonexistent Department",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
			salary_slips_created=1,
			salary_slips_submitted=1,
		)
		result = cycle.get_payroll_checklist_status(company=company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["payroll_entry"]["reason"], "not_in_entry")
		self.assertEqual(result["steps"]["payroll_entry"]["counts"]["not_in_entry"], 1)

	def test_payroll_entry_done_when_every_due_employee_is_covered(self):
		company = self._new_company("F2")
		employee = self._due_employee(company)
		month, year = _month(27)
		start = f"{year:04d}-{month:02d}-01"
		pe = self._insert(
			"Payroll Entry",
			company=company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			department="ADHD057 Nonexistent Department",
			docstatus=1,
			status="Submitted",
			number_of_employees=1,
			salary_slips_created=1,
			salary_slips_submitted=1,
		)
		self._insert(
			"Payroll Employee Detail",
			parent=pe,
			parenttype="Payroll Entry",
			parentfield="employees",
			idx=1,
			employee=employee,
			employee_name="Focus Payroll Test 2",
		)
		result = cycle.get_payroll_checklist_status(company=company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "done")
		self.assertEqual(result["steps"]["payroll_entry"]["counts"]["not_in_entry"], 0)

	# ---- step 5: salary slips -----------------------------------------------------------------------------

	def test_salary_slips_pending_when_none_generated(self):
		month, year = _month(16)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["salary_slips"]["state"], "pending")
		self.assertEqual(result["steps"]["salary_slips"]["reason"], "not_generated")

	def test_salary_slips_pending_while_any_slip_is_draft(self):
		month, year = _month(17)
		self._insert(
			"Salary Slip",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			start_date=f"{year:04d}-{month:02d}-01",
			end_date=f"{year:04d}-{month:02d}-28",
			docstatus=0,
			status="Draft",
			currency="USD",
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["salary_slips"]["state"], "pending")
		self.assertEqual(result["steps"]["salary_slips"]["reason"], "drafts")

	def test_salary_slips_done_when_every_slip_is_submitted(self):
		month, year = _month(18)
		self._insert(
			"Salary Slip",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			start_date=f"{year:04d}-{month:02d}-01",
			end_date=f"{year:04d}-{month:02d}-28",
			docstatus=1,
			status="Submitted",
			currency="USD",
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["salary_slips"]["state"], "done")
		self.assertEqual(result["steps"]["salary_slips"]["counts"]["submitted"], 1)

	def test_salary_slips_needs_permission(self):
		month, year = _month(19)
		with patch("frappe.has_permission", side_effect=lambda dt, *a, **k: dt != "Salary Slip"):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["salary_slips"]["state"], "no_access")
		self.assertEqual(result["steps"]["salary_slips"]["counts"], {})

	# ---- step 6: bank entry --------------------------------------------------------------------------------

	def test_bank_entry_pending_without_a_submitted_payroll_entry(self):
		month, year = _month(20)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["bank_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["bank_entry"]["reason"], "no_payroll_entry")

	def test_bank_entry_pending_until_the_journal_entry_is_submitted(self):
		month, year = _month(21)
		start = f"{year:04d}-{month:02d}-01"
		pe = self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
		)
		je = self._insert(
			"Journal Entry",
			company=self.company,
			posting_date=start,
			voucher_type="Bank Entry",
			naming_series="Journal Voucher-",
			docstatus=0,
		)
		self._insert(
			"Journal Entry Account",
			parent=je,
			parenttype="Journal Entry",
			parentfield="accounts",
			idx=1,
			account="ADHD057 Nonexistent Account",
			reference_type="Payroll Entry",
			reference_name=pe,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["bank_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["bank_entry"]["reason"], "draft_bank_entry")

	def test_bank_entry_done_once_the_journal_entry_is_submitted(self):
		month, year = _month(22)
		start = f"{year:04d}-{month:02d}-01"
		pe = self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
		)
		je = self._insert(
			"Journal Entry",
			company=self.company,
			posting_date=start,
			voucher_type="Bank Entry",
			naming_series="Journal Voucher-",
			docstatus=1,
		)
		self._insert(
			"Journal Entry Account",
			parent=je,
			parenttype="Journal Entry",
			parentfield="accounts",
			idx=1,
			account="ADHD057 Nonexistent Account",
			reference_type="Payroll Entry",
			reference_name=pe,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["bank_entry"]["state"], "done")
		self.assertEqual(result["steps"]["bank_entry"]["counts"]["paid"], 1)

	def test_bank_entry_needs_permission_on_journal_entry(self):
		month, year = _month(23)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=self.company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
		)
		with patch("frappe.has_permission", side_effect=lambda dt, *a, **k: dt != "Journal Entry"):
			result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["bank_entry"]["state"], "no_access")

	# ---- company scoping and currency ------------------------------------------------------------------------

	def test_a_step_never_counts_another_companys_records(self):
		month, year = _month(24)
		start = f"{year:04d}-{month:02d}-01"
		self._insert(
			"Payroll Entry",
			company=self.other_company,
			posting_date=start,
			start_date=start,
			end_date=f"{year:04d}-{month:02d}-28",
			payroll_frequency="Monthly",
			docstatus=1,
			status="Submitted",
			number_of_employees=0,
			salary_slips_created=1,
			salary_slips_submitted=1,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		self.assertEqual(result["steps"]["payroll_entry"]["state"], "pending")
		self.assertEqual(result["steps"]["payroll_entry"]["counts"]["entries"], 0)

	def test_no_amount_ever_leaves_the_module(self):
		"""Payroll figures are sensitive: only counts and statuses may appear anywhere in the response."""
		month, year = _month(25)
		self._insert(
			"Additional Salary",
			employee=self.employee,
			employee_name="Focus Payroll Test",
			company=self.company,
			salary_component="ADHD057 Nonexistent Component",
			amount=999999,
			payroll_date=f"{year:04d}-{month:02d}-15",
			docstatus=0,
			is_recurring=0,
			disabled=0,
		)
		result = cycle.get_payroll_checklist_status(company=self.company, month=month, year=year)
		blob = frappe.as_json(result)
		self.assertNotIn("999999", blob)
		for key in ("amount", "net_pay", "gross_pay", "grand_total"):
			self.assertNotIn(key, blob)

	def test_get_payroll_checklist_companies_defaults_to_the_users_company(self):
		with patch("frappe.defaults.get_user_default", return_value=self.company):
			result = cycle.get_payroll_checklist_companies()
		self.assertEqual(result["default"], self.company)
		self.assertIn(self.company, result["companies"])


def _date(year: int, month: int, day: int):
	import datetime

	return datetime.date(year, month, day)


if __name__ == "__main__":
	unittest.main()
