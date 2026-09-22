# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Step detection for the Focus mode Payroll Cycle Checklist (ADHD-057).

The six steps follow the payroll flow HRMS really has (see ``payroll_entry.py``):

1. ``attendance``: every employee due pay has every day of the period accounted for by a submitted
   Attendance or a holiday (the very rule ``PayrollEntry.get_employees_with_unmarked_attendance`` uses to
   block a Payroll Entry), and no Attendance for the period is left in draft.
2. ``leave``: no Leave Application overlapping the period still waits for approval (status Open) or for
   submission (status Approved, still a draft).
3. ``additions``: no draft Additional Salary, Employee Incentive or Arrear, and no unsettled Expense Claim,
   is waiting for the period. The system cannot know whether everything was *entered*, so this step is never
   "done" by itself: with nothing waiting it says ``unknown`` and the person ticks it.
4. ``payroll_entry``: a submitted Payroll Entry exists for the period (submitting it creates the salary
   slips), none is left in draft, failed or queued.
5. ``salary_slips``: slips exist for the period, every one is submitted and no employee of a created
   Payroll Entry lacks one.
6. ``bank_entry``: every submitted Payroll Entry has a submitted Bank Entry / Cash Entry (a Journal Entry
   whose account row references the Payroll Entry, see ``PayrollEntry.has_bank_entries``).

A step is one of ``done``, ``pending`` (with counts saying what is left), ``unknown`` ("cannot tell": the
server has no reliable evidence) or ``no_access`` (the caller may not read what the step needs). Nothing is
ever reported ``done`` on absence of evidence. Only counts and statuses leave this module, never amounts.

Everything is read through ``frappe.get_list``, so User Permissions apply; when User Permissions could hide
records from the caller (for instance an Employee-role user, who only sees their own), the step says
``unknown`` rather than count a fraction of the picture.
"""

import calendar
from datetime import date

import frappe
from frappe import _
from frappe.utils import cint, cstr

STEP_IDS = ("attendance", "leave", "additions", "payroll_entry", "salary_slips", "bank_entry")

# Bounds: a checklist must never build unbounded lists. Past a bound the step says it cannot tell.
MAX_EMPLOYEES = 5000
CHUNK_SIZE = 500
MAX_ENTRIES = 50
MAX_COMPANIES = 100
MAX_NAME_LENGTH = 140  # the length of a Frappe document name
MIN_YEAR, MAX_YEAR = 1900, 2200

BANK_VOUCHER_TYPES = ["Bank Entry", "Cash Entry"]


@frappe.whitelist()
def get_payroll_checklist_companies() -> dict:
	"""The companies the caller may read and the one to start on."""
	companies = frappe.get_list("Company", pluck="name", order_by="name asc", limit_page_length=MAX_COMPANIES)
	default = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	if default not in companies:
		default = companies[0] if companies else None
	return {"companies": companies, "default": default}


@frappe.whitelist()
def get_payroll_checklist_status(company: str, month: int | str, year: int | str) -> dict:
	"""Where the payroll cycle of ``company`` stands for the calendar month ``month``/``year``."""
	start, end = _period(month, year)
	_validate_company(company)

	ctx = frappe._dict(company=company, start=start.isoformat(), end=end.isoformat())
	_load_entries(ctx)

	steps = {
		"attendance": _check_attendance(ctx),
		"leave": _check_leave(ctx),
		"additions": _check_additions(ctx),
		"payroll_entry": _check_payroll_entry(ctx),
		"salary_slips": _check_salary_slips(ctx),
		"bank_entry": _check_bank_entry(ctx),
	}
	return {
		"company": company,
		"month": start.month,
		"year": start.year,
		"period_start": ctx.start,
		"period_end": ctx.end,
		"steps": steps,
		"entries": ctx.entries or [],
	}


# ---- inputs -------------------------------------------------------------------------------------------


def _period(month: int | str, year: int | str) -> tuple[date, date]:
	try:
		month, year = int(month), int(year)
		start = date(year, month, 1)
	except (TypeError, ValueError, OverflowError):
		start = None
	if start is None or not MIN_YEAR <= year <= MAX_YEAR:
		frappe.throw(_("Month and year must be a valid calendar month."), frappe.ValidationError)
	return start, date(year, month, calendar.monthrange(year, month)[1])


def _validate_company(company: str) -> None:
	if not isinstance(company, str) or not company or len(company) > MAX_NAME_LENGTH:
		frappe.throw(_("Company is required."), frappe.ValidationError)
	if not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} not found.").format(frappe.bold(company)), frappe.DoesNotExistError)
	if not frappe.has_permission("Company", "read", doc=company):
		frappe.throw(
			_("Not permitted to read Company {0}.").format(frappe.bold(company)), frappe.PermissionError
		)


# ---- reading with the caller's permissions -----------------------------------------------------------


def _can_read(doctype: str) -> bool:
	return bool(frappe.has_permission(doctype, "read"))


def _is_view_limited(doctype: str, company: str) -> bool:
	"""Whether User Permissions could hide some records of ``doctype`` from the caller."""
	user = frappe.session.user
	if user == "Administrator":
		return False
	user_permissions = frappe.permissions.get_user_permissions(user)
	if not user_permissions:
		return False

	linked = {doctype}
	for df in frappe.get_meta(doctype).get_link_fields():
		if not df.ignore_user_permissions:
			linked.add(df.options)

	for allowed_doctype, rows in user_permissions.items():
		if allowed_doctype not in linked:
			continue
		rows = [row for row in rows if not row.get("applicable_for") or row.get("applicable_for") == doctype]
		if not rows:
			continue
		if allowed_doctype == "Company" and any(row.get("doc") == company for row in rows):
			# the checklist only ever looks at one company, and the caller may see all of it
			continue
		return True
	return False


def _access_problem(ctx, *doctypes: str) -> str | None:
	"""``no_access`` or ``limited_view`` when the caller cannot see all of what the doctypes hold."""
	if not all(_can_read(doctype) for doctype in doctypes):
		return "no_access"
	if any(_is_view_limited(doctype, ctx.company) for doctype in doctypes):
		return "limited_view"
	return None


def _count(doctype: str, filters: dict) -> int:
	rows = frappe.get_list(doctype, filters=filters, fields=[{"COUNT": "name", "as": "n"}])
	return cint(rows[0].n) if rows else 0


def _step(state: str, reason: str | None = None, **counts) -> dict:
	return {"state": state, "reason": reason, "counts": counts}


def _blocked(problem: str) -> dict:
	"""``no_access`` is a state of its own; a limited view is a "cannot tell"."""
	if problem == "no_access":
		return _step("no_access", "no_access")
	return _step("unknown", problem)


def _chunks(names: list[str]):
	for index in range(0, len(names), CHUNK_SIZE):
		yield names[index : index + CHUNK_SIZE]


# ---- Payroll Entries of the period (steps 4 to 6 read them) ------------------------------------------


def _load_entries(ctx) -> None:
	"""Fills ``ctx.entries`` (the entries that start in the period) or says why it cannot."""
	ctx.entries = None
	ctx.entries_problem = _access_problem(ctx, "Payroll Entry")
	if ctx.entries_problem:
		return

	rows = frappe.get_list(
		"Payroll Entry",
		filters={
			"company": ctx.company,
			"docstatus": ["<", 2],
			"start_date": ["between", [ctx.start, ctx.end]],
		},
		fields=[
			"name",
			"docstatus",
			"status",
			"start_date",
			"end_date",
			"payroll_frequency",
			"branch",
			"department",
			"designation",
			"grade",
			"number_of_employees",
			"salary_slips_created",
			"salary_slips_submitted",
		],
		order_by="start_date asc, creation asc",
		limit_page_length=MAX_ENTRIES + 1,
	)
	if len(rows) > MAX_ENTRIES:
		ctx.entries_problem = "too_many"
		return

	for row in rows:
		row.start_date = cstr(row.start_date)
		row.end_date = cstr(row.end_date)
	ctx.entries = rows


def _entry_employees(entries: list[dict]) -> set[str] | None:
	"""The employees named in the entries' Employees table, None when there are too many to list."""
	if not entries:
		return set()
	rows = frappe.get_all(
		"Payroll Employee Detail",
		filters={"parent": ["in", [entry.name for entry in entries]], "parenttype": "Payroll Entry"},
		pluck="employee",
		limit_page_length=MAX_EMPLOYEES + 1,
	)
	return None if len(rows) > MAX_EMPLOYEES else set(rows)


def _slip_employees(ctx) -> set[str] | None:
	"""The employees with a live salary slip in the period, None when there are too many to list."""
	rows = frappe.get_list(
		"Salary Slip",
		filters=_slip_filters(ctx),
		pluck="employee",
		limit_page_length=MAX_EMPLOYEES + 1,
	)
	return None if len(rows) > MAX_EMPLOYEES else set(rows)


def _slip_filters(ctx, **extra) -> dict:
	return {
		"company": ctx.company,
		"docstatus": ["<", 2],
		"start_date": ["between", [ctx.start, ctx.end]],
		**extra,
	}


# ---- Step 1: attendance -------------------------------------------------------------------------------


def _due_employees(ctx) -> tuple[list[dict] | None, str | None]:
	"""Employees who can be paid in the period, before any Payroll Entry says so.

	The rule mirrors ``get_filtered_employees`` in ``payroll_entry.py``: not Inactive, employed at some point
	of the period, with a submitted Salary Structure Assignment that has started. (The entry also filters on
	payroll frequency, currency and payable account; those are settled on the entry itself.)
	"""
	if "due" in ctx:
		return ctx.due, ctx.due_problem

	ctx.due, ctx.due_problem = None, _access_problem(ctx, "Employee", "Salary Structure Assignment")
	if ctx.due_problem:
		return ctx.due, ctx.due_problem

	rows = frappe.get_list(
		"Employee",
		filters={"company": ctx.company, "status": ["!=", "Inactive"], "date_of_joining": ["<=", ctx.end]},
		fields=["name", "date_of_joining", "relieving_date"],
		order_by="name asc",
		limit_page_length=MAX_EMPLOYEES + 1,
	)
	if len(rows) > MAX_EMPLOYEES:
		ctx.due_problem = "too_many"
		return ctx.due, ctx.due_problem

	employed = [row for row in rows if not row.relieving_date or cstr(row.relieving_date) >= ctx.start]
	with_assignment = set()
	for names in _chunks([row.name for row in employed]):
		with_assignment.update(
			frappe.get_list(
				"Salary Structure Assignment",
				filters={"docstatus": 1, "employee": ["in", names], "from_date": ["<=", ctx.end]},
				pluck="employee",
				distinct=True,
				limit_page_length=0,
			)
		)
	ctx.due = [row for row in employed if row.name in with_assignment]
	return ctx.due, ctx.due_problem


def _unmarked(ctx, employees: list[dict]) -> tuple[int, int]:
	"""(employees with unmarked days, unmarked days in all), by HRMS's own Payroll Entry rule."""
	employees_unmarked = days_unmarked = 0
	for names in _chunks([employee.name for employee in employees]):
		entry = frappe.get_doc(
			{
				"doctype": "Payroll Entry",
				"start_date": ctx.start,
				"end_date": ctx.end,
				"validate_attendance": 1,
				"employees": [{"employee": name} for name in names],
			}
		)
		for row in entry.get_employees_with_unmarked_attendance() or []:
			employees_unmarked += 1
			days_unmarked += cint(row["unmarked_days"])
	return employees_unmarked, days_unmarked


def _check_attendance(ctx) -> dict:
	if problem := _access_problem(ctx, "Attendance", "Employee", "Salary Structure Assignment"):
		return _blocked(problem)

	employees, problem = _due_employees(ctx)
	if problem:
		return _blocked(problem)
	if not employees:
		return _step("unknown", "no_employees", employees=0)

	employees_unmarked, days_unmarked = _unmarked(ctx, employees)
	drafts = _count(
		"Attendance",
		{"company": ctx.company, "docstatus": 0, "attendance_date": ["between", [ctx.start, ctx.end]]},
	)
	counts = {
		"employees": len(employees),
		"employees_unmarked": employees_unmarked,
		"days_unmarked": days_unmarked,
		"draft_attendance": drafts,
	}
	if employees_unmarked or drafts:
		return _step("pending", "unmarked" if employees_unmarked else "drafts", **counts)
	return _step("done", None, **counts)


# ---- Step 2: leave ------------------------------------------------------------------------------------


def _check_leave(ctx) -> dict:
	if problem := _access_problem(ctx, "Leave Application"):
		return _blocked(problem)

	overlapping = {
		"company": ctx.company,
		"docstatus": 0,
		"from_date": ["<=", ctx.end],
		"to_date": [">=", ctx.start],
	}
	# a leave counts once approved and submitted (submitting is what marks attendance and the leave ledger)
	open_applications = _count("Leave Application", {**overlapping, "status": "Open"})
	approved_not_submitted = _count("Leave Application", {**overlapping, "status": "Approved"})
	counts = {"open": open_applications, "approved_not_submitted": approved_not_submitted}
	if open_applications or approved_not_submitted:
		return _step("pending", "waiting", **counts)
	return _step("done", None, **counts)


# ---- Step 3: additional pay and expense claims --------------------------------------------------------


def _count_additions(ctx, doctype: str) -> int:
	in_period = ["between", [ctx.start, ctx.end]]
	base = {"company": ctx.company, "docstatus": 0}
	if doctype == "Additional Salary":
		# one-off rows carry a payroll date; recurring ones cover a span of dates
		base["disabled"] = 0
		return _count(doctype, {**base, "is_recurring": 0, "payroll_date": in_period}) + _count(
			doctype,
			{**base, "is_recurring": 1, "from_date": ["<=", ctx.end], "to_date": [">=", ctx.start]},
		)
	if doctype == "Expense Claim":
		# a draft (or approved but unsubmitted) claim still waits for the approver; a submitted, approved
		# claim that has not been reimbursed in full still waits for its payment
		waiting = _count(doctype, {**base, "posting_date": in_period})
		unpaid = _count(
			doctype,
			{
				"company": ctx.company,
				"docstatus": 1,
				"posting_date": in_period,
				"status": ["in", ["Unpaid", "Partially Paid"]],
			},
		)
		return waiting + unpaid
	return _count(doctype, {**base, "payroll_date": in_period})


def _check_additions(ctx) -> dict:
	counts = {}
	skipped = []
	for key, doctype in (
		("additional_salaries", "Additional Salary"),
		("incentives", "Employee Incentive"),
		("arrears", "Arrear"),
		("expense_claims", "Expense Claim"),
	):
		if _access_problem(ctx, doctype):
			skipped.append(doctype)
			continue
		counts[key] = _count_additions(ctx, doctype)

	if not counts:
		return _step("no_access", "no_access", skipped=skipped)
	waiting = sum(counts.values())
	if waiting:
		return _step("pending", "waiting", skipped=skipped, **counts)
	# nothing is waiting, which is not proof that everything was entered: leave it to the person's tick
	return _step("unknown", "partial_access" if skipped else "nothing_waiting", skipped=skipped, **counts)


# ---- Step 4: Payroll Entry ----------------------------------------------------------------------------


def _check_payroll_entry(ctx) -> dict:
	if ctx.entries_problem:
		return _blocked(ctx.entries_problem)

	entries = ctx.entries
	if not entries:
		return _step("pending", "no_payroll_entry", entries=0)

	failed = [entry for entry in entries if entry.status == "Failed"]
	queued = [entry for entry in entries if entry.status == "Queued"]
	drafts = [entry for entry in entries if entry.docstatus == 0]
	counts = {
		"entries": len(entries),
		"submitted": sum(1 for entry in entries if entry.docstatus == 1),
		"drafts": len(drafts),
		"failed": len(failed),
		"queued": len(queued),
	}
	if failed:
		return _step("pending", "failed", **counts)
	if queued:
		return _step("pending", "queued", **counts)
	if drafts:
		return _step("pending", "draft", **counts)

	# Employees due pay who are in no entry: with a Branch/Department/... filter on an entry that usually
	# means one more entry is missing; without one it is more likely another payroll frequency.
	filtered = any(entry.branch or entry.department or entry.designation or entry.grade for entry in entries)
	counts["filtered"] = int(filtered)
	uncovered = _uncovered_employees(ctx)
	if uncovered is not None:
		counts["not_in_entry"] = uncovered
		if uncovered and filtered:
			return _step("pending", "not_in_entry", **counts)
	return _step("done", None, **counts)


def _uncovered_employees(ctx) -> int | None:
	"""How many employees due pay are in no Payroll Entry and have no slip; None when it cannot be told."""
	employees, problem = _due_employees(ctx)
	if problem or employees is None:
		return None
	covered = _entry_employees(ctx.entries)
	if covered is None:
		return None
	if _access_problem(ctx, "Salary Slip"):
		return None
	slipped = _slip_employees(ctx)
	if slipped is None:
		return None
	accounted = covered | slipped
	return sum(1 for employee in employees if employee.name not in accounted)


# ---- Step 5: salary slips -----------------------------------------------------------------------------


def _check_salary_slips(ctx) -> dict:
	if problem := _access_problem(ctx, "Salary Slip"):
		return _blocked(problem)

	rows = frappe.get_list(
		"Salary Slip",
		filters=_slip_filters(ctx),
		fields=["docstatus", {"COUNT": "name", "as": "n"}],
		group_by="docstatus",
	)
	by_docstatus = {cint(row.docstatus): cint(row.n) for row in rows}
	drafts, submitted = by_docstatus.get(0, 0), by_docstatus.get(1, 0)
	counts = {"draft": drafts, "submitted": submitted}

	entries = ctx.entries or []
	failed = [entry for entry in entries if entry.status == "Failed"]
	queued = [entry for entry in entries if entry.status == "Queued"]
	counts.update(entries_failed=len(failed), entries_queued=len(queued))

	if failed:
		return _step("pending", "failed", **counts)
	if queued:
		return _step("pending", "queued", **counts)
	if not (drafts or submitted):
		return _step("pending", "not_generated", **counts)
	if drafts:
		return _step("pending", "drafts", **counts)

	# every slip is submitted; is anyone in a created Payroll Entry left without one?
	created = [entry for entry in entries if entry.docstatus == 1 and entry.salary_slips_created]
	if created:
		expected, slipped = _entry_employees(created), _slip_employees(ctx)
		if expected is None or slipped is None:
			counts["missing"] = None
			return _step("unknown", "too_many", **counts)
		counts["missing"] = len(expected - slipped)
		if counts["missing"]:
			return _step("pending", "missing", **counts)
	return _step("done", None, **counts)


# ---- Step 6: bank entry -------------------------------------------------------------------------------


def _check_bank_entry(ctx) -> dict:
	if ctx.entries_problem:
		return _blocked(ctx.entries_problem)
	if problem := _access_problem(ctx, "Journal Entry"):
		return _blocked(problem)

	submitted = [entry for entry in ctx.entries if entry.docstatus == 1]
	if not submitted:
		return _step("pending", "no_payroll_entry", entries=0)

	# a bank entry books what the submitted slips of the entry add up to, so it needs some
	names = [entry.name for entry in submitted]
	referenced = frappe.get_all(
		"Journal Entry Account",
		filters={
			"reference_type": "Payroll Entry",
			"reference_name": ["in", names],
			"parenttype": "Journal Entry",
		},
		fields=["parent", "reference_name"],
		limit_page_length=0,
	)
	journal_entries = {}
	if referenced:
		journal_entries = {
			row.name: row.docstatus
			for row in frappe.get_list(
				"Journal Entry",
				filters={
					"name": ["in", list({row.parent for row in referenced})],
					"voucher_type": ["in", BANK_VOUCHER_TYPES],
					"docstatus": ["<", 2],
				},
				fields=["name", "docstatus"],
				limit_page_length=0,
			)
		}

	paid, in_draft = set(), set()
	for row in referenced:
		docstatus = journal_entries.get(row.parent)
		if docstatus == 1:
			paid.add(row.reference_name)
		elif docstatus == 0:
			in_draft.add(row.reference_name)
	in_draft -= paid

	counts = {"entries": len(submitted), "paid": len(paid), "draft_bank_entry": len(in_draft)}
	if len(paid) == len(submitted):
		return _step("done", None, **counts)
	return _step("pending", "draft_bank_entry" if in_draft else "no_bank_entry", **counts)
