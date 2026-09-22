"""Server side of the Focus "Balance at a Glance" card on the Leave Application form (ADHD-058).

The card must show the same numbers HRMS shows everywhere else, so nothing is worked out again here: the
entitlement, used and remaining figures are the ones `get_leave_details` returns for the form's own
"Allocated Leaves" table, the days the application takes come from `get_number_of_leave_days` (half days and
holidays as the Leave Type says), and "would this go over the balance" is the very comparison
`LeaveApplication.validate_balance_leaves` makes on save (`leave_balance_for_consumption < total_leave_days`).
One request answers all of it, so the form does not need three.

Read only. Nothing is written and nothing here blocks a save or a submit.
"""

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate

from hrms.hr.doctype.leave_application.leave_application import (
	get_leave_allocation_records,
	get_leave_balance_on,
	get_leave_details,
	get_leave_entries,
	get_number_of_leave_days,
	is_lwp,
	validate_leave_access,
)

NAME_MAX_LENGTH = 140
# The card shows amber when fewer than this many days would be left after the application (ADHD-058).
LOW_BALANCE_DAYS = 2

# What the card says about this application. The client only picks words and colours for these.
STATE_RECORDED = "recorded"  # already submitted or cancelled: nothing to warn about
STATE_LWP = "lwp"  # Leave Without Pay: HRMS keeps no balance for it
STATE_NO_DAYS = "no_days"  # the chosen days are all holidays (HRMS refuses to save that)
STATE_NO_ALLOCATION = (
	"no_allocation"  # nothing allocated for this date, and a negative balance is not allowed
)
STATE_BALANCE = "balance"  # no end date yet, so there is no impact to show
STATE_SHORT = "short"  # HRMS will refuse the save: not enough balance
STATE_NEGATIVE_ALLOWED = "negative_allowed"  # goes below the balance, and the Leave Type allows that
STATE_LOW = "low"
STATE_OK = "ok"

UNAVAILABLE = {"available": False, "reason": "not_permitted"}


def _check_name(value, label: str) -> str:
	value = (value or "").strip() if isinstance(value, str) else ""
	if not value or len(value) > NAME_MAX_LENGTH:
		frappe.throw(_("{0} is not valid.").format(label))
	return value


def _can_read(employee: str, leave_application: str | None) -> bool:
	"""HRMS's own rule for who may see an employee's leave figures, asked without raising.

	The rule throws PermissionError, and the desk turns that into a "Not permitted" dialog that also re-routes
	the page: far too loud for a card that only decorates the form. So ask, drop the message the throw queued,
	and let the card say quietly that it has nothing to show. A name that does not exist gets the same answer as
	one the caller may not read, so this cannot be used to find out which employees exist.
	"""
	if not frappe.db.exists("Employee", employee):
		return False
	try:
		validate_leave_access(employee, leave_application)
	except frappe.PermissionError:
		frappe.clear_last_message()
		return False
	return True


def _days_for_application(
	employee: str,
	leave_type: str,
	from_date: str,
	to_date: str | None,
	half_day,
	half_day_date,
	leave_application: str | None,
) -> float | None:
	"""Leave days between the dates as the doctype counts them, or None when there is nothing to count."""
	if not to_date or date_diff(to_date, from_date) < 0:
		return None
	try:
		return flt(
			get_number_of_leave_days(
				employee,
				leave_type,
				from_date,
				to_date,
				half_day,
				half_day_date or None,
				leave_application=leave_application,
			)
		)
	except frappe.ValidationError:
		# For instance "no Holiday List was found": HRMS says so itself when the form is saved, and the
		# count is only a preview. Drop the queued message so it does not pop up over the form.
		frappe.clear_last_message()
		return None


def _encashed_days(employee: str, leave_type: str, allocation) -> float:
	"""Days of this allocation already paid out (encashed), counted the way `get_leaves_for_period` does."""
	if not allocation:
		return 0.0
	start, end = getdate(allocation.from_date), getdate(allocation.to_date)
	total = 0.0
	for entry in get_leave_entries(employee, leave_type, start, end):
		if entry.transaction_type == "Leave Encashment" and entry.from_date >= start and entry.to_date <= end:
			total += flt(entry.leaves)
	# ledger rows hold encashment as a negative number of leaves
	return abs(total)


def _state(
	*,
	saved,
	lwp: bool,
	allow_negative: bool,
	allocated: bool,
	days: float | None,
	remaining: float,
	usable: float | None,
) -> str:
	if saved and cint(saved.docstatus) > 0:
		return STATE_RECORDED
	if lwp:
		return STATE_LWP
	if days is not None and days <= 0:
		return STATE_NO_DAYS
	if not allocated:
		if days is not None and allow_negative:
			return STATE_NEGATIVE_ALLOWED
		return STATE_NO_ALLOCATION
	if days is None:
		return STATE_BALANCE
	# The comparison LeaveApplication.validate_balance_leaves makes on save.
	if usable < days or not usable:
		return STATE_NEGATIVE_ALLOWED if allow_negative else STATE_SHORT
	if remaining - days < LOW_BALANCE_DAYS:
		return STATE_LOW
	return STATE_OK


@frappe.whitelist()
def get_leave_glance(
	employee: str,
	leave_type: str,
	from_date: str,
	to_date: str | None = None,
	half_day: int | str | None = None,
	half_day_date: str | None = None,
	leave_application: str | None = None,
) -> dict:
	"""Everything the Balance at a Glance card shows for one Leave Application.

	Returns {"available": False, "reason": ...} when the caller may not see this employee's leave, or the
	Leave Type is unknown: the card then says it has nothing to show.
	"""
	employee = _check_name(employee, _("Employee"))
	leave_type = _check_name(leave_type, _("Leave Type"))
	leave_application = (leave_application or "").strip() or None
	if leave_application:
		leave_application = _check_name(leave_application, _("Leave Application"))
	if not from_date:
		frappe.throw(_("From Date is required."))
	from_date = getdate(from_date)
	to_date = getdate(to_date) if to_date else None

	if not _can_read(employee, leave_application):
		return dict(UNAVAILABLE)

	leave_type_doc = frappe.db.get_value(
		"Leave Type", leave_type, ["name", "is_lwp", "allow_negative"], as_dict=True
	)
	if not leave_type_doc or not frappe.has_permission("Leave Type", "read", leave_type):
		return dict(UNAVAILABLE)

	# What the database says about this application, not what the browser claims: a submitted or cancelled
	# one is history, and the balance already reflects it (or never did).
	saved = None
	if leave_application:
		saved = frappe.db.get_value(
			"Leave Application",
			leave_application,
			["docstatus", "status", "total_leave_days", "employee"],
			as_dict=True,
		)
		if saved and saved.employee != employee:
			# The employee on a draft can be changed on the form before it is saved: then the saved copy says
			# nothing about the one being asked about. (The name never widens access either:
			# validate_leave_access only honours it when the saved employee is the one asked for.)
			saved = None

	lwp = bool(cint(is_lwp(leave_type)))
	allow_negative = bool(cint(leave_type_doc.allow_negative))

	# The figures of the form's own "Allocated Leaves" table (leave_application.js make_dashboard), for the
	# same date, so the card and the table can never disagree.
	details = get_leave_details(employee, from_date, leave_application=leave_application)
	entry = details["leave_allocation"].get(leave_type)
	allocated = bool(entry)
	allocation = get_leave_allocation_records(employee, from_date, leave_type).get(leave_type)

	if saved and cint(saved.docstatus) > 0:
		days = flt(saved.total_leave_days)
	else:
		days = _days_for_application(
			employee, leave_type, from_date, to_date, half_day, half_day_date, leave_application
		)

	remaining = flt(entry["remaining_leaves"]) if entry else 0.0
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision")) or 2
	recorded = bool(saved and cint(saved.docstatus) > 0)
	usable = None
	if allocated and not lwp and days is not None and days > 0 and not recorded:
		# the same call, with the same arguments, as LeaveApplication.validate_balance_leaves
		balance = get_leave_balance_on(
			employee,
			leave_type,
			from_date,
			to_date,
			consider_all_leaves_in_the_allocation_period=True,
			for_consumption=True,
			leave_application=leave_application,
		)
		usable = flt(balance.get("leave_balance_for_consumption"), precision)

	state = _state(
		saved=saved,
		lwp=lwp,
		allow_negative=allow_negative,
		allocated=allocated,
		days=days,
		remaining=remaining,
		usable=usable,
	)
	counted = bool(saved and cint(saved.docstatus) == 1 and saved.status == "Approved")
	shows_impact = state in (STATE_OK, STATE_LOW, STATE_SHORT, STATE_NEGATIVE_ALLOWED)

	return {
		"available": True,
		"leave_type": leave_type,
		"state": state,
		"is_lwp": lwp,
		"allow_negative": allow_negative,
		"allocated": allocated,
		"docstatus": cint(saved.docstatus) if saved else 0,
		"counted": counted,
		"total_leaves": flt(entry["total_leaves"]) if entry else 0.0,
		"used": flt(entry["leaves_taken"]) if entry else 0.0,
		"expired": flt(entry["expired_leaves"]) if entry else 0.0,
		"remaining": remaining,
		"carried_forward": flt(allocation.unused_leaves) if allocation else 0.0,
		"encashed": flt(_encashed_days(employee, leave_type, allocation)),
		"period_from": str(allocation.from_date) if allocation else None,
		"period_to": str(allocation.to_date) if allocation else None,
		"days": days,
		"usable": usable,
		"after": flt(remaining - days, precision) if shows_impact else None,
	}
