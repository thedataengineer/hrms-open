# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Whitelisted endpoints for the Focus "Onboard New Employee" wizard (ADHD-014).

Every argument is type-annotated (`hooks.py` sets `require_type_annotated_api_methods`, and an un-annotated
whitelisted function answers every real browser request with HTTP 417). A browser always sends the wizard's
answers as a JSON string, never as a parsed object, so `payload` is typed `str`.

All the actual work (validation, description, atomic creation) lives in
`hrms.hr.services.adhd_employee_onboarding`, which this module only exposes and permission-gates.
"""

import frappe

from hrms.hr.services import adhd_employee_onboarding as service


@frappe.whitelist()
def get_context() -> dict:
	"""Read-only: what the wizard needs before it draws Step 1."""
	frappe.has_permission("Employee", "create", throw=True)
	return service.get_context()


@frappe.whitelist()
def check_step(payload: str, step: str | None = None) -> dict:
	"""Validate the answers so far (or just one step) without writing anything."""
	frappe.has_permission("Employee", "create", throw=True)
	return service.check(payload, step=step)


@frappe.whitelist()
def create_employee_onboarding(payload: str) -> dict:
	"""Create every document the confirmed answers describe, atomically: all of it or none of it."""
	frappe.has_permission("Employee", "create", throw=True)
	return service.create(payload)
