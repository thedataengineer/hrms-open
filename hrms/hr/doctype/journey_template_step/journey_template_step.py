# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class JourneyTemplateStep(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Literal["Task", "Open a Document", "Ask a Question"]
		depends_on: DF.Data | None
		description: DF.SmallText | None
		due_offset_days: DF.Int
		owner_role: DF.Link | None
		owner_type: DF.Literal["Employee", "Employee's Manager", "HR", "Role", "User"]
		owner_user: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reference_doctype: DF.Link | None
		required: DF.Check
		title: DF.Data
	# end: auto-generated types
