# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class JourneyTask(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Literal["Task", "Open a Document", "Ask a Question"]
		completed_by: DF.Link | None
		completed_on: DF.Datetime | None
		configured_user: DF.Link | None
		depends_on: DF.Data | None
		description: DF.SmallText | None
		due_date: DF.Date | None
		last_reminder_sent_on: DF.Date | None
		note: DF.SmallText | None
		owner_role: DF.Link | None
		owner_type: DF.Literal["Employee", "Employee's Manager", "HR", "Role", "User"]
		owner_unresolved: DF.Check
		owner_user: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reference_doctype: DF.Link | None
		required: DF.Check
		status: DF.Literal["Blocked", "Open", "Done", "Skipped"]
		title: DF.Data
		todo: DF.Link | None
	# end: auto-generated types
