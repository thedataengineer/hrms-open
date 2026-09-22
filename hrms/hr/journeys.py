# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Journeys: onboarding, offboarding, promotion, transfer, return-from-leave and custom life-event flows
built as orchestrated, multi-owner sequences (Workday "Journeys" / Oracle "Guided journeys").

Design notes, so the next person does not have to re-derive them:

- Journey / Journey Template carry no DocPerm for the "Employee" role. An employee's own read access to
  a specific Journey comes from the automatic DocShare that `frappe.desk.form.assign_to.add` creates when
  it assigns them a ToDo they could not already read (see `frappe/desk/form/assign_to.py`, `_add`).  That
  is "self-service through the controller, not by widening DocPerm": every whitelisted function here still
  checks the caller's permission itself before it does anything.
- A task only gets a ToDo (and only then is its owner actually resolved) once its dependency is satisfied
  ("open tasks lazily"). Until then it sits as status "Blocked" with no owner, so a person only ever sees
  what is next, never the whole list of everything that could eventually happen to them.
- Every list this module returns is bounded (`limit_page_length`) and every write is scoped to one Journey
  or one batch of overdue tasks, so nothing here loops per-employee inside a request or scans an unbounded
  table.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.desk.form import assign_to
from frappe.utils import add_days, cint, getdate, now_datetime

ACTIVE_STATUSES = ("Not Started", "In Progress")
HR_ROLES = ("HR Manager", "HR User")
MAX_APPLICABLE_TEMPLATES = 20
MAX_EMPLOYEE_JOURNEYS = 20
MAX_HR_QUEUE_USERS = 10
REMINDER_BATCH_SIZE = 500


class JourneyPermissionError(frappe.PermissionError):
	pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_hr(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles & set(HR_ROLES)) or "System Manager" in roles


def _is_system(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _check_str(value, fieldname: str, max_length: int = 140) -> str:
	value = "" if value is None else str(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(fieldname))
	if len(value) > max_length:
		frappe.throw(_("{0} is too long.").format(fieldname))
	return value


def get_hr_queue_users(limit: int = MAX_HR_QUEUE_USERS) -> list[str]:
	"""Enabled users holding the HR Manager role, for steps whose owner cannot be resolved."""
	return get_role_users("HR Manager", limit)


def get_manager_user(employee: str) -> str | None:
	"""The user_id of `employee`'s direct manager (`Employee.reports_to`), or None if it cannot be resolved."""
	reports_to = frappe.db.get_value("Employee", employee, "reports_to")
	if not reports_to:
		return None
	manager_user, manager_status = frappe.db.get_value("Employee", reports_to, ["user_id", "status"]) or (
		None,
		None,
	)
	if not manager_user or manager_status != "Active":
		return None
	if not frappe.db.get_value("User", manager_user, "enabled"):
		return None
	return manager_user


def get_role_users(role: str, limit: int = MAX_HR_QUEUE_USERS) -> list[str]:
	"""Enabled users holding `role`, bounded and ordered for a deterministic pick."""
	has_role = frappe.qb.DocType("Has Role")
	user = frappe.qb.DocType("User")
	return (
		frappe.qb.from_(has_role)
		.join(user)
		.on(has_role.parent == user.name)
		.select(user.name)
		.distinct()
		.where((has_role.role == role) & (has_role.parenttype == "User") & (user.enabled == 1))
		.where(user.name != "Administrator")
		.orderby(user.name)
		.limit(cint(limit) or MAX_HR_QUEUE_USERS)
		.run(pluck=True)
	)


def resolve_owner(employee_doc, owner_type: str, owner_role: str | None, configured_user: str | None):
	"""Return (user_or_None, unresolved: bool).

	Always resolves to at most one user, so there is exactly one ToDo and one clear owner per task — never
	several people quietly assigned the same thing. When a step's chosen owner cannot be resolved (HR or
	Role with nobody in it, a manager-less employee, ...) it falls back to the first person in the HR
	queue (deterministic, alphabetical) and is flagged `owner_unresolved` so it is never silently dropped.
	"""
	if owner_type == "Employee":
		user = employee_doc.user_id
		if user and frappe.db.get_value("User", user, "enabled"):
			return user, False

	elif owner_type == "Employee's Manager":
		user = get_manager_user(employee_doc.name)
		if user:
			return user, False

	elif owner_type == "HR":
		users = get_hr_queue_users()
		if users:
			return users[0], False

	elif owner_type == "Role" and owner_role:
		users = get_role_users(owner_role)
		if users:
			return users[0], False

	elif owner_type == "User":
		if configured_user and frappe.db.get_value("User", configured_user, "enabled"):
			return configured_user, False

	hr_queue = get_hr_queue_users()
	return (hr_queue[0] if hr_queue else None), True


# ---------------------------------------------------------------------------
# Starting a journey
# ---------------------------------------------------------------------------


def _matching_templates(journey_type: str, trigger: str, employee_doc) -> list[str]:
	filters = {
		"journey_type": journey_type,
		"trigger": trigger,
		"disabled": 0,
		"company": employee_doc.company,
	}
	templates = frappe.get_all(
		"Journey Template",
		filters=filters,
		fields=["name", "department", "designation"],
		limit_page_length=MAX_APPLICABLE_TEMPLATES,
	)
	matched = []
	for template in templates:
		if template.department and template.department != employee_doc.department:
			continue
		if template.designation and template.designation != employee_doc.designation:
			continue
		matched.append(template.name)
	return matched


@frappe.whitelist(methods=["GET"])
def get_applicable_templates(employee: str) -> list[dict]:
	"""Templates a person could manually start for `employee` right now (for the "Start a journey" dialog)."""
	employee = _check_str(employee, "Employee")
	frappe.has_permission("Employee", "read", doc=employee, throw=True)

	employee_doc = frappe.get_cached_doc("Employee", employee)
	templates = frappe.get_all(
		"Journey Template",
		filters={"disabled": 0, "company": employee_doc.company},
		fields=["name", "title", "journey_type", "department", "designation"],
		order_by="title asc",
		limit_page_length=MAX_APPLICABLE_TEMPLATES,
	)
	return [
		t
		for t in templates
		if (not t.department or t.department == employee_doc.department)
		and (not t.designation or t.designation == employee_doc.designation)
	]


@frappe.whitelist(methods=["POST"])
def start_journey(
	template: str,
	employee: str,
	start_date: str | None = None,
	triggered_by_doctype: str | None = None,
	triggered_by: str | None = None,
) -> str:
	"""Start a journey for `employee` from `template`. Returns the Journey name.

	Idempotent when `triggered_by_doctype`/`triggered_by` are given: a second call for the same trigger
	document returns the existing Journey instead of creating a duplicate (event handlers may run more
	than once for the same document).
	"""
	template = _check_str(template, "Journey Template")
	employee = _check_str(employee, "Employee")
	calling_from_event = bool(triggered_by_doctype and triggered_by)

	if not calling_from_event and not _is_hr():
		frappe.throw(_("Only HR can start a journey."), JourneyPermissionError)

	if not frappe.db.exists("Employee", employee):
		frappe.throw(_("Employee {0} does not exist.").format(employee))
	if not frappe.db.exists("Journey Template", template):
		frappe.throw(_("Journey Template {0} does not exist.").format(template))

	if calling_from_event:
		existing = frappe.db.exists(
			"Journey",
			{
				"journey_template": template,
				"employee": employee,
				"triggered_by_doctype": triggered_by_doctype,
				"triggered_by": triggered_by,
				"status": ("!=", "Cancelled"),
			},
		)
		if existing:
			return existing

	template_doc = frappe.get_doc("Journey Template", template)
	if template_doc.disabled:
		frappe.throw(_("Journey Template {0} is disabled.").format(template))
	employee_doc = frappe.get_cached_doc("Employee", employee)

	journey = frappe.new_doc("Journey")
	journey.journey_template = template_doc.name
	journey.employee = employee_doc.name
	journey.start_date = getdate(start_date) if start_date else getdate()
	journey.status = "Not Started"
	journey.triggered_by_doctype = triggered_by_doctype
	journey.triggered_by = triggered_by

	for step in template_doc.steps:
		journey.append(
			"tasks",
			{
				"title": step.title,
				"description": step.description,
				"status": "Blocked",
				"required": step.required,
				"action": step.action,
				"reference_doctype": step.reference_doctype,
				"owner_type": step.owner_type,
				"owner_role": step.owner_role,
				"configured_user": step.owner_user,
				"depends_on": step.depends_on,
				"due_date": add_days(journey.start_date, cint(step.due_offset_days)),
			},
		)

	journey.insert(ignore_permissions=True)
	_open_ready_tasks(journey, employee_doc)
	_recompute_progress(journey)
	journey.save(ignore_permissions=True)
	return journey.name


# ---------------------------------------------------------------------------
# Opening tasks lazily
# ---------------------------------------------------------------------------


def _open_ready_tasks(journey, employee_doc) -> bool:
	"""Open every Blocked task whose dependency is satisfied. Mutates `journey.tasks` in place.

	Returns True if anything changed. Safe to call repeatedly (idempotent): a task that is already Open,
	Done or Skipped is left alone, and a task is only ever opened once (it already carries a `todo` once
	opened, and never re-enters "Blocked").
	"""
	changed = False
	done_or_skipped_titles = {t.title for t in journey.tasks if t.status in ("Done", "Skipped")}

	for task in journey.tasks:
		if task.status != "Blocked":
			continue
		if task.depends_on and task.depends_on not in done_or_skipped_titles:
			continue

		user, unresolved = resolve_owner(employee_doc, task.owner_type, task.owner_role, task.configured_user)
		task.owner_user = user
		task.owner_unresolved = 1 if unresolved else 0
		task.status = "Open"
		changed = True

		if user:
			task.todo = _assign_task(journey, task, user)

	return changed


def _assign_task(journey, task, todo_user: str) -> str | None:
	description = task.title
	if journey.employee:
		employee_name = frappe.db.get_value("Employee", journey.employee, "employee_name")
		if employee_name:
			description = f"{task.title}: {employee_name}"

	result = assign_to._add(
		{
			"assign_to": [todo_user],
			"doctype": "Journey",
			"name": journey.name,
			"description": description,
			"date": task.due_date,
			"notify": 0,
		},
		ignore_permissions=True,
	)
	# `assign_to.get()` (what `_add` returns) is filtered to Open ToDos for this reference, newest first;
	# the one for `todo_user` is the one we just made (or the pre-existing Open one for them, if a task
	# were somehow reopened for the same person — `_add` de-duplicates instead of creating a second row).
	for todo in result:
		if todo.owner == todo_user:
			return todo.name
	return result[0].name if result else None


# ---------------------------------------------------------------------------
# Completing / skipping tasks
# ---------------------------------------------------------------------------


def _can_act_on_task(task, journey) -> bool:
	user = frappe.session.user
	if _is_system(user) or _is_hr(user):
		return True
	return bool(task.owner_user) and task.owner_user == user


def _get_journey_and_task(journey: str, task: str):
	journey = _check_str(journey, "Journey")
	task = _check_str(task, "Task")
	if not frappe.db.exists("Journey", journey):
		frappe.throw(_("Journey {0} does not exist.").format(journey))

	journey_doc = frappe.get_doc("Journey", journey)
	task_row = None
	for row in journey_doc.tasks:
		if row.name == task:
			task_row = row
			break
	if not task_row:
		frappe.throw(_("Task {0} was not found on this journey.").format(task))
	return journey_doc, task_row


def _transition_task(journey: str, task: str, new_status: str, note: str | None = None) -> dict:
	journey_doc, task_row = _get_journey_and_task(journey, task)

	if journey_doc.status in ("Completed", "Cancelled"):
		frappe.throw(_("This journey is already {0}.").format(journey_doc.status))
	if task_row.status not in ("Open",):
		frappe.throw(_("Only an open task can be marked {0}.").format(new_status.lower()))
	if not _can_act_on_task(task_row, journey_doc):
		frappe.throw(
			_("Only the task's owner or HR can mark this task {0}.").format(new_status.lower()),
			JourneyPermissionError,
		)

	if note is not None:
		note = str(note).strip()[:500]
		task_row.note = note or task_row.note

	task_row.status = new_status
	task_row.completed_on = now_datetime()
	task_row.completed_by = frappe.session.user

	if task_row.todo:
		assign_to.set_status(
			"Journey", journey_doc.name, todo=task_row.todo, status="Closed", ignore_permissions=True
		)

	employee_doc = frappe.get_cached_doc("Employee", journey_doc.employee)
	_open_ready_tasks(journey_doc, employee_doc)
	_recompute_progress(journey_doc)
	journey_doc.save(ignore_permissions=True)

	return {
		"journey": journey_doc.name,
		"status": journey_doc.status,
		"progress_percent": journey_doc.progress_percent,
	}


@frappe.whitelist(methods=["POST"])
def complete_task(journey: str, task: str, note: str | None = None) -> dict:
	return _transition_task(journey, task, "Done", note)


@frappe.whitelist(methods=["POST"])
def skip_task(journey: str, task: str, note: str | None = None) -> dict:
	return _transition_task(journey, task, "Skipped", note)


# ---------------------------------------------------------------------------
# Progress / status
# ---------------------------------------------------------------------------


def _recompute_progress(journey) -> None:
	if journey.status == "Cancelled":
		return

	total = len(journey.tasks)
	if total == 0:
		journey.progress_percent = 100
		journey.status = "Completed"
		return

	resolved = sum(1 for t in journey.tasks if t.status in ("Done", "Skipped"))
	journey.progress_percent = round(100 * resolved / total, 2)

	if resolved == total:
		journey.status = "Completed"
	elif resolved > 0 or any(t.status == "Open" for t in journey.tasks):
		journey.status = "In Progress"
	else:
		journey.status = "Not Started"


@frappe.whitelist(methods=["POST"])
def cancel_journey(journey: str, reason: str | None = None) -> dict:
	journey = _check_str(journey, "Journey")
	if not _is_hr():
		frappe.throw(_("Only HR can cancel a journey."), JourneyPermissionError)

	journey_doc = frappe.get_doc("Journey", journey)
	if journey_doc.status in ("Completed", "Cancelled"):
		return {"journey": journey_doc.name, "status": journey_doc.status}

	for task in journey_doc.tasks:
		if task.todo:
			assign_to.set_status(
				"Journey", journey_doc.name, todo=task.todo, status="Cancelled", ignore_permissions=True
			)

	journey_doc.status = "Cancelled"
	journey_doc.save(ignore_permissions=True)

	if reason:
		journey_doc.add_comment("Info", _("Cancelled: {0}").format(str(reason).strip()[:500]))

	return {"journey": journey_doc.name, "status": journey_doc.status}


# ---------------------------------------------------------------------------
# Reading journeys (client / self-service)
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["GET"])
def get_employee_journeys(employee: str, include_completed: bool | int | str = 0) -> list[dict]:
	employee = _check_str(employee, "Employee")
	frappe.has_permission("Employee", "read", doc=employee, throw=True)

	filters = {"employee": employee}
	if not cint(include_completed):
		filters["status"] = ("in", ACTIVE_STATUSES)

	journeys = frappe.get_all(
		"Journey",
		filters=filters,
		fields=["name", "journey_template", "journey_type", "status", "progress_percent", "start_date"],
		order_by="modified desc",
		limit_page_length=MAX_EMPLOYEE_JOURNEYS,
	)
	for journey in journeys:
		next_task = frappe.db.get_value(
			"Journey Task",
			{"parent": journey.name, "parenttype": "Journey", "status": "Open"},
			["title", "due_date"],
			order_by="due_date asc",
			as_dict=True,
		)
		journey["next_task"] = next_task
	return journeys


# ---------------------------------------------------------------------------
# Event handlers (registered by the lead in hooks.py; see the final report)
# ---------------------------------------------------------------------------


def _start_from_trigger(journey_type: str, trigger: str, employee: str, source_doc) -> None:
	if not employee or not frappe.db.exists("Employee", employee):
		return
	try:
		employee_doc = frappe.get_cached_doc("Employee", employee)
		for template_name in _matching_templates(journey_type, trigger, employee_doc):
			start_journey(
				template=template_name,
				employee=employee,
				triggered_by_doctype=source_doc.doctype,
				triggered_by=source_doc.name,
			)
	except Exception:
		# A bad template must never block the document that triggered it (an Employee being created, a
		# promotion being submitted, ...).
		frappe.log_error(
			title="Journeys: could not start journey from trigger",
			message=frappe.get_traceback(),
		)


def on_employee_after_insert(doc, method: str | None = None) -> None:
	"""Registered on Employee's `after_insert`. Starts any matching Onboarding journeys."""
	_start_from_trigger("Onboarding", "Employee Joined", doc.name, doc)


def on_employee_separation_on_submit(doc, method: str | None = None) -> None:
	"""Registered on Employee Separation's `on_submit`. Starts any matching Offboarding journeys."""
	_start_from_trigger("Offboarding", "Employee Separation", doc.employee, doc)


def on_employee_promotion_on_submit(doc, method: str | None = None) -> None:
	"""Registered on Employee Promotion's `on_submit`. Starts any matching Promotion journeys."""
	_start_from_trigger("Promotion", "Employee Promotion", doc.employee, doc)


def on_employee_transfer_on_submit(doc, method: str | None = None) -> None:
	"""Registered on Employee Transfer's `on_submit`. Starts any matching Transfer journeys."""
	_start_from_trigger("Transfer", "Employee Transfer", doc.employee, doc)


# ---------------------------------------------------------------------------
# Daily job: in-app overdue reminders
# ---------------------------------------------------------------------------


def send_overdue_reminders(batch_size: int = REMINDER_BATCH_SIZE) -> int:
	"""Write a Notification Log entry for each overdue, open task whose template wants reminders.

	Incremental and bounded: only tasks with `last_reminder_sent_on` unset or before today are
	considered (the field is the watermark), the query is capped at `batch_size` rows, and it is ordered
	oldest-due-first so a big backlog is worked down over several days rather than starved by new misses.
	At most one Notification Log per task per day, in-app only (never email).
	"""
	journey_task = frappe.qb.DocType("Journey Task")
	journey = frappe.qb.DocType("Journey")
	template = frappe.qb.DocType("Journey Template")

	cutoff = getdate()
	rows = (
		frappe.qb.from_(journey_task)
		.join(journey)
		.on(journey_task.parent == journey.name)
		.join(template)
		.on(journey.journey_template == template.name)
		.select(
			journey_task.name,
			journey_task.parent,
			journey_task.title,
			journey_task.owner_user,
			journey_task.due_date,
		)
		.where(journey_task.parenttype == "Journey")
		.where(journey_task.status == "Open")
		.where(journey_task.owner_user.notnull())
		.where(journey_task.due_date < cutoff)
		.where((journey_task.last_reminder_sent_on.isnull()) | (journey_task.last_reminder_sent_on < cutoff))
		.where(journey.status.isin(ACTIVE_STATUSES))
		.where(template.send_reminders == 1)
		.orderby(journey_task.due_date)
		.limit(cint(batch_size) or REMINDER_BATCH_SIZE)
		.run(as_dict=True)
	)

	sent = 0
	for row in rows:
		_create_overdue_notification(row)
		frappe.db.set_value("Journey Task", row.name, "last_reminder_sent_on", cutoff, update_modified=False)
		sent += 1

	return sent


def _create_overdue_notification(row) -> None:
	subject = _("{0} is overdue on {1}").format(frappe.bold(row.title), frappe.bold(row.parent))
	notification = frappe.new_doc("Notification Log")
	notification.for_user = row.owner_user
	notification.from_user = "Administrator"
	notification.type = "Alert"  # never emailed (frappe.hooks.notification_skip_email_types)
	notification.document_type = "Journey"
	notification.document_name = row.parent
	notification.subject = subject
	notification.insert(ignore_permissions=True)


def daily() -> None:
	"""Registered under `scheduler_events.daily`."""
	send_overdue_reminders()


# ---------------------------------------------------------------------------
# Building a Journey Template from an existing boarding template
# ---------------------------------------------------------------------------

_BOARDING_TEMPLATE_JOURNEY_TYPE = {
	"Employee Onboarding Template": ("Onboarding", "Employee Joined"),
	"Employee Separation Template": ("Offboarding", "Employee Separation"),
}


@frappe.whitelist(methods=["POST"])
def build_template_from_boarding_template(source_doctype: str, source_name: str) -> str:
	"""Copy an Employee Onboarding Template / Employee Separation Template's activities into a new
	Journey Template. Returns the new Journey Template's name."""
	source_doctype = _check_str(source_doctype, "Source DocType")
	source_name = _check_str(source_name, "Source Name")

	if source_doctype not in _BOARDING_TEMPLATE_JOURNEY_TYPE:
		frappe.throw(_("{0} is not a boarding template.").format(source_doctype))
	if not _is_hr():
		frappe.throw(_("Only HR can build a journey template."), JourneyPermissionError)

	frappe.has_permission(source_doctype, "read", doc=source_name, throw=True)
	source = frappe.get_doc(source_doctype, source_name)
	journey_type, trigger = _BOARDING_TEMPLATE_JOURNEY_TYPE[source_doctype]

	template = frappe.new_doc("Journey Template")
	template.title = _("{0} (from {1})").format(source.title, source_doctype)
	template.journey_type = journey_type
	template.trigger = trigger
	template.company = source.company or frappe.db.get_single_value("Global Defaults", "default_company")
	template.department = source.department
	template.designation = source.designation

	for activity in source.activities:
		if activity.user:
			owner_type, owner_role, owner_user = "User", None, activity.user
		elif activity.role:
			owner_type, owner_role, owner_user = "Role", activity.role, None
		else:
			owner_type, owner_role, owner_user = "HR", None, None

		template.append(
			"steps",
			{
				"title": activity.activity_name,
				"description": activity.description,
				"owner_type": owner_type,
				"owner_role": owner_role,
				"owner_user": owner_user,
				"due_offset_days": cint(activity.begin_on),
				"required": activity.required_for_employee_creation,
				"action": "Task",
			},
		)

	template.insert(ignore_permissions=True)
	return template.name
