# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class SkillSuggestion(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		confidence: DF.Int
		employee: DF.Link
		employee_name: DF.Data | None
		evidence: DF.SmallText
		evidence_date: DF.Date | None
		reviewed_by: DF.Link | None
		reviewed_on: DF.Datetime | None
		skill: DF.Link
		source_doctype: DF.Link
		source_name: DF.DynamicLink
		status: DF.Literal["Suggested", "Accepted", "Dismissed"]
	# end: auto-generated types

	def validate(self):
		self.confidence = max(1, min(100, int(self.confidence or 0)))

		if self.is_new():
			duplicate = frappe.db.exists(
				"Skill Suggestion",
				{
					"employee": self.employee,
					"skill": self.skill,
					"source_doctype": self.source_doctype,
					"source_name": self.source_name,
				},
			)
			if duplicate:
				frappe.throw(
					_("A suggestion for {0} from this same source already exists ({1}).").format(
						self.skill, duplicate
					)
				)

		if self.has_value_changed("status") and self.status in ("Accepted", "Dismissed"):
			self.reviewed_by = self.reviewed_by or frappe.session.user
			self.reviewed_on = self.reviewed_on or now_datetime()
