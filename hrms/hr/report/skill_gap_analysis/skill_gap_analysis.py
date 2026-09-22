# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Which employees are missing skills their own designation calls for.

Only reports "missing" (a Designation Skill the employee doesn't have at all), never a proficiency
"shortfall": `Designation Skill` (hrms/hr/doctype/designation_skill/designation_skill.json) has no
minimum-proficiency field to fall short of - see hrms.hr.skills_cloud.analyze_skill_gaps for the same
finding and the one place (Talent Opportunity Skill) that does support shortfalls.

Bulk, not per-employee: at most three queries regardless of how many employees are in scope
(employees, their designations' required skills, their own skill maps), then the comparison happens
in Python. An employee whose designation has no required skills, or who has none missing, is left out
- this report is meant to be a short, actionable list, not a roster.
"""

from collections import defaultdict

import frappe
from frappe import _

from hrms.hr.skills_cloud import _get_employee_for_user, _is_hr

MAX_EMPLOYEES = 500


def execute(filters: dict | None = None) -> tuple:
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	chart = get_chart_data(data)
	return columns, data, None, chart


def get_columns() -> list[dict]:
	return [
		{
			"fieldname": "employee",
			"fieldtype": "Link",
			"label": _("Employee"),
			"options": "Employee",
			"width": 120,
		},
		{"fieldname": "employee_name", "fieldtype": "Data", "label": _("Employee Name"), "width": 150},
		{
			"fieldname": "designation",
			"fieldtype": "Link",
			"label": _("Designation"),
			"options": "Designation",
			"width": 150,
		},
		{
			"fieldname": "department",
			"fieldtype": "Link",
			"label": _("Department"),
			"options": "Department",
			"width": 130,
		},
		{
			"fieldname": "company",
			"fieldtype": "Link",
			"label": _("Company"),
			"options": "Company",
			"width": 130,
		},
		{"fieldname": "required_skills", "fieldtype": "Int", "label": _("Required Skills"), "width": 110},
		{"fieldname": "missing_skills", "fieldtype": "Int", "label": _("Missing Skills"), "width": 110},
		{"fieldname": "missing_skill_list", "fieldtype": "Data", "label": _("Missing"), "width": 300},
	]


def _employee_scope(filters: frappe._dict) -> list[dict] | None:
	"""Employees the caller may see, honouring the filters. Returns None if there is nothing to scope
	(no linked Employee and not HR)."""
	user = frappe.session.user
	base_filters = {"status": "Active"}
	if filters.get("company"):
		base_filters["company"] = filters.company
	if filters.get("department"):
		base_filters["department"] = filters.department
	if filters.get("designation"):
		base_filters["designation"] = filters.designation
	if filters.get("employee"):
		base_filters["name"] = filters.employee

	extra_conditions = []
	if not _is_hr(user):
		my_employee = _get_employee_for_user(user)
		if not my_employee:
			return None
		bounds = frappe.db.get_value("Employee", my_employee, ["lft", "rgt"])
		if not bounds:
			base_filters["name"] = my_employee
		else:
			my_lft, my_rgt = bounds
			extra_conditions = [["lft", ">=", my_lft], ["rgt", "<=", my_rgt]]
		# A non-HR caller may only narrow further into their own scope, never point `employee` at
		# someone outside it - the range condition above still applies even if they tried.

	condition_filters = [[key, "=", value] for key, value in base_filters.items()]
	condition_filters.extend(extra_conditions)

	return frappe.get_all(
		"Employee",
		filters=condition_filters,
		fields=["name", "employee_name", "designation", "department", "company"],
		order_by="employee_name asc",
		limit_page_length=MAX_EMPLOYEES,
	)


def get_data(filters: frappe._dict) -> list[dict]:
	employees = _employee_scope(filters)
	if not employees:
		return []

	designations = list({e.designation for e in employees if e.designation})
	required_by_designation = defaultdict(list)
	if designations:
		for row in frappe.get_all(
			"Designation Skill",
			filters={"parent": ["in", designations], "parenttype": "Designation"},
			fields=["parent", "skill"],
		):
			required_by_designation[row.parent].append(row.skill)

	emp_names = [e.name for e in employees]
	have_by_employee = defaultdict(set)
	if emp_names:
		for row in frappe.get_all(
			"Employee Skill",
			filters={"parent": ["in", emp_names], "parenttype": "Employee Skill Map"},
			fields=["parent", "skill"],
		):
			have_by_employee[row.parent].add(row.skill)

	data = []
	for emp in employees:
		required = required_by_designation.get(emp.designation, [])
		if not required:
			continue
		have = have_by_employee.get(emp.name, set())
		missing = [s for s in required if s not in have]
		if not missing:
			continue
		data.append(
			{
				"employee": emp.name,
				"employee_name": emp.employee_name,
				"designation": emp.designation,
				"department": emp.department,
				"company": emp.company,
				"required_skills": len(required),
				"missing_skills": len(missing),
				"missing_skill_list": ", ".join(missing),
			}
		)
	data.sort(key=lambda r: r["missing_skills"], reverse=True)
	return data


def get_chart_data(data: list[dict]) -> dict | None:
	if not data:
		return None
	top = data[:10]
	return {
		"data": {
			"labels": [row["employee_name"] for row in top],
			"datasets": [{"name": _("Missing Skills"), "values": [row["missing_skills"] for row in top]}],
		},
		"type": "bar",
		"barOptions": {"spaceRatio": 0.7},
		"height": 250,
	}
