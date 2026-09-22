# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class TalentOpportunitySkill(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		minimum_proficiency: DF.Rating
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		requirement: DF.Literal["Required", "Nice to have"]
		skill: DF.Link
	# end: auto-generated types

	pass
