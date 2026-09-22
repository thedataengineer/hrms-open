# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class TalentOpportunity(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from hrms.hr.doctype.talent_opportunity_interest.talent_opportunity_interest import (
			TalentOpportunityInterest,
		)
		from hrms.hr.doctype.talent_opportunity_skill.talent_opportunity_skill import TalentOpportunitySkill

		company: DF.Link | None
		department: DF.Link | None
		description: DF.TextEditor | None
		end_date: DF.Date | None
		hours_per_week: DF.Float
		interests: DF.Table[TalentOpportunityInterest]
		job_opening: DF.Link | None
		owner_employee: DF.Link
		skills: DF.Table[TalentOpportunitySkill]
		start_date: DF.Date | None
		status: DF.Literal["Open", "Filled", "Closed"]
		title: DF.Data
		type: DF.Literal["Open Role", "Project", "Mentoring", "Stretch Assignment"]
	# end: auto-generated types

	def validate(self):
		self.validate_dates()
		self.validate_unique_interests()

	def validate_dates(self):
		if self.start_date and self.end_date and getdate(self.end_date) < getdate(self.start_date):
			frappe.throw(_("End Date cannot be before Start Date"))

	def validate_unique_interests(self):
		seen = set()
		for row in self.interests:
			if row.employee in seen:
				frappe.throw(_("{0} has expressed interest more than once").format(row.employee))
			seen.add(row.employee)
