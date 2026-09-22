# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What Hubble adds to RTB's Focus inbox: HR documents waiting on the signed-in person that are due today or
overdue. RTB calls `urgent_items` through its `focus_urgent_items` hook (see `erpnext/adhd/api.py`) and
ranks the rows with its own; every row here is read with the caller's permissions (`frappe.get_list`),
bounded, and carries only a name, a type and a date, never anyone's reason text.
"""

import frappe
from frappe import _
from frappe.utils import getdate

MAX_ROWS_PER_SOURCE = 20


def urgent_items(user: str, current_day: str) -> list[dict]:
	"""Leave applications and expense claims waiting on `user` as approver, and interviews `user` conducts,
	that are due today or before. A source the person may not read contributes nothing rather than an error."""
	rows: list[dict] = []
	for source in (_leave_to_approve, _claims_to_approve, _interviews_to_hold):
		try:
			rows.extend(source(user, getdate(current_day)))
		except frappe.PermissionError:
			continue
	return rows


def _leave_to_approve(user, day):
	"""An open leave application whose first day has come is overdue for its approver."""
	return [
		{
			"doctype": "Leave Application",
			"name": row.name,
			"title": _("{0}: {1}").format(row.leave_type, row.employee_name or row.employee),
			"counterparty": row.employee_name or row.employee,
			"due_date": row.from_date,
		}
		for row in frappe.get_list(
			"Leave Application",
			filters={"status": "Open", "docstatus": 0, "leave_approver": user, "from_date": ("<=", day)},
			fields=["name", "employee", "employee_name", "leave_type", "from_date"],
			order_by="from_date asc",
			limit_page_length=MAX_ROWS_PER_SOURCE,
		)
	]


def _claims_to_approve(user, day):
	"""A draft expense claim is waiting on its approver from the day it was made."""
	return [
		{
			"doctype": "Expense Claim",
			"name": row.name,
			"title": _("Expense claim: {0}").format(row.employee_name or row.employee),
			"counterparty": row.employee_name or row.employee,
			"due_date": row.posting_date,
		}
		for row in frappe.get_list(
			"Expense Claim",
			filters={
				"approval_status": "Draft",
				"docstatus": 0,
				"expense_approver": user,
				"posting_date": ("<=", day),
			},
			fields=["name", "employee", "employee_name", "posting_date"],
			order_by="posting_date asc",
			limit_page_length=MAX_ROWS_PER_SOURCE,
		)
	]


def _interviews_to_hold(user, day):
	"""A pending interview scheduled for today or earlier, for someone on its interviewer list."""
	seen = set()
	rows = []
	for row in frappe.get_list(
		"Interview",
		filters=[
			["Interview Detail", "interviewer", "=", user],
			["Interview", "status", "=", "Pending"],
			["Interview", "docstatus", "<", 2],
			["Interview", "scheduled_on", "<=", day],
		],
		fields=["name", "job_applicant", "designation", "scheduled_on"],
		order_by="scheduled_on asc",
		limit_page_length=MAX_ROWS_PER_SOURCE,
	):
		if row.name in seen:
			continue
		seen.add(row.name)
		rows.append(
			{
				"doctype": "Interview",
				"name": row.name,
				"title": _("Interview: {0}").format(row.job_applicant),
				"counterparty": row.designation,
				"due_date": row.scheduled_on,
			}
		)
	return rows
