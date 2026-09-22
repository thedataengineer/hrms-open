# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class JourneyTemplate(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from hrms.hr.doctype.journey_template_step.journey_template_step import JourneyTemplateStep

		company: DF.Link
		department: DF.Link | None
		designation: DF.Link | None
		disabled: DF.Check
		journey_type: DF.Literal[
			"Onboarding", "Offboarding", "Promotion", "Transfer", "Return from Leave", "Custom"
		]
		send_reminders: DF.Check
		steps: DF.Table[JourneyTemplateStep]
		title: DF.Data
		trigger: DF.Literal[
			"Manual", "Employee Joined", "Employee Separation", "Employee Promotion", "Employee Transfer"
		]
	# end: auto-generated types

	def validate(self):
		self.validate_unique_step_titles()
		self.validate_dependencies()

	def validate_unique_step_titles(self):
		seen = set()
		for step in self.steps:
			key = (step.title or "").strip().lower()
			if not key:
				frappe.throw(_("Row {0}: every step needs a title.").format(step.idx))
			if key in seen:
				frappe.throw(
					_("Row {0}: step title {1} is used more than once in this template.").format(
						step.idx, frappe.bold(step.title)
					)
				)
			seen.add(key)

	def validate_dependencies(self):
		titles_by_key = {(step.title or "").strip().lower(): step.title for step in self.steps}

		for step in self.steps:
			if not step.depends_on:
				continue

			dep_key = step.depends_on.strip().lower()
			if dep_key == (step.title or "").strip().lower():
				frappe.throw(_("Row {0}: a step cannot depend on itself.").format(step.idx))

			if dep_key not in titles_by_key:
				frappe.throw(
					_("Row {0}: {1} depends on {2}, which is not a step in this template.").format(
						step.idx, frappe.bold(step.title), frappe.bold(step.depends_on)
					)
				)

		self.check_for_cycles(titles_by_key)

	def check_for_cycles(self, titles_by_key):
		depends_on_by_key = {
			(step.title or "").strip().lower(): (step.depends_on or "").strip().lower() for step in self.steps
		}

		for start_key in depends_on_by_key:
			seen = set()
			current = start_key
			while current:
				if current in seen:
					frappe.throw(
						_("The step dependencies form a loop starting at {0}.").format(
							frappe.bold(titles_by_key.get(start_key, start_key))
						)
					)
				seen.add(current)
				current = depends_on_by_key.get(current) or None
