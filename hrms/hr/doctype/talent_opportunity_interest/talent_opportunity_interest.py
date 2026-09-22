# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class TalentOpportunityInterest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		employee: DF.Link
		employee_name: DF.Data | None
		expressed_on: DF.Date | None
		match_score: DF.Int
		note: DF.SmallText | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		status: DF.Literal["Interested", "Shortlisted", "Declined"]
	# end: auto-generated types

	pass
