"""Server side of the Focus "Onboard New Employee" wizard (ADHD-014).

The wizard collects a person's details one small step at a time in the browser and writes NOTHING to the
server until the person confirms the last step. `check` validates the answers without writing anything and
describes, in plain words, what confirming will create. `create` then makes every document in one atomic
unit: a database savepoint is taken first and rolled back if any step fails, so either everything exists
or nothing does.

Which documents (checked against the HRMS / ERPNext source, see the notes at each step):

* Employee: its Education is the `education` child table (`Employee Education` is `istable`), so it is written
  as rows of the Employee, not as a separate document.
* Address: a real document, linked to the employee through its `links` Dynamic Link table.
* Salary Structure Assignment: a real (submittable) document.
* Leave: a Leave Policy Assignment, not a bare Leave Allocation. Submitting one makes HRMS create the
  Leave Allocations itself (`LeavePolicyAssignment.grant_leave_alloc_for_employee`), with the pro-rata,
  earned-leave and carry-forward rules of each leave type, and HRMS's own Employee form assigns leave this way
  ("Assign Leave Policy" in `hrms/public/js/erpnext/employee.js`). A hand-made Leave Allocation would skip all of
  that.

Permissions are the caller's own: every insert uses the normal permission checks (no ``ignore_permissions``).
"""

import datetime
import json
import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	add_months,
	cint,
	flt,
	fmt_money,
	formatdate,
	get_last_day,
	getdate,
	strip_html_tags,
	today,
)

# The steps of the wizard, in screen order. The server only needs the ones that carry data.
STEP_LABELS = {
	"who": "Who is joining",
	"job": "Job details",
	"education": "Education",
	"address": "Address",
	"salary": "Salary",
	"leave": "Leave",
	"review": "Review",
}
# steps the person may leave for later; a reminder is created for each one that is skipped
SKIPPABLE_STEPS = ("education", "address", "salary", "leave")
REMINDER_DAYS = 7
MAX_EDUCATION_ROWS = 10
MAX_PAYLOAD_CHARS = 100_000
TEXT_MAX = 140
LONG_TEXT_MAX = 500
MONEY_MAX = Decimal("999999999999")
FIRST_PLAUSIBLE_DATE = datetime.date(1900, 1, 1)
FIRST_PLAUSIBLE_JOINING = datetime.date(1950, 1, 1)
MAX_FUTURE_JOINING_DAYS = 730

_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONEY_SHAPE = re.compile(r"^\d{1,13}(\.\d{1,6})?$")
_BAD_CHARS = re.compile(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BAD_CHARS_MULTILINE = re.compile(r"[<>\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CREATE_DOCTYPES = {
	"employee": "Employee",
	"address": "Address",
	"salary": "Salary Structure Assignment",
	"leave": "Leave Policy Assignment",
	"reminders": "ToDo",
}


def step_label(step: str) -> str:
	return _(STEP_LABELS.get(step, step))


# ----------------------------------------------------------------------------------------------------------
# Context
# ----------------------------------------------------------------------------------------------------------


def get_context() -> dict:
	"""What the wizard needs before it draws its first screen. Read only."""
	naming = frappe.db.get_single_value("HR Settings", "emp_created_by") or None
	employee_meta = frappe.get_meta("Employee")
	address_meta = frappe.get_meta("Address")
	education_meta = frappe.get_meta("Employee Education")

	return {
		"default_company": frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or None,
		"naming": naming,
		"naming_ready": bool(naming),
		"today": today(),
		"can_create": {
			"employee": bool(frappe.has_permission("Employee", "create")),
			"address": bool(frappe.has_permission("Address", "create")),
			"salary": bool(frappe.has_permission("Salary Structure Assignment", "create")),
			"leave": bool(frappe.has_permission("Leave Policy Assignment", "create")),
			"reminders": bool(frappe.has_permission("ToDo", "create")),
			"onboarding": bool(frappe.has_permission("Employee Onboarding", "create")),
		},
		"can_submit": {
			"salary": bool(frappe.has_permission("Salary Structure Assignment", "submit")),
			"leave": bool(frappe.has_permission("Leave Policy Assignment", "submit")),
		},
		"has_employment_type": bool(employee_meta.has_field("employment_type")),
		"address_types": _options(address_meta, "address_type"),
		"education_levels": _options(education_meta, "level"),
		"reminder_days": REMINDER_DAYS,
		"max_education_rows": MAX_EDUCATION_ROWS,
	}


def _options(meta, fieldname: str) -> list[str]:
	field = meta.get_field(fieldname)
	return [option for option in (field.options or "").split("\n") if option] if field else []


# ----------------------------------------------------------------------------------------------------------
# Small validators. Each names the field in its message and never raises: it records the problem and moves on
# so a person sees every problem of a screen at once.
# ----------------------------------------------------------------------------------------------------------


class _Errors:
	"""Collects validation problems. With `only`, problems of other steps are dropped (a screen is checked while
	the later ones are still empty)."""

	def __init__(self, only: str | None = None):
		self.only = only
		self.items: list[dict] = []

	def add(self, step: str, field: str | None, label: str | None, message: str, row: int | None = None):
		if self.only and step != self.only:
			return
		self.items.append({"step": step, "field": field, "label": label, "message": message, "row": row})

	def has(self, step: str, field: str | None = None) -> bool:
		return any(item["step"] == step and (field is None or item["field"] == field) for item in self.items)


def _text(errors, step, field, label, value, *, required=False, max_len=TEXT_MAX, row=None, multiline=False):
	if value is None or value == "":
		if required:
			errors.add(step, field, label, _("{0} is required.").format(label), row)
		return None
	if not isinstance(value, str):
		errors.add(step, field, label, _("{0} must be text.").format(label), row)
		return None
	value = value.strip()
	if not value:
		if required:
			errors.add(step, field, label, _("{0} is required.").format(label), row)
		return None
	pattern = _BAD_CHARS_MULTILINE if multiline else _BAD_CHARS
	if pattern.search(value) or (not multiline and re.search(r"[\r\n\t]", value)):
		errors.add(
			step,
			field,
			label,
			_("{0} cannot contain the characters < or > or control characters.").format(label),
			row,
		)
		return None
	if len(value) > max_len:
		errors.add(
			step,
			field,
			label,
			_("{0} is too long: at most {1} characters.").format(label, max_len),
			row,
		)
		return None
	return value


def _date(errors, step, field, label, value, *, required=True, row=None):
	if value is None or value == "":
		if required:
			errors.add(step, field, label, _("{0} is required.").format(label), row)
		return None
	if not isinstance(value, str) or not _DATE_SHAPE.match(value.strip()):
		errors.add(step, field, label, _("{0} is not a valid date.").format(label), row)
		return None
	try:
		return datetime.date.fromisoformat(value.strip())
	except ValueError:
		errors.add(step, field, label, _("{0} is not a valid date.").format(label), row)
		return None


def _whole_number(errors, step, field, label, value, *, low, high, required=True, row=None):
	if value is None or value == "":
		if required:
			errors.add(step, field, label, _("{0} is required.").format(label), row)
		return None
	if isinstance(value, bool) or not (
		isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"\d{1,6}", value.strip()))
	):
		errors.add(step, field, label, _("{0} must be a whole number.").format(label), row)
		return None
	number = cint(value)
	if number < low or number > high:
		errors.add(
			step,
			field,
			label,
			_("{0} must be between {1} and {2}.").format(label, low, high),
			row,
		)
		return None
	return number


def _money(errors, step, field, label, value, *, required=False):
	if value is None or value == "":
		if required:
			errors.add(step, field, label, _("{0} is required.").format(label), None)
		return None
	number = None
	if isinstance(value, bool):
		number = None
	elif isinstance(value, int | float):
		try:
			number = Decimal(str(value))
		except InvalidOperation:
			number = None
	elif isinstance(value, str) and _MONEY_SHAPE.match(value.strip()):
		number = Decimal(value.strip())
	if number is None or not number.is_finite():
		errors.add(
			step, field, label, _("{0} must be a number, for example 45000 or 45000.50.").format(label)
		)
		return None
	if number < 0:
		errors.add(step, field, label, _("{0} cannot be negative.").format(label))
		return None
	if number > MONEY_MAX:
		errors.add(step, field, label, _("{0} is too large.").format(label))
		return None
	return flt(number)


def _link_allowed(link_doctype: str, value: str, for_doctype: str) -> bool:
	"""Whether the caller's User Permissions let them use `value` (a `link_doctype`) on a `for_doctype`
	document. Mirrors the check `frappe.permissions.has_user_permission` makes when a document is saved."""
	from frappe.core.doctype.user_permission.user_permission import get_user_permissions
	from frappe.permissions import get_allowed_docs_for_doctype

	rows = (get_user_permissions() or {}).get(link_doctype)
	if not rows:
		return True
	allowed = get_allowed_docs_for_doctype(rows, for_doctype)
	return not allowed or value in allowed


def _link(errors, step, field, label, doctype, value, *, for_doctype, required=False, row=None):
	"""A Link value that must exist and be one the caller may use."""
	value = _text(errors, step, field, label, value, required=required, row=row)
	if value is None:
		return None
	if not frappe.db.exists(doctype, value):
		errors.add(step, field, label, _("{0} '{1}' was not found.").format(label, value), row)
		return None
	if not _link_allowed(doctype, value, for_doctype):
		errors.add(
			step,
			field,
			label,
			_("You are not allowed to use {0} '{1}'.").format(label, value),
			row,
		)
		return None
	return value


# ----------------------------------------------------------------------------------------------------------
# Validation of a whole payload
# ----------------------------------------------------------------------------------------------------------


def _load_payload(raw, errors: _Errors) -> dict:
	if isinstance(raw, str):
		if len(raw) > MAX_PAYLOAD_CHARS:
			errors.add("review", None, None, _("That is more information than this wizard can take."))
			return {}
		try:
			raw = json.loads(raw)
		except ValueError:
			errors.add("review", None, None, _("The wizard sent answers the server could not read."))
			return {}
	if not isinstance(raw, dict):
		errors.add("review", None, None, _("The wizard sent answers the server could not read."))
		return {}
	return raw


def _section(raw: dict, key: str) -> dict:
	value = raw.get(key)
	return value if isinstance(value, dict) else {}


def validate(raw, only_step: str | None = None) -> tuple[frappe._dict, list[dict]]:
	"""Check the answers. Returns the cleaned answers and the list of problems (empty when all is well). Writes
	nothing. With `only_step`, only the problems of that screen are reported."""
	if only_step is not None and only_step not in STEP_LABELS:
		only_step = None
	errors = _Errors(only_step)
	raw = _load_payload(raw, errors)

	cleaned = frappe._dict(
		who=None, job=None, education=[], address=None, salary=None, leave=None, skipped=[], submit=False
	)

	naming = frappe.db.get_single_value("HR Settings", "emp_created_by")
	if not naming:
		errors.add(
			"who",
			None,
			None,
			_(
				"Employees cannot be added yet: no way of numbering them is set in HR Settings "
				"(Employee Naming System). An administrator needs to set that first."
			),
		)

	if not frappe.has_permission("Employee", "create"):
		errors.add("who", None, None, _("You do not have permission to add employees."))

	cleaned.skipped = _validate_skipped(raw, errors)
	cleaned.submit = bool(_section(raw, "options").get("submit"))

	cleaned.who = _validate_who(_section(raw, "who"), naming, errors)
	cleaned.job = _validate_job(_section(raw, "job"), cleaned.who, naming, errors)
	company = cleaned.job.get("company") if cleaned.job else None
	# cleaned.job stores dates as ISO strings (it is returned to the client); the later steps need real
	# `date` objects to compare against, so it is parsed back here rather than passed around as text.
	joining = (
		getdate(cleaned.job["date_of_joining"])
		if cleaned.job and cleaned.job.get("date_of_joining")
		else None
	)

	if "education" not in cleaned.skipped:
		cleaned.education = _validate_education(raw.get("education"), errors)
	if "address" not in cleaned.skipped:
		cleaned.address = _validate_address(_section(raw, "address"), cleaned.who, errors)
	if "salary" not in cleaned.skipped:
		cleaned.salary = _validate_salary(_section(raw, "salary"), company, joining, errors)
	if "leave" not in cleaned.skipped:
		cleaned.leave = _validate_leave(_section(raw, "leave"), company, joining, errors)

	_validate_permissions(cleaned, errors)
	return cleaned, errors.items


def _validate_skipped(raw: dict, errors: _Errors) -> list[str]:
	skipped = raw.get("skipped") or []
	if not isinstance(skipped, list) or any(not isinstance(step, str) for step in skipped):
		errors.add("review", None, None, _("The list of skipped steps could not be read."))
		return []
	clean = []
	for step in skipped:
		if step not in SKIPPABLE_STEPS:
			errors.add(
				"review",
				None,
				None,
				_("{0} is not a step that can be skipped.").format(step[:40]),
			)
		elif step not in clean:
			clean.append(step)
	return clean


def _validate_who(who: dict, naming: str | None, errors: _Errors) -> dict | None:
	step = "who"
	values = {
		"first_name": _text(
			errors, step, "first_name", _("First name"), who.get("first_name"), required=True
		),
		"middle_name": _text(errors, step, "middle_name", _("Middle name"), who.get("middle_name")),
		"last_name": _text(errors, step, "last_name", _("Last name"), who.get("last_name")),
	}
	values["gender"] = _link(
		errors,
		step,
		"gender",
		_("Gender"),
		"Gender",
		who.get("gender"),
		for_doctype="Employee",
		required=True,
	)

	# Employee.date_of_birth is a mandatory field on the doctype itself (checked in the real Employee doctype
	# JSON), so frappe.client.insert would refuse the document even if this validator let it through.
	born = _date(errors, step, "date_of_birth", _("Date of birth"), who.get("date_of_birth"), required=True)
	if born:
		if born > getdate(today()):
			errors.add(step, "date_of_birth", _("Date of birth"), _("Date of birth cannot be in the future."))
			born = None
		elif born < FIRST_PLAUSIBLE_DATE:
			errors.add(
				step,
				"date_of_birth",
				_("Date of birth"),
				_("Date of birth cannot be before {0}.").format(formatdate(FIRST_PLAUSIBLE_DATE)),
			)
			born = None
	values["date_of_birth"] = born.isoformat() if born else None

	if naming == "Employee Number":
		number = _text(
			errors, step, "employee_number", _("Employee number"), who.get("employee_number"), required=True
		)
		# the number becomes the employee's ID (EmployeeMaster.autoname), so it has to be free
		if number and frappe.db.exists("Employee", number):
			errors.add(
				step,
				"employee_number",
				_("Employee number"),
				_("Employee number '{0}' is already used by another employee.").format(number),
			)
			number = None
		values["employee_number"] = number
	else:
		values["employee_number"] = _text(
			errors, step, "employee_number", _("Employee number"), who.get("employee_number")
		)
	return values


def _validate_job(job: dict, who: dict | None, naming: str | None, errors: _Errors) -> dict | None:
	step = "job"
	values = {}
	company = _link(
		errors,
		step,
		"company",
		_("Company"),
		"Company",
		job.get("company"),
		for_doctype="Employee",
		required=True,
	)
	values["company"] = company

	joining = _date(errors, step, "date_of_joining", _("Date of joining"), job.get("date_of_joining"))
	born = getdate(who["date_of_birth"]) if who and who.get("date_of_birth") else None
	if joining:
		latest = getdate(add_days(today(), MAX_FUTURE_JOINING_DAYS))
		if joining < FIRST_PLAUSIBLE_JOINING:
			errors.add(
				step,
				"date_of_joining",
				_("Date of joining"),
				_("Date of joining cannot be before {0}.").format(formatdate(FIRST_PLAUSIBLE_JOINING)),
			)
			joining = None
		elif joining > latest:
			errors.add(
				step,
				"date_of_joining",
				_("Date of joining"),
				_("Date of joining is more than two years away. Check the year."),
			)
			joining = None
		elif born and joining < born:
			errors.add(
				step,
				"date_of_joining",
				_("Date of joining"),
				_("Date of joining cannot be before the date of birth."),
			)
			joining = None
	values["date_of_joining"] = joining.isoformat() if joining else None

	if job.get("employment_type") not in (None, ""):
		if frappe.get_meta("Employee").has_field("employment_type"):
			values["employment_type"] = _link(
				errors,
				step,
				"employment_type",
				_("Employment type"),
				"Employment Type",
				job.get("employment_type"),
				for_doctype="Employee",
			)
		else:
			errors.add(step, "employment_type", _("Employment type"), _("Employment type is not available."))
	values["designation"] = _link(
		errors,
		step,
		"designation",
		_("Designation"),
		"Designation",
		job.get("designation"),
		for_doctype="Employee",
	)

	department = _link(
		errors,
		step,
		"department",
		_("Department"),
		"Department",
		job.get("department"),
		for_doctype="Employee",
	)
	if department and company:
		department_company = frappe.db.get_value("Department", department, "company")
		if department_company != company:
			errors.add(
				step,
				"department",
				_("Department"),
				_("Department '{0}' belongs to {1}, not to {2}.").format(
					department, department_company or _("no company"), company
				),
			)
			department = None
	values["department"] = department

	# the same person twice: an identical name, date of birth and joining date at one company is almost
	# certainly the wizard being run again (for example after a lost connection)
	if (
		who
		and company
		and joining
		and who.get("first_name")
		and who.get("date_of_birth")
		and frappe.has_permission("Employee", "read")
	):
		filters = {
			"first_name": who["first_name"],
			"last_name": who.get("last_name") or "",
			"date_of_birth": who["date_of_birth"],
			"date_of_joining": joining,
			"company": company,
		}
		if who.get("last_name") is None:
			filters["last_name"] = ["in", ["", None]]
		duplicate = frappe.get_list("Employee", filters=filters, limit=1, pluck="name")
		if duplicate:
			errors.add(
				step,
				"date_of_joining",
				_("Date of joining"),
				_(
					"An employee with the same name, date of birth and joining date already exists: {0}. "
					"Open that record instead of adding the person again."
				).format(duplicate[0]),
			)
	return values


def _validate_education(rows, errors: _Errors) -> list[dict]:
	step = "education"
	if not isinstance(rows, list) or not rows:
		errors.add(
			step,
			None,
			None,
			_("Add at least one qualification, or use Skip and Focus will remind you in {0} days.").format(
				REMINDER_DAYS
			),
		)
		return []
	if len(rows) > MAX_EDUCATION_ROWS:
		errors.add(
			step,
			None,
			None,
			_(
				"At most {0} qualifications can be added here. Add the rest on the employee record later."
			).format(MAX_EDUCATION_ROWS),
		)
		return []

	levels = _options(frappe.get_meta("Employee Education"), "level")
	this_year = getdate(today()).year
	clean = []
	for index, row in enumerate(rows):
		if not isinstance(row, dict):
			errors.add(step, None, None, _("Qualification {0} could not be read.").format(index + 1), index)
			continue
		item = {
			"qualification": _text(
				errors,
				step,
				"qualification",
				_("Qualification"),
				row.get("qualification"),
				required=True,
				row=index,
			),
			"school_univ": _text(
				errors,
				step,
				"school_univ",
				_("School or university"),
				row.get("school_univ"),
				required=True,
				max_len=LONG_TEXT_MAX,
				row=index,
				multiline=True,
			),
			"year_of_passing": _whole_number(
				errors,
				step,
				"year_of_passing",
				_("Year of passing"),
				row.get("year_of_passing"),
				low=1900,
				high=this_year + 6,
				row=index,
			),
		}
		level = row.get("level")
		if level not in (None, ""):
			if level in levels:
				item["level"] = level
			else:
				errors.add(
					step,
					"level",
					_("Level"),
					_("Level '{0}' is not one of the choices.").format(str(level)[:40]),
					index,
				)
		clean.append(item)
	return clean


def _validate_address(address: dict, who: dict | None, errors: _Errors) -> dict | None:
	step = "address"
	types = _options(frappe.get_meta("Address"), "address_type")
	values = {}

	address_type = address.get("address_type")
	if address_type in (None, ""):
		errors.add(step, "address_type", _("Address type"), _("Address type is required."))
	elif address_type not in types:
		errors.add(
			step,
			"address_type",
			_("Address type"),
			_("Address type '{0}' is not one of the choices.").format(str(address_type)[:40]),
		)
	else:
		values["address_type"] = address_type

	values["address_line1"] = _text(
		errors, step, "address_line1", _("Address line 1"), address.get("address_line1"), required=True
	)
	values["address_line2"] = _text(
		errors, step, "address_line2", _("Address line 2"), address.get("address_line2")
	)
	values["city"] = _text(errors, step, "city", _("City"), address.get("city"), required=True)
	values["state"] = _text(errors, step, "state", _("State or province"), address.get("state"))
	values["pincode"] = _text(errors, step, "pincode", _("Postal code"), address.get("pincode"), max_len=20)
	values["country"] = _link(
		errors,
		step,
		"country",
		_("Country"),
		"Country",
		address.get("country"),
		for_doctype="Address",
		required=True,
	)
	# Address.autoname refuses an address with no title; the person's name is the natural one
	values["address_title"] = _text(
		errors, step, "address_title", _("Address title"), address.get("address_title")
	) or (_employee_full_name(who) if who else None)
	return values


def _employee_full_name(who: dict | None) -> str | None:
	if not who:
		return None
	name = " ".join(
		part for part in (who.get("first_name"), who.get("middle_name"), who.get("last_name")) if part
	)
	return name or None


def _validate_salary(salary: dict, company, joining, errors: _Errors) -> dict | None:
	step = "salary"
	values = {}
	structure_name = _link(
		errors,
		step,
		"salary_structure",
		_("Salary structure"),
		"Salary Structure",
		salary.get("salary_structure"),
		for_doctype="Salary Structure Assignment",
		required=True,
	)
	structure = None
	if structure_name:
		structure = frappe.db.get_value(
			"Salary Structure",
			structure_name,
			["company", "currency", "docstatus", "is_active"],
			as_dict=True,
		)
		label = _("Salary structure")
		if structure.docstatus != 1:
			errors.add(
				step,
				"salary_structure",
				label,
				_("Salary structure '{0}' is not submitted yet, so it cannot be assigned.").format(
					structure_name
				),
			)
			structure = None
		elif structure.is_active != "Yes":
			errors.add(
				step,
				"salary_structure",
				label,
				_("Salary structure '{0}' is not active.").format(structure_name),
			)
			structure = None
		elif company and structure.company != company:
			errors.add(
				step,
				"salary_structure",
				label,
				_("Salary structure '{0}' belongs to {1}, but this employee joins {2}.").format(
					structure_name, structure.company, company
				),
			)
			structure = None
	values["salary_structure"] = structure_name if structure else None
	values["currency"] = structure.currency if structure else None

	from_date = _date(errors, step, "from_date", _("Start date"), salary.get("from_date"))
	if from_date and joining and from_date < joining:
		errors.add(
			step,
			"from_date",
			_("Start date"),
			_("Start date cannot be before the joining date ({0}).").format(formatdate(joining)),
		)
		from_date = None
	values["from_date"] = from_date.isoformat() if from_date else None

	values["base"] = _money(errors, step, "base", _("Base pay"), salary.get("base"))

	slab = _link(
		errors,
		step,
		"income_tax_slab",
		_("Income tax slab"),
		"Income Tax Slab",
		salary.get("income_tax_slab"),
		for_doctype="Salary Structure Assignment",
	)
	if slab:
		details = frappe.db.get_value(
			"Income Tax Slab", slab, ["docstatus", "disabled", "currency", "company"], as_dict=True
		)
		label = _("Income tax slab")
		if details.docstatus != 1 or details.disabled:
			errors.add(step, "income_tax_slab", label, _("Income tax slab '{0}' is not in use.").format(slab))
			slab = None
		elif structure and details.currency != structure.currency:
			errors.add(
				step,
				"income_tax_slab",
				label,
				_("Income tax slab '{0}' is in {1}, but the salary structure is in {2}.").format(
					slab, details.currency, structure.currency
				),
			)
			slab = None
		elif company and details.company and details.company != company:
			errors.add(
				step,
				"income_tax_slab",
				label,
				_("Income tax slab '{0}' belongs to {1}, not to {2}.").format(slab, details.company, company),
			)
			slab = None
	values["income_tax_slab"] = slab

	if structure_name and structure and not slab and not errors.has(step, "income_tax_slab"):
		from hrms.payroll.doctype.salary_structure_assignment.salary_structure_assignment import (
			get_tax_component,
		)

		tax_component = get_tax_component(structure_name)
		if tax_component:
			errors.add(
				step,
				"income_tax_slab",
				_("Income tax slab"),
				_(
					"An income tax slab is needed because salary structure '{0}' deducts income tax ({1})."
				).format(structure_name, tax_component),
			)
	return values


def _validate_leave(leave: dict, company, joining, errors: _Errors) -> dict | None:
	step = "leave"
	values = {}
	policy = _link(
		errors,
		step,
		"leave_policy",
		_("Leave policy"),
		"Leave Policy",
		leave.get("leave_policy"),
		for_doctype="Leave Policy Assignment",
		required=True,
	)
	if policy and frappe.db.get_value("Leave Policy", policy, "docstatus") != 1:
		errors.add(
			step,
			"leave_policy",
			_("Leave policy"),
			_("Leave policy '{0}' is not submitted yet, so it cannot be assigned.").format(policy),
		)
		policy = None
	values["leave_policy"] = policy

	based_on = leave.get("assignment_based_on")
	if based_on in (None, ""):
		errors.add(
			step,
			"assignment_based_on",
			_("Leave period is based on"),
			_("Choose how the leave period is set."),
		)
		based_on = None
	elif based_on not in ("Leave Period", "Joining Date"):
		errors.add(
			step,
			"assignment_based_on",
			_("Leave period is based on"),
			_("Choose either a leave period or the joining date."),
		)
		based_on = None
	values["assignment_based_on"] = based_on

	effective_from = effective_to = None
	period = None
	if based_on == "Leave Period":
		period = _link(
			errors,
			step,
			"leave_period",
			_("Leave period"),
			"Leave Period",
			leave.get("leave_period"),
			for_doctype="Leave Policy Assignment",
			required=True,
		)
		if period:
			details = frappe.db.get_value(
				"Leave Period", period, ["company", "from_date", "to_date", "is_active"], as_dict=True
			)
			label = _("Leave period")
			if not details.is_active:
				errors.add(step, "leave_period", label, _("Leave period '{0}' is not active.").format(period))
				period = None
			elif company and details.company != company:
				errors.add(
					step,
					"leave_period",
					label,
					_("Leave period '{0}' belongs to {1}, but this employee joins {2}.").format(
						period, details.company, company
					),
				)
				period = None
			elif joining and details.to_date < joining:
				errors.add(
					step,
					"leave_period",
					label,
					_("Leave period '{0}' ends on {1}, before the joining date.").format(
						period, formatdate(details.to_date)
					),
				)
				period = None
			else:
				effective_from, effective_to = details.from_date, details.to_date
	elif based_on == "Joining Date" and joining:
		# LeavePolicyAssignment.set_dates: from the joining date to the end of the month a year later
		effective_from = joining
		effective_to = get_last_day(add_months(joining, 12))
	values["leave_period"] = period
	values["effective_from"] = getdate(effective_from).isoformat() if effective_from else None
	values["effective_to"] = getdate(effective_to).isoformat() if effective_to else None
	return values


def _validate_permissions(cleaned, errors: _Errors) -> None:
	"""The caller may only get what they may create (and, when they ask for submitting, submit)."""
	checks = (
		("address", cleaned.address, "Address", _("addresses")),
		("salary", cleaned.salary, "Salary Structure Assignment", _("salary structure assignments")),
		("leave", cleaned.leave, "Leave Policy Assignment", _("leave policy assignments")),
	)
	for step, data, doctype, plural in checks:
		if data is None:
			continue
		if not frappe.has_permission(doctype, "create"):
			errors.add(
				step,
				None,
				None,
				_("You do not have permission to add {0}. Skip this step and ask someone who can.").format(
					plural
				),
			)
		elif cleaned.submit and step in ("salary", "leave") and not frappe.has_permission(doctype, "submit"):
			errors.add(
				step,
				None,
				None,
				_(
					"You do not have permission to submit {0}. Turn off submitting, or ask someone who can."
				).format(plural),
			)
	if cleaned.skipped and not frappe.has_permission("ToDo", "create"):
		errors.add("review", None, None, _("You do not have permission to add reminders."))


# ----------------------------------------------------------------------------------------------------------
# Check: validate and describe what confirming would do
# ----------------------------------------------------------------------------------------------------------


def check(raw, step: str | None = None) -> dict:
	"""Validate the answers (all of them, or one screen's) and, when they are all fine, describe in plain words
	what confirming will create. Writes nothing."""
	cleaned, errors = validate(raw, only_step=step)
	if errors:
		return {"ok": False, "errors": errors}
	if step:
		return {"ok": True, "errors": []}
	return {"ok": True, "errors": [], **describe(cleaned)}


def describe(cleaned) -> dict:
	who, job = cleaned.who, cleaned.job
	naming = frappe.db.get_single_value("HR Settings", "emp_created_by")
	full_name = _employee_full_name(who)
	lines: list[dict] = []
	documents: list[dict] = []

	joining_text = formatdate(job["date_of_joining"])
	line = _("Add {0} as a new employee of {1}, joining on {2}.").format(
		full_name, job["company"], joining_text
	)
	extras = [
		part
		for part in (
			job.get("designation") and _("Designation: {0}").format(job["designation"]),
			job.get("department") and _("Department: {0}").format(job["department"]),
			job.get("employment_type") and _("Employment type: {0}").format(job["employment_type"]),
		)
		if part
	]
	if extras:
		line += " " + "; ".join(extras) + "."
	if naming == "Employee Number":
		line += " " + _("Their employee ID will be the number {0}.").format(who["employee_number"])
	elif naming == "Full Name":
		line += " " + _("Their employee ID will be their full name.")
	else:
		line += " " + _("Their employee ID is created automatically.")
	lines.append({"step": "job", "text": line})
	documents.append({"doctype": "Employee", "label": _("Employee record"), "action": _("Saved")})

	if cleaned.education:
		names = [
			f"{row['qualification']} ({row['school_univ'].splitlines()[0]}, {row['year_of_passing']})"
			for row in cleaned.education
		]
		lines.append(
			{
				"step": "education",
				"text": _("Record {0} on the employee: {1}.").format(
					_("1 qualification") if len(names) == 1 else _("{0} qualifications").format(len(names)),
					"; ".join(names),
				),
			}
		)

	if cleaned.address:
		address = cleaned.address
		place = ", ".join(
			part for part in (address["address_line1"], address["city"], address["country"]) if part
		)
		lines.append(
			{
				"step": "address",
				"text": _("Save a {0} address ({1}) and link it to the employee.").format(
					address["address_type"], place
				),
			}
		)
		documents.append({"doctype": "Address", "label": _("Address"), "action": _("Saved")})

	submit_action = _("Submitted") if cleaned.submit else _("Draft")
	if cleaned.salary:
		salary = cleaned.salary
		text = _("Assign salary structure {0} from {1}.").format(
			salary["salary_structure"], formatdate(salary["from_date"])
		)
		if salary.get("base") is not None:
			text += " " + _("Base pay: {0}.").format(fmt_money(salary["base"], currency=salary["currency"]))
		if salary.get("income_tax_slab"):
			text += " " + _("Income tax slab: {0}.").format(salary["income_tax_slab"])
		text += " " + (
			_("It will be submitted, so payroll can use it.")
			if cleaned.submit
			else _("It is saved as a draft: submit it before payroll is run.")
		)
		lines.append({"step": "salary", "text": text})
		documents.append(
			{
				"doctype": "Salary Structure Assignment",
				"label": _("Salary structure assignment"),
				"action": submit_action,
			}
		)

	if cleaned.leave:
		leave = cleaned.leave
		text = _("Assign leave policy {0} for {1} to {2}.").format(
			leave["leave_policy"], formatdate(leave["effective_from"]), formatdate(leave["effective_to"])
		)
		text += " " + (
			_("It will be submitted, which creates the employee's leave balances now.")
			if cleaned.submit
			else _("It is saved as a draft: no leave balances exist until you submit it.")
		)
		lines.append({"step": "leave", "text": text})
		documents.append(
			{
				"doctype": "Leave Policy Assignment",
				"label": _("Leave policy assignment"),
				"action": submit_action,
			}
		)

	reminders = []
	if cleaned.skipped:
		due = add_days(today(), REMINDER_DAYS)
		for step in SKIPPABLE_STEPS:
			if step in cleaned.skipped:
				reminders.append({"step": step, "label": step_label(step), "due": due})
		lines.append(
			{
				"step": "review",
				"text": _(
					"Add a reminder to your ToDo list, due {0} ({1} days from today), for: {2}."
				).format(formatdate(due), REMINDER_DAYS, ", ".join(item["label"] for item in reminders)),
			}
		)
		documents.append(
			{"doctype": "ToDo", "label": _("Reminder for each skipped step"), "action": _("Saved")}
		)

	return {"summary": lines, "documents": documents, "reminders": reminders}


# ----------------------------------------------------------------------------------------------------------
# Create: everything or nothing
# ----------------------------------------------------------------------------------------------------------


def create(raw) -> dict:
	"""Create every document the answers describe, atomically.

	Returns `{"ok": True, ...}` with the documents made, or `{"ok": False, ...}` naming the step that failed and
	why. In the failure case nothing has been written: a savepoint taken before the first insert is rolled back.
	"""
	cleaned, errors = validate(raw)
	if errors:
		return _failed_check(errors)

	save_point = f"onb_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	progress = {"step": "employee"}
	try:
		result = _create_documents(cleaned, progress)
	except Exception as exc:
		_rollback(save_point)
		return _failed_create(progress["step"], exc)

	try:
		frappe.db.release_savepoint(save_point)
	except Exception:
		# only a savepoint that no longer exists can fail to release; the work is in the transaction either way
		pass

	result["notes"] = _take_messages()
	return result


def _create_documents(cleaned, progress: dict) -> dict:
	documents = []

	progress["step"] = "employee"
	employee = _insert_employee(cleaned)
	documents.append(_document(employee, "employee", _("Employee record"), "Saved"))

	if cleaned.address:
		progress["step"] = "address"
		address = _insert_address(cleaned, employee)
		documents.append(_document(address, "address", _("Address"), "Saved"))

	if cleaned.salary:
		progress["step"] = "salary"
		assignment = _insert_salary(cleaned, employee)
		documents.append(_document(assignment, "salary", _("Salary structure assignment")))

	if cleaned.leave:
		progress["step"] = "leave"
		leave_assignment = _insert_leave(cleaned, employee)
		documents.append(_document(leave_assignment, "leave", _("Leave policy assignment")))
		if leave_assignment.docstatus == 1:
			for allocation in frappe.get_list(
				"Leave Allocation",
				filters={"leave_policy_assignment": leave_assignment.name},
				fields=["name", "leave_type", "new_leaves_allocated"],
				limit_page_length=50,
			):
				documents.append(
					{
						"step": "leave",
						"doctype": "Leave Allocation",
						"name": allocation.name,
						"label": _("{0}: {1} days").format(
							allocation.leave_type, flt(allocation.new_leaves_allocated)
						),
						"status": _("Submitted"),
					}
				)

	reminders = []
	if cleaned.skipped:
		progress["step"] = "reminders"
		reminders = _insert_reminders(cleaned, employee)

	return {
		"ok": True,
		"employee": employee.name,
		"employee_name": employee.employee_name,
		"documents": documents,
		"reminders": reminders,
	}


def _document(doc, step: str, label: str, status: str | None = None) -> dict:
	if status is None:
		status = _("Submitted") if doc.docstatus == 1 else _("Draft")
	else:
		status = _(status)
	return {"step": step, "doctype": doc.doctype, "name": doc.name, "label": label, "status": status}


def _insert_employee(cleaned) -> "frappe.model.document.Document":
	who, job = cleaned.who, cleaned.job
	values = {
		"doctype": "Employee",
		"status": "Active",
		**{key: value for key, value in who.items() if value not in (None, "")},
		**{key: value for key, value in job.items() if value not in (None, "")},
	}
	if cleaned.education:
		values["education"] = [
			{key: value for key, value in row.items() if value not in (None, "")} for row in cleaned.education
		]
	doc = frappe.get_doc(values)
	doc.insert()
	return doc


def _insert_address(cleaned, employee) -> "frappe.model.document.Document":
	values = {key: value for key, value in cleaned.address.items() if value not in (None, "")}
	doc = frappe.get_doc(
		{
			"doctype": "Address",
			**values,
			"links": [{"link_doctype": "Employee", "link_name": employee.name}],
		}
	)
	doc.insert()
	return doc


def _insert_salary(cleaned, employee) -> "frappe.model.document.Document":
	salary = cleaned.salary
	doc = frappe.get_doc(
		{
			"doctype": "Salary Structure Assignment",
			"employee": employee.name,
			"company": employee.company,
			"salary_structure": salary["salary_structure"],
			"currency": salary["currency"],
			"from_date": salary["from_date"],
			**({"base": salary["base"]} if salary.get("base") is not None else {}),
			**({"income_tax_slab": salary["income_tax_slab"]} if salary.get("income_tax_slab") else {}),
		}
	)
	doc.insert()
	if cleaned.submit:
		doc.submit()
	return doc


def _insert_leave(cleaned, employee) -> "frappe.model.document.Document":
	leave = cleaned.leave
	doc = frappe.get_doc(
		{
			"doctype": "Leave Policy Assignment",
			"employee": employee.name,
			"leave_policy": leave["leave_policy"],
			"assignment_based_on": leave["assignment_based_on"],
			"leave_period": leave.get("leave_period"),
			"effective_from": leave["effective_from"],
			"effective_to": leave["effective_to"],
		}
	)
	doc.insert()
	if cleaned.submit:
		doc.submit()
	return doc


def _insert_reminders(cleaned, employee) -> list[dict]:
	due = add_days(today(), REMINDER_DAYS)
	reminders = []
	for step in SKIPPABLE_STEPS:
		if step not in cleaned.skipped:
			continue
		description = _("Complete the {0} step for the new employee {1} ({2}).").format(
			frappe.utils.escape_html(step_label(step)),
			frappe.utils.escape_html(employee.employee_name),
			frappe.utils.escape_html(employee.name),
		)
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": description,
				"reference_type": "Employee",
				"reference_name": employee.name,
				"date": due,
				"allocated_to": frappe.session.user,
				"assigned_by": frappe.session.user,
				"status": "Open",
				"priority": "Medium",
			}
		)
		todo.insert()
		reminders.append({"step": step, "label": step_label(step), "name": todo.name, "due": due})
	return reminders


def _rollback(save_point: str) -> None:
	try:
		frappe.db.rollback(save_point=save_point)
	except Exception:
		# The savepoint is gone (a statement that commits implicitly ended the transaction): a full rollback is
		# the only way left to keep "nothing created".
		frappe.db.rollback()


def _take_messages() -> list[str]:
	"""Warnings the documents raised while saving, as plain text. They are shown on the summary instead of as
	pop-ups on top of it."""
	notes = []
	for entry in frappe.get_message_log():
		message = entry.get("message") if isinstance(entry, dict) else str(entry)
		message = strip_html_tags(str(message or "")).strip()
		if message and message not in notes:
			notes.append(message[:400])
	frappe.clear_messages()
	return notes[:10]


def _failed_check(errors: list[dict]) -> dict:
	first = errors[0]
	return {
		"ok": False,
		"stage": "check",
		"step": first["step"],
		"step_label": step_label(first["step"]),
		"message": _("Nothing was created. {0}").format(first["message"]),
		"errors": errors,
		"created": [],
	}


def _failed_create(step: str, exc: Exception) -> dict:
	frappe.clear_messages()
	reason = _plain_reason(exc)
	return {
		"ok": False,
		"stage": "create",
		"step": step,
		"step_label": step_label(step) if step in STEP_LABELS else _(CREATE_DOCTYPES.get(step, step)),
		"message": _('Nothing was created. The step "{0}" failed: {1}').format(
			step_label(step) if step in STEP_LABELS else _(CREATE_DOCTYPES.get(step, step)), reason
		),
		"reason": reason,
		"errors": [],
		"created": [],
	}


def _plain_reason(exc: Exception) -> str:
	"""A failure in words a person can act on, never a traceback."""
	if isinstance(exc, frappe.PermissionError):
		detail = strip_html_tags(str(exc)).strip()
		return _("You do not have permission to do this.") + (f" ({detail[:200]})" if detail else "")
	if isinstance(exc, frappe.DuplicateEntryError | frappe.UniqueValidationError):
		return _("That record already exists, so it was not added again.")
	if isinstance(exc, frappe.ValidationError):
		detail = strip_html_tags(str(exc)).strip()
		if detail:
			return detail[:500]

	try:
		log = frappe.log_error(
			title="Focus employee onboarding wizard failed", message=frappe.get_traceback()
		)
		reference = getattr(log, "name", None)
	except Exception:
		reference = None
	message = _("Something unexpected went wrong on the server.")
	if reference:
		message += " " + _("Tell your administrator the Error Log reference is {0}.").format(reference)
	return message
