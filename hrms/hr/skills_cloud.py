# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Skills Cloud: deterministic skill inference and the talent marketplace matching engine.

No language model is involved anywhere in this file. A skill is only ever suggested because a real
sentence, written by or about the employee in an existing HRMS document, contains that skill's name
or one of its declared aliases as a whole word (or whole phrase). The sentence is stored on the
suggestion so a person can always see *why* it was suggested, never just a bare claim.

Evidence sources (verified against the real doctype JSON before being used here; see the module
docstring of each `_..._evidence` function for the exact fields read):
  - Employee Performance Feedback (submitted) - reviewer's own words about the employee
  - Goal                                       - the employee's own stated goal
  - Training Result (submitted)                - a completed, graded training course
  - Timesheet (submitted)                      - the employee's own description of logged work

Scoring
-------
Suggestion confidence (0-100) for one piece of evidence:

    confidence = round(SOURCE_WEIGHT[source_doctype] * recency_factor(evidence_date))

recency_factor halves every RECENCY_HALF_LIFE_DAYS days and never drops below MIN_RECENCY_FACTOR:

    recency_factor(date) = max(MIN_RECENCY_FACTOR, 0.5 ** (age_in_days / RECENCY_HALF_LIFE_DAYS))

Each suggestion cites exactly one piece of evidence (one sentence, one source document). If several
different documents mention the same skill for the same employee, each produces its own suggestion
row - the Employee form groups them and shows the strongest one first, so multiple independent
mentions read as stronger evidence without the engine ever inventing a combined number.

Marketplace match % between an employee and a Talent Opportunity:

    credit(skill_row) = 1.0                                   if no minimum proficiency is set and
                                                                 the employee has any proficiency > 0
                       = min(1.0, employee_proficiency /
                             minimum_proficiency)               otherwise (0 if the employee lacks it)

    match % = round(100 * (0.75 * mean(credit(r) for r in required_rows)
                          + 0.25 * mean(credit(r) for r in nice_to_have_rows)))

    (a category with no rows is left out and the other category is used alone; an opportunity with
    no skill rows at all cannot be matched and scores 0)

Scale
-----
- The inference scan (`refresh_suggestions`) is incremental: each evidence source is read only past
  a watermark (that source's highest `modified` seen so far), in a single bounded query
  (`limit_page_length=batch_size`), so a run costs O(batch_size) per source regardless of how much
  history exists. The watermark is kept in `frappe.cache()` (this feature has no Settings DocType of
  its own in scope); losing the cache only means the next run rescans from the start of the table in
  bounded batches again - the (employee, skill, source_doctype, source_name) uniqueness guard on
  Skill Suggestion makes that safe (no duplicate rows), just occasionally more work.
- The word-matcher compiles ALL skills and aliases into one alternation regex once per run (not once
  per row, and not once per skill), so matching a batch of evidence rows is O(batch_size) regex scans,
  not O(rows * skills).
- `match_people` never loops over every employee: it starts from the opportunity's own (small) skill
  list, fetches only the Employee Skill rows for those specific skills in one query, and scores only
  the employees that query returns. An employee with none of the wanted skills costs nothing.
"""

import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, now_datetime, nowdate, strip_html

# ---------------------------------------------------------------------------
# Tunables (no dedicated Settings DocType is in this feature's file scope; see module docstring)
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 2000
MAX_SUGGESTIONS_PER_EMPLOYEE = 30
DEFAULT_MATCH_LIMIT = 20
MAX_MATCH_LIMIT = 100
MAX_CANDIDATE_SCAN = 1000
MAX_OPPORTUNITY_SCAN = 300
# Accepting a suggestion confirms the skill exists but not exactly how strong it is; 0.6 is the raw
# Rating value Frappe stores for "3 out of 5" (Rating fields are stored as a 0-1 fraction; see
# frappe/model/document.py:_fix_rating_value). The employee or HR can correct it afterwards.
DEFAULT_ACCEPTED_PROFICIENCY = 0.6

SOURCE_WEIGHT = {
	"Employee Performance Feedback": 90,
	"Training Result": 75,
	"Goal": 55,
	"Timesheet": 40,
}
RECENCY_HALF_LIFE_DAYS = 180
MIN_RECENCY_FACTOR = 0.15

_CACHE_PREFIX = "skills_cloud_watermark:"
_WORD_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9])"
_WORD_BOUNDARY_AFTER = r"(?![A-Za-z0-9])"


# ---------------------------------------------------------------------------
# Permissions
#
# Skill Suggestion and the sensitive parts of Talent Opportunity (who's interested) deliberately do
# NOT grant the Employee role DocType-level read/write (see their DocType JSON): a person's inferred
# skills, and who is interested in a role, are personal enough that "any user with the Employee role"
# is too wide. Every employee-facing entry point below is a whitelisted function that checks access
# itself, then reads/writes with ignore_permissions=True - the "controller, not a wider DocPerm"
# pattern the brief asks for, and the same shape already used in leave_application.py (see e.g.
# hrms/hr/doctype/leave_application/leave_application.py:1020 and :1664-1668).
# ---------------------------------------------------------------------------


def _get_employee_for_user(user: str | None = None) -> str | None:
	user = user or frappe.session.user
	return frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, "name")


def _is_hr(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _is_manager_of(employee: str, user: str | None = None) -> bool:
	"""True if `user` manages `employee`, directly or transitively.

	Employee is a nested-set tree keyed by `reports_to` (see erpnext/setup/doctype/employee/employee.json
	`is_tree: 1`, and the same lft/rgt range check already used by
	hrms/hr/page/organizational_chart/organizational_chart.py:get_connections). A single indexed
	range lookup, not a walk up the chain.
	"""
	my_employee = _get_employee_for_user(user)
	if not my_employee or my_employee == employee:
		return False
	bounds = frappe.db.get_value("Employee", my_employee, ["lft", "rgt"])
	target = frappe.db.get_value("Employee", employee, ["lft", "rgt"])
	if not bounds or not target:
		return False
	my_lft, my_rgt = bounds
	emp_lft, emp_rgt = target
	return my_lft is not None and emp_lft is not None and my_lft < emp_lft and emp_rgt < my_rgt


def can_view_employee_data(employee: str, user: str | None = None) -> bool:
	"""Read access: the employee themself, their manager (any level up), or HR."""
	user = user or frappe.session.user
	if _is_hr(user):
		return True
	my_employee = _get_employee_for_user(user)
	if my_employee and my_employee == employee:
		return True
	return _is_manager_of(employee, user)


def can_act_as_employee(employee: str, user: str | None = None) -> bool:
	"""Write access for self-service actions (accept/dismiss a suggestion, express interest): the
	employee themself, or HR - never a manager acting for someone else."""
	user = user or frappe.session.user
	if _is_hr(user):
		return True
	my_employee = _get_employee_for_user(user)
	return bool(my_employee and my_employee == employee)


def _require_view_access(employee: str, user: str | None = None) -> None:
	if not can_view_employee_data(employee, user):
		frappe.throw(_("You are not permitted to view this employee's skills data"), frappe.PermissionError)


def _require_act_access(employee: str, user: str | None = None) -> None:
	if not can_act_as_employee(employee, user):
		frappe.throw(_("You are not permitted to act on this employee's skills data"), frappe.PermissionError)


def _require_opportunity_owner_or_hr(opportunity: str, user: str | None = None) -> None:
	user = user or frappe.session.user
	if _is_hr(user):
		return
	owner_employee = frappe.db.get_value("Talent Opportunity", opportunity, "owner_employee")
	my_employee = _get_employee_for_user(user)
	if not owner_employee or not my_employee or owner_employee != my_employee:
		frappe.throw(_("Only the opportunity owner or HR can view its candidates"), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Watermarks (see module docstring: cache-backed, self-healing)
# ---------------------------------------------------------------------------


def _get_watermark(source_doctype: str) -> str | None:
	value = frappe.cache().get_value(_CACHE_PREFIX + source_doctype)
	return cstr(value) if value else None


def _set_watermark(source_doctype: str, value) -> None:
	if value:
		frappe.cache().set_value(_CACHE_PREFIX + source_doctype, cstr(value))


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------


def _compile_matcher(terms_to_skill: dict[str, str]) -> re.Pattern | None:
	if not terms_to_skill:
		return None
	# Longest term first, so e.g. "React Native" is preferred over "React" at the same position.
	ordered = sorted(terms_to_skill, key=len, reverse=True)
	alternation = "|".join(re.escape(term) for term in ordered)
	return re.compile(_WORD_BOUNDARY_BEFORE + "(" + alternation + ")" + _WORD_BOUNDARY_AFTER, re.IGNORECASE)


def get_skill_matcher() -> tuple[re.Pattern | None, dict[str, str]]:
	"""Build the (compiled regex, lowercase-term -> Skill name) pair used to find skill mentions.

	Called once per `refresh_suggestions` run (not once per evidence row, not once per skill): the
	whole catalogue is read in a single query and folded into one alternation pattern.
	"""
	skills = frappe.get_all("Skill", fields=["name", "aliases"], limit_page_length=0)
	terms_to_skill: dict[str, str] = {}
	for row in skills:
		terms = [row.name] + [a.strip() for a in (row.aliases or "").split(",") if a.strip()]
		for term in terms:
			key = term.strip().lower()
			if not key:
				continue
			# First skill to claim a term wins; later collisions are silently ignored so one typo'd
			# alias can never make two different skills fire off the same word.
			terms_to_skill.setdefault(key, row.name)
	return _compile_matcher(terms_to_skill), terms_to_skill


def _split_sentences(text: str) -> list[str]:
	text = strip_html(text or "")
	text = re.sub(r"\s+", " ", text).strip()
	if not text:
		return []
	return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def find_skill_evidence(
	text: str, pattern: re.Pattern | None, terms_to_skill: dict[str, str]
) -> list[tuple[str, str]]:
	"""Return [(skill_name, sentence), ...] - the first sentence that mentions each skill, once each."""
	if not pattern or not text:
		return []
	hits: list[tuple[str, str]] = []
	seen: set[str] = set()
	for sentence in _split_sentences(text):
		for match in pattern.finditer(sentence):
			skill_name = terms_to_skill.get(match.group(1).lower())
			if not skill_name or skill_name in seen:
				continue
			seen.add(skill_name)
			hits.append((skill_name, sentence[:240]))
	return hits


def _recency_factor(evidence_date, today=None) -> float:
	if not evidence_date:
		return MIN_RECENCY_FACTOR
	today = getdate(today) if today else getdate(nowdate())
	age_days = max(0, (today - getdate(evidence_date)).days)
	return max(MIN_RECENCY_FACTOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))


def _confidence(source_doctype: str, evidence_date) -> int:
	weight = SOURCE_WEIGHT.get(source_doctype, 30)
	return max(1, min(100, round(weight * _recency_factor(evidence_date))))


# ---------------------------------------------------------------------------
# Evidence sources
#
# Each returns a bounded, ordered (oldest-modified-first) list of dicts:
#   {employee, text, date, source_name, modified}
# in exactly one query (a query-builder join for the two that live on a child table), so a batch of
# `limit` evidence rows never becomes `limit` separate queries.
# ---------------------------------------------------------------------------


def _feedback_evidence(since: str | None, employee: str | None, limit: int) -> list[dict]:
	"""hrms/hr/doctype/employee_performance_feedback/employee_performance_feedback.json:
	`employee` (Link), `feedback` (Text Editor), `added_on` (Datetime), submittable."""
	filters = {"docstatus": 1}
	if employee:
		filters["employee"] = employee
	if since:
		filters["modified"] = [">", since]
	rows = frappe.get_all(
		"Employee Performance Feedback",
		filters=filters,
		fields=["name", "employee", "feedback", "added_on", "modified"],
		order_by="modified asc",
		limit_page_length=limit,
	)
	out = []
	for row in rows:
		text = strip_html(row.feedback or "").strip()
		if not text:
			continue
		out.append(
			{
				"employee": row.employee,
				"text": text,
				"date": getdate(row.added_on or row.modified),
				"source_name": row.name,
				"modified": row.modified,
			}
		)
	return out


def _goal_evidence(since: str | None, employee: str | None, limit: int) -> list[dict]:
	"""hrms/hr/doctype/goal/goal.json: `employee` (Link), `goal_name` (Data), `description` (Text
	Editor), `end_date` (Date). Not submittable, so no docstatus filter."""
	filters = {}
	if employee:
		filters["employee"] = employee
	if since:
		filters["modified"] = [">", since]
	rows = frappe.get_all(
		"Goal",
		filters=filters,
		fields=["name", "employee", "goal_name", "description", "end_date", "modified"],
		order_by="modified asc",
		limit_page_length=limit,
	)
	out = []
	for row in rows:
		text = " ".join(p for p in (row.goal_name, strip_html(row.description or "")) if p).strip()
		if not text:
			continue
		out.append(
			{
				"employee": row.employee,
				"text": text,
				"date": getdate(row.end_date or row.modified),
				"source_name": row.name,
				"modified": row.modified,
			}
		)
	return out


def _training_evidence(since: str | None, employee: str | None, limit: int) -> list[dict]:
	"""hrms/hr/doctype/training_result/training_result.json (submittable) has child table
	`employees` -> Training Result Employee (`employee` Link, `comments` Text, `grade` Data) and
	`training_event` -> Training Event (`event_name`, `course`, `introduction`)."""
	TRE = frappe.qb.DocType("Training Result Employee")
	TR = frappe.qb.DocType("Training Result")
	TE = frappe.qb.DocType("Training Event")

	query = (
		frappe.qb.from_(TRE)
		.join(TR)
		.on(TRE.parent == TR.name)
		.left_join(TE)
		.on(TR.training_event == TE.name)
		.select(
			TRE.employee,
			TRE.comments,
			TR.name.as_("source_name"),
			TR.modified,
			TE.event_name,
			TE.course,
			TE.introduction,
			TE.end_time,
			TE.start_time,
		)
		.where(TR.docstatus == 1)
		.orderby(TR.modified)
		.limit(limit)
	)
	if employee:
		query = query.where(TRE.employee == employee)
	if since:
		query = query.where(TR.modified > since)

	out = []
	for row in query.run(as_dict=True):
		parts = [row.event_name, row.course, strip_html(row.introduction or ""), row.comments]
		text = " ".join(p for p in parts if p).strip()
		if not text or not row.employee:
			continue
		event_date = row.end_time or row.start_time or row.modified
		out.append(
			{
				"employee": row.employee,
				"text": text,
				"date": getdate(event_date),
				"source_name": row.source_name,
				"modified": row.modified,
			}
		)
	return out


def _timesheet_evidence(since: str | None, employee: str | None, limit: int) -> list[dict]:
	"""hrms/hr/doctype/../timesheet (erpnext/projects/doctype/timesheet, submittable, HRMS overrides
	it via EmployeeTimesheet): `employee` (Link). Child `time_logs` -> Timesheet Detail:
	`description` (Small Text), `to_time` (Datetime)."""
	TD = frappe.qb.DocType("Timesheet Detail")
	TS = frappe.qb.DocType("Timesheet")

	query = (
		frappe.qb.from_(TD)
		.join(TS)
		.on(TD.parent == TS.name)
		.select(TS.employee, TD.description, TS.name.as_("source_name"), TS.modified, TD.to_time)
		.where(TS.docstatus == 1)
		.orderby(TS.modified)
		.limit(limit)
	)
	if employee:
		query = query.where(TS.employee == employee)
	if since:
		query = query.where(TS.modified > since)

	out = []
	for row in query.run(as_dict=True):
		text = (row.description or "").strip()
		if not text or not row.employee:
			continue
		out.append(
			{
				"employee": row.employee,
				"text": text,
				"date": getdate(row.to_time or row.modified),
				"source_name": row.source_name,
				"modified": row.modified,
			}
		)
	return out


_EVIDENCE_SOURCES = (
	("Employee Performance Feedback", _feedback_evidence),
	("Goal", _goal_evidence),
	("Training Result", _training_evidence),
	("Timesheet", _timesheet_evidence),
)


# ---------------------------------------------------------------------------
# Suggestion generation
# ---------------------------------------------------------------------------


def _employee_current_and_reviewed_skills(employee: str) -> set[str]:
	"""Skills that should never be (re-)suggested: already on the employee's skill map, or already
	Suggested/Accepted/Dismissed as a Skill Suggestion (regardless of source)."""
	known = set()
	if frappe.db.exists("Employee Skill Map", employee):
		known.update(
			frappe.get_all(
				"Employee Skill",
				filters={"parent": employee, "parenttype": "Employee Skill Map"},
				pluck="skill",
			)
		)
	known.update(frappe.get_all("Skill Suggestion", filters={"employee": employee}, pluck="skill"))
	return known


def _create_suggestion(
	employee: str,
	skill: str,
	confidence: int,
	evidence: str,
	evidence_date,
	source_doctype: str,
	source_name: str,
) -> str | None:
	if frappe.db.exists(
		"Skill Suggestion",
		{"employee": employee, "skill": skill, "source_doctype": source_doctype, "source_name": source_name},
	):
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Skill Suggestion",
			"employee": employee,
			"skill": skill,
			"confidence": confidence,
			"evidence": evidence,
			"evidence_date": evidence_date,
			"source_doctype": source_doctype,
			"source_name": source_name,
			"status": "Suggested",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def refresh_suggestions(employee: str | None = None, batch_size: int | float | str | None = None) -> dict:
	"""The incremental engine. With no `employee`, scans every evidence source past its watermark
	(bounded to `batch_size` rows each) and advances the watermark. With `employee`, ignores the
	watermark and scans just that employee's rows (still bounded per source) - the on-demand path.

	Never called directly by the scheduler (see `scheduled_refresh_suggestions`): this function does
	not commit, so it is safe to call from a test.
	"""
	batch_size = min(MAX_BATCH_SIZE, cint(batch_size) or DEFAULT_BATCH_SIZE)
	pattern, terms_to_skill = get_skill_matcher()
	result = {"processed": 0, "created": 0}
	if not pattern:
		return result

	skip_cache: dict[str, set[str]] = {}
	pending_count: dict[str, int] = {}
	created_this_run: dict[str, set[str]] = defaultdict(set)

	for source_doctype, fetch in _EVIDENCE_SOURCES:
		since = None if employee else _get_watermark(source_doctype)
		rows = fetch(since, employee, batch_size)
		max_modified = since

		for row in rows:
			result["processed"] += 1
			modified = row.get("modified")
			if modified and (not max_modified or cstr(modified) > cstr(max_modified)):
				max_modified = modified

			emp = row["employee"]
			hits = find_skill_evidence(row["text"], pattern, terms_to_skill)
			if not hits:
				continue

			if emp not in skip_cache:
				skip_cache[emp] = _employee_current_and_reviewed_skills(emp)
				pending_count[emp] = frappe.db.count(
					"Skill Suggestion", {"employee": emp, "status": "Suggested"}
				)
			skip = skip_cache[emp]

			for skill_name, sentence in hits:
				if skill_name in skip or skill_name in created_this_run[emp]:
					continue
				# Bound how many un-reviewed suggestions one employee can accumulate - a full skill
				# catalogue and a lot of history should never turn into an unbounded pile to review.
				if pending_count[emp] + len(created_this_run[emp]) >= MAX_SUGGESTIONS_PER_EMPLOYEE:
					break
				confidence = _confidence(source_doctype, row["date"])
				name = _create_suggestion(
					emp, skill_name, confidence, sentence, row["date"], source_doctype, row["source_name"]
				)
				if name:
					result["created"] += 1
					created_this_run[emp].add(skill_name)

		if not employee and max_modified:
			_set_watermark(source_doctype, max_modified)

	return result


def scheduled_refresh_suggestions() -> dict:
	"""Scheduler entry point (see the report for the `hooks.py` line to add). Commits after each
	incremental pass, as other batched HRMS jobs do (see e.g.
	hrms/hr/doctype/shift_type/shift_type.py:237)."""
	result = refresh_suggestions()
	frappe.db.commit()  # nosemgrep
	return result


@frappe.whitelist()
def refresh_my_suggestions(employee: str | None = None) -> dict:
	employee = employee or _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))
	_require_view_access(employee)
	return refresh_suggestions(employee=employee)


# ---------------------------------------------------------------------------
# Reviewing suggestions
# ---------------------------------------------------------------------------


def _employee_skill_map_doc(employee: str):
	if frappe.db.exists("Employee Skill Map", employee):
		return frappe.get_doc("Employee Skill Map", employee)
	doc = frappe.new_doc("Employee Skill Map")
	doc.employee = employee
	return doc


def _mark_siblings(employee: str, skill: str, status: str, exclude: str) -> None:
	"""Once one suggestion for (employee, skill) is reviewed, retire the other pending suggestions
	for the same skill so the Employee form doesn't keep asking about something already decided."""
	others = frappe.get_all(
		"Skill Suggestion",
		filters={"employee": employee, "skill": skill, "status": "Suggested", "name": ["!=", exclude]},
		pluck="name",
	)
	for name in others:
		frappe.db.set_value(
			"Skill Suggestion",
			name,
			{"status": status, "reviewed_by": frappe.session.user, "reviewed_on": now_datetime()},
		)


@frappe.whitelist()
def accept_suggestion(name: str) -> dict:
	doc = frappe.get_doc("Skill Suggestion", name)
	_require_act_access(doc.employee)
	if doc.status != "Suggested":
		frappe.throw(_("This suggestion has already been reviewed"))

	skill_map = _employee_skill_map_doc(doc.employee)
	if not any(row.skill == doc.skill for row in skill_map.get("employee_skills", [])):
		skill_map.append(
			"employee_skills",
			{"skill": doc.skill, "proficiency": DEFAULT_ACCEPTED_PROFICIENCY, "evaluation_date": nowdate()},
		)
		skill_map.save(ignore_permissions=True)

	doc.status = "Accepted"
	doc.reviewed_by = frappe.session.user
	doc.reviewed_on = now_datetime()
	doc.save(ignore_permissions=True)
	_mark_siblings(doc.employee, doc.skill, "Accepted", doc.name)
	return {"ok": True, "skill": doc.skill}


@frappe.whitelist()
def dismiss_suggestion(name: str) -> dict:
	doc = frappe.get_doc("Skill Suggestion", name)
	_require_act_access(doc.employee)
	if doc.status != "Suggested":
		frappe.throw(_("This suggestion has already been reviewed"))

	doc.status = "Dismissed"
	doc.reviewed_by = frappe.session.user
	doc.reviewed_on = now_datetime()
	doc.save(ignore_permissions=True)
	_mark_siblings(doc.employee, doc.skill, "Dismissed", doc.name)
	return {"ok": True, "skill": doc.skill}


@frappe.whitelist()
def get_employee_skill_summary(employee: str | None = None) -> dict:
	"""Everything the Employee form's Skills section needs in one call: current skills, pending
	suggestions (best evidence per skill), and gaps against the employee's own designation."""
	employee = employee or _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))
	_require_view_access(employee)

	current_skills = frappe.get_all(
		"Employee Skill",
		filters={"parent": employee, "parenttype": "Employee Skill Map"},
		fields=["skill", "proficiency"],
		order_by="skill asc",
	)

	suggestions = frappe.get_all(
		"Skill Suggestion",
		filters={"employee": employee, "status": "Suggested"},
		fields=["name", "skill", "confidence", "evidence", "evidence_date", "source_doctype", "source_name"],
		order_by="confidence desc",
		limit_page_length=200,
	)
	best_per_skill: dict[str, dict] = {}
	for row in suggestions:
		if row.skill not in best_per_skill:
			best_per_skill[row.skill] = row

	return {
		"employee": employee,
		"skills": current_skills,
		"suggestions": list(best_per_skill.values()),
		"gaps": analyze_skill_gaps(employee),
	}


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


def _employee_skill_map(employee: str) -> dict[str, float]:
	rows = frappe.get_all(
		"Employee Skill",
		filters={"parent": employee, "parenttype": "Employee Skill Map"},
		fields=["skill", "proficiency"],
	)
	return {row.skill: flt(row.proficiency) for row in rows}


def analyze_skill_gaps(
	employee: str,
	designation: str | None = None,
	job_opening: str | None = None,
	opportunity: str | None = None,
) -> list[dict]:
	"""Missing skills, and proficiency shortfalls where a minimum is actually tracked.

	`Designation Skill` (hrms/hr/doctype/designation_skill/designation_skill.json) only stores which
	skills are wanted, not a minimum level, so a gap against a designation or job opening can only
	ever be "missing" - never a shortfall. `Talent Opportunity Skill` does carry a minimum
	proficiency, so gaps against an opportunity can be either.
	"""
	skill_map = _employee_skill_map(employee)
	rows: list[dict] = []

	if opportunity:
		rows = frappe.get_all(
			"Talent Opportunity Skill",
			filters={"parent": opportunity, "parenttype": "Talent Opportunity"},
			fields=["skill", "minimum_proficiency", "requirement"],
		)
	else:
		if job_opening and not designation:
			designation = frappe.db.get_value("Job Opening", job_opening, "designation")
		designation = designation or frappe.db.get_value("Employee", employee, "designation")
		if designation:
			rows = [
				{"skill": r.skill, "minimum_proficiency": 0, "requirement": "Required"}
				for r in frappe.get_all(
					"Designation Skill",
					filters={"parent": designation, "parenttype": "Designation"},
					fields=["skill"],
				)
			]

	gaps = []
	for row in rows:
		have = skill_map.get(row["skill"], 0)
		minimum = flt(row.get("minimum_proficiency") or 0)
		if have <= 0:
			gaps.append(
				{
					"skill": row["skill"],
					"gap_type": "missing",
					"employee_proficiency": have,
					"minimum_proficiency": minimum or None,
					"requirement": row.get("requirement") or "Required",
				}
			)
		elif minimum and have < minimum:
			gaps.append(
				{
					"skill": row["skill"],
					"gap_type": "shortfall",
					"employee_proficiency": have,
					"minimum_proficiency": minimum,
					"requirement": row.get("requirement") or "Required",
				}
			)
	return gaps


@frappe.whitelist()
def get_skill_gaps(
	employee: str | None = None, designation: str | None = None, job_opening: str | None = None
) -> list[dict]:
	employee = employee or _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))
	_require_view_access(employee)
	return analyze_skill_gaps(employee, designation=designation, job_opening=job_opening)


# ---------------------------------------------------------------------------
# Marketplace matching
# ---------------------------------------------------------------------------


def _credit(row: dict, skill_map: dict[str, float]) -> float:
	have = skill_map.get(row["skill"], 0)
	minimum = flt(row.get("minimum_proficiency") or 0)
	if minimum > 0:
		return min(1.0, have / minimum) if have else 0.0
	return 1.0 if have else 0.0


def compute_match(required_rows: list[dict], nice_rows: list[dict], skill_map: dict[str, float]) -> int:
	"""See the module docstring for the formula this implements."""
	required_score = (
		(sum(_credit(r, skill_map) for r in required_rows) / len(required_rows)) if required_rows else None
	)
	nice_score = (sum(_credit(r, skill_map) for r in nice_rows) / len(nice_rows)) if nice_rows else None

	if required_score is None and nice_score is None:
		return 0
	if required_score is None:
		fraction = nice_score
	elif nice_score is None:
		fraction = required_score
	else:
		fraction = 0.75 * required_score + 0.25 * nice_score
	return max(0, min(100, round(fraction * 100)))


def _match_reasons(rows: list[dict], skill_map: dict[str, float], limit: int = 6) -> list[str]:
	reasons = []
	for row in rows[:limit]:
		have = skill_map.get(row["skill"], 0)
		minimum = flt(row.get("minimum_proficiency") or 0)
		label = row.get("requirement") or "Required"
		if have <= 0:
			reasons.append(_("{0} ({1}): not yet on your skill map").format(row["skill"], label))
		elif minimum and have < minimum:
			reasons.append(_("{0} ({1}): below the wanted level").format(row["skill"], label))
		else:
			reasons.append(_("{0} ({1}): you have this").format(row["skill"], label))
	return reasons


def _split_requirement(rows: list[dict]) -> tuple[list[dict], list[dict]]:
	required = [r for r in rows if (r.get("requirement") or "Required") != "Nice to have"]
	nice = [r for r in rows if (r.get("requirement") or "Required") == "Nice to have"]
	return required, nice


def match_opportunities(employee: str, limit: int | float | str | None = None) -> list[dict]:
	skill_map = _employee_skill_map(employee)
	if not skill_map:
		return []

	opportunities = frappe.get_all(
		"Talent Opportunity",
		filters={"status": "Open"},
		fields=[
			"name",
			"title",
			"type",
			"owner_employee",
			"department",
			"company",
			"hours_per_week",
			"start_date",
			"end_date",
		],
		order_by="modified desc",
		limit_page_length=MAX_OPPORTUNITY_SCAN,
	)
	if not opportunities:
		return []

	names = [o.name for o in opportunities]
	skill_rows = frappe.get_all(
		"Talent Opportunity Skill",
		filters={"parent": ["in", names], "parenttype": "Talent Opportunity"},
		fields=["parent", "skill", "minimum_proficiency", "requirement"],
		limit_page_length=0,
	)
	by_parent = defaultdict(list)
	for row in skill_rows:
		by_parent[row.parent].append(row)

	results = []
	for opp in opportunities:
		rows = by_parent.get(opp.name, [])
		if not rows:
			continue
		required, nice = _split_requirement(rows)
		score = compute_match(required, nice, skill_map)
		if score <= 0:
			continue
		results.append(
			{
				**opp,
				"match_score": score,
				"why": _match_reasons(required + nice, skill_map),
			}
		)

	results.sort(key=lambda r: r["match_score"], reverse=True)
	limit = min(MAX_MATCH_LIMIT, cint(limit) or DEFAULT_MATCH_LIMIT)
	return results[:limit]


@frappe.whitelist()
def get_matching_opportunities(
	employee: str | None = None, limit: int | float | str | None = None
) -> list[dict]:
	employee = employee or _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))
	_require_view_access(employee)
	return match_opportunities(employee, limit=limit)


def match_people(opportunity: str, limit: int | float | str | None = None) -> list[dict]:
	rows = frappe.get_all(
		"Talent Opportunity Skill",
		filters={"parent": opportunity, "parenttype": "Talent Opportunity"},
		fields=["skill", "minimum_proficiency", "requirement"],
	)
	if not rows:
		return []
	skill_names = list({r.skill for r in rows})

	# Single query: only employees who already have at least one of the wanted skills are touched -
	# never a loop or a query per employee (see module docstring).
	emp_skill_rows = frappe.get_all(
		"Employee Skill",
		filters={"skill": ["in", skill_names], "parenttype": "Employee Skill Map"},
		fields=["parent as employee", "skill", "proficiency"],
		limit_page_length=0,
	)
	if not emp_skill_rows:
		return []

	by_employee = defaultdict(dict)
	for row in emp_skill_rows:
		by_employee[row.employee][row.skill] = flt(row.proficiency)

	candidate_names = list(by_employee)[:MAX_CANDIDATE_SCAN]
	employees = frappe.get_all(
		"Employee",
		filters={"name": ["in", candidate_names], "status": "Active"},
		fields=["name", "employee_name", "designation", "department"],
	)

	required, nice = _split_requirement(rows)
	results = []
	for emp in employees:
		skill_map = by_employee[emp.name]
		score = compute_match(required, nice, skill_map)
		if score <= 0:
			continue
		results.append(
			{
				"employee": emp.name,
				"employee_name": emp.employee_name,
				"designation": emp.designation,
				"department": emp.department,
				"match_score": score,
				"why": _match_reasons(required + nice, skill_map),
			}
		)

	results.sort(key=lambda r: r["match_score"], reverse=True)
	limit = min(MAX_MATCH_LIMIT, cint(limit) or DEFAULT_MATCH_LIMIT)
	return results[:limit]


@frappe.whitelist()
def get_matching_candidates(opportunity: str, limit: int | float | str | None = None) -> list[dict]:
	"""Algorithmic suggestions: people who look like a good fit, whether or not they've applied."""
	_require_opportunity_owner_or_hr(opportunity)
	return match_people(opportunity, limit=limit)


@frappe.whitelist()
def list_my_opportunities() -> list[dict]:
	"""The "For owners" tab's opportunity picker: opportunities the calling user owns. Self-scoped by
	the filter itself, so no extra permission check is needed (an HR user browsing everyone else's
	opportunities uses the ordinary Talent Opportunity list view, which they have full DocPerm on)."""
	employee = _get_employee_for_user()
	if not employee:
		return []
	return frappe.get_all(
		"Talent Opportunity",
		filters={"owner_employee": employee},
		fields=["name", "title", "status"],
		order_by="modified desc",
		limit_page_length=100,
	)


@frappe.whitelist()
def get_opportunity_interests(opportunity: str) -> list[dict]:
	"""Who actually clicked "Express interest" - the owner's/HR's "candidates" tab. Distinct from
	`get_matching_candidates`, which is the algorithm's own suggestions regardless of who applied."""
	_require_opportunity_owner_or_hr(opportunity)
	return frappe.get_all(
		"Talent Opportunity Interest",
		filters={"parent": opportunity, "parenttype": "Talent Opportunity"},
		fields=["name", "employee", "employee_name", "note", "match_score", "status", "expressed_on"],
		order_by="match_score desc",
	)


@frappe.whitelist()
def update_interest_status(opportunity: str, employee: str, status: str) -> dict:
	"""Owner/HR moves a candidate to Shortlisted or Declined."""
	_require_opportunity_owner_or_hr(opportunity)
	if status not in ("Interested", "Shortlisted", "Declined"):
		frappe.throw(_("Invalid status"))

	doc = frappe.get_doc("Talent Opportunity", opportunity)
	row = next((r for r in doc.interests if r.employee == employee), None)
	if not row:
		frappe.throw(_("{0} has not expressed interest in this opportunity").format(employee))
	row.status = status
	doc.save(ignore_permissions=True)
	return {"ok": True}


# ---------------------------------------------------------------------------
# Marketplace listing and interest
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_open_opportunities(
	opportunity_type: str | None = None,
	start: int | float | str | None = None,
	page_length: int | float | str | None = None,
) -> list[dict]:
	filters = {"status": "Open"}
	if opportunity_type:
		filters["type"] = opportunity_type
	opportunities = frappe.get_all(
		"Talent Opportunity",
		filters=filters,
		fields=[
			"name",
			"title",
			"type",
			"owner_employee",
			"department",
			"company",
			"hours_per_week",
			"start_date",
			"end_date",
		],
		order_by="modified desc",
		limit_start=cint(start) or 0,
		limit_page_length=min(MAX_OPPORTUNITY_SCAN, cint(page_length) or DEFAULT_MATCH_LIMIT),
	)
	if not opportunities:
		return []

	names = [o.name for o in opportunities]
	skill_rows = frappe.get_all(
		"Talent Opportunity Skill",
		filters={"parent": ["in", names], "parenttype": "Talent Opportunity"},
		fields=["parent", "skill", "minimum_proficiency", "requirement"],
		limit_page_length=0,
	)
	by_parent = defaultdict(list)
	for row in skill_rows:
		by_parent[row.parent].append(
			{
				"skill": row.skill,
				"minimum_proficiency": row.minimum_proficiency,
				"requirement": row.requirement,
			}
		)

	my_employee = _get_employee_for_user()
	skill_map = _employee_skill_map(my_employee) if my_employee else {}
	my_interests: set[str] = set()
	if my_employee:
		my_interests = set(
			frappe.get_all(
				"Talent Opportunity Interest",
				filters={
					"parent": ["in", names],
					"parenttype": "Talent Opportunity",
					"employee": my_employee,
				},
				pluck="parent",
			)
		)

	out = []
	for opp in opportunities:
		rows = by_parent.get(opp.name, [])
		required, nice = _split_requirement(rows)
		score = compute_match(required, nice, skill_map) if skill_map else 0
		out.append(
			{
				**opp,
				"skills": rows,
				"match_score": score,
				"why": _match_reasons(required + nice, skill_map) if skill_map else [],
				"already_interested": opp.name in my_interests,
			}
		)
	return out


@frappe.whitelist()
def express_interest(opportunity: str, note: str | None = None) -> dict:
	employee = _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))

	doc = frappe.get_doc("Talent Opportunity", opportunity)
	if doc.status != "Open":
		frappe.throw(_("This opportunity is no longer open"))

	required, nice = _split_requirement(
		[
			{"skill": r.skill, "minimum_proficiency": r.minimum_proficiency, "requirement": r.requirement}
			for r in doc.skills
		]
	)
	score = compute_match(required, nice, _employee_skill_map(employee))

	existing = next((row for row in doc.interests if row.employee == employee), None)
	if existing:
		existing.status = "Interested"
		existing.note = note
		existing.match_score = score
	else:
		doc.append(
			"interests",
			{
				"employee": employee,
				"note": note,
				"match_score": score,
				"status": "Interested",
				"expressed_on": nowdate(),
			},
		)
	doc.save(ignore_permissions=True)
	return {"ok": True, "match_score": score}


@frappe.whitelist()
def withdraw_interest(opportunity: str) -> dict:
	employee = _get_employee_for_user()
	if not employee:
		frappe.throw(_("No Employee record is linked to your user"))

	doc = frappe.get_doc("Talent Opportunity", opportunity)
	before = len(doc.interests)
	doc.interests = [row for row in doc.interests if row.employee != employee]
	if len(doc.interests) != before:
		doc.save(ignore_permissions=True)
	return {"ok": True}
