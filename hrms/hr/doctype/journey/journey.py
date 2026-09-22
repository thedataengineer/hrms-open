# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class Journey(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from hrms.hr.doctype.journey_task.journey_task import JourneyTask

		company: DF.Link | None
		employee: DF.Link
		journey_template: DF.Link
		journey_type: DF.Literal[
			"Onboarding", "Offboarding", "Promotion", "Transfer", "Return from Leave", "Custom", ""
		]
		progress_percent: DF.Percent
		start_date: DF.Date
		status: DF.Literal["Not Started", "In Progress", "Completed", "Cancelled"]
		tasks: DF.Table[JourneyTask]
		triggered_by: DF.DynamicLink | None
		triggered_by_doctype: DF.Link | None
	# end: auto-generated types

	# All state changes (starting, opening the next tasks, completing, skipping, cancelling) go through
	# hrms.hr.journeys so that permission checks, dependency resolution and progress recomputation always
	# happen together. This controller stays thin on purpose: it does not duplicate that logic.
