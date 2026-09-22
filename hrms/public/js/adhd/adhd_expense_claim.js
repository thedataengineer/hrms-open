// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// ADHD-059: "Receipt Attached?" ambient check on the Expense Claim form. Prevents the rejection-by-missing-
// receipt surprise by flagging rows that (as far as this can honestly tell) have no receipt.
//
// What "this row has a receipt" can mean, verified against hrms/hr/doctype/expense_claim/expense_claim.json
// and expense_claim_detail.json:
//   - Neither doctype ships an Attach/Attach Image field, so there is no first-party per-row signal. If a site
//     customises "Expense Claim Detail" with one (Customize Form can add any field to a child table), this
//     module uses it — the value is right there on each row, in memory, so no server round trip is needed and
//     the summary is exact ("X of Y" is literally counted from that field).
//   - Without such a field, a File in Frappe attaches to the whole Expense Claim (attached_to_doctype /
//     attached_to_name — see frappe/core/doctype/file/file.py), never to one row of its child table.
//     `attached_to_field` only ever holds a fieldname (set from the upload request's `fieldname`, see
//     frappe/handler.py upload_file()), never a child row name, so the ticket's per-row idea via that column
//     does not hold up and is not used. In this fallback there genuinely is no way to say which row a given
//     attachment belongs to.
//     - Zero attachments on the claim is still certain for every row, so every row can honestly be badged.
//     - One or more attachments make the per-row question unanswerable, so no row is badged "missing" (that
//       would risk a false claim) and the summary says a count, not a per-row fact.
//
// Refreshes after an attachment is added or removed: ERPNext reloads the document then
// (frappe/public/js/frappe/form/controls/attach.js on_upload_complete -> frm.save()/reload, and
// frappe/public/js/frappe/form/sidebar/attachments.js remove_attachment -> sidebar.reload_docinfo()), and
// both paths end in the form's own `refresh`, which this module already re-runs on.
//
// Display only: nothing here blocks a save or a submit, and everything is removed the moment Focus Mode goes off.

frappe.provide("erpnext.adhd");

(() => {
	const SUMMARY_ID = "adhd-receipt-summary";
	const BADGE_CLASS = "adhd-no-receipt";
	const FILE_METHOD = "frappe.client.get_list";
	const MAX_FILES = 100;
	const ATTACH_FIELDTYPES = ["Attach", "Attach Image"];

	function isActive() {
		return Boolean(frappe.boot && frappe.boot.adhd_mode);
	}

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function removeAll(frm) {
		const $layout = frm && frm.layout && frm.layout.wrapper;
		if (!$layout) return;
		$layout.find(`#${SUMMARY_ID}`).remove();
		$layout.find(".adhd-no-receipt-cell").remove();
	}

	// Mirrors the fallback other ADHD grid modules use (e.g. adhd_stock_entry.js findGridRow): the documented
	// API first, a plain DOM lookup by the row's own data-name if that comes back empty.
	function getGridRow(frm, rowName) {
		const gridRow = frm.fields_dict.expenses?.grid?.grid_rows_by_docname?.[rowName];
		if (gridRow?.row?.length) return gridRow.row;
		return $(frm.fields_dict.expenses?.grid?.wrapper)
			.find("[data-name]")
			.filter((_, element) => element.dataset.name === rowName)
			.first();
	}

	// The real per-row signal, when a site has customised the child table with one. Doctype metadata for a
	// form's child tables is always loaded with the form, so this needs no server call.
	function findReceiptFieldname(frm) {
		const childDoctype = frm.fields_dict.expenses && frm.fields_dict.expenses.df.options;
		if (!childDoctype || typeof frappe.get_meta !== "function") return null;
		const meta = frappe.get_meta(childDoctype);
		const field = (meta?.fields || []).find((df) => ATTACH_FIELDTYPES.includes(df.fieldtype));
		return field ? field.fieldname : null;
	}

	function badgeRow(frm, row) {
		const $row = getGridRow(frm, row.name);
		if (!$row || !$row.length || $row.find(`.${BADGE_CLASS}`).length) return;
		$row.append(
			`<div class="col grid-static-col adhd-no-receipt-cell">
				<span class="${BADGE_CLASS}">📎 ${esc(__("No receipt"))}</span>
			</div>`,
		);
	}

	// {rows, total} in field mode: rows come straight from the doc, no fetch.
	function evaluateWithField(frm, fieldname) {
		const rows = frm.doc.expenses || [];
		const withReceipt = rows.filter((row) => Boolean(row[fieldname])).length;
		return {
			mode: "field",
			total: rows.length,
			withReceipt,
			missing: rows.filter((row) => !row[fieldname]),
		};
	}

	async function fetchAttachmentCount(frm) {
		if (frm.is_new()) return 0; // an unsaved document has no name a File could attach to
		try {
			const response = await frappe.call({
				method: FILE_METHOD,
				args: {
					doctype: "File",
					filters: {
						attached_to_doctype: "Expense Claim",
						attached_to_name: frm.doc.name,
					},
					fields: ["name"],
					limit_page_length: MAX_FILES,
				},
				// a permission hiccup must not pop a dialog over the form; the summary just says "not available"
				silent: true,
			});
			return (response && response.message && response.message.length) || 0;
		} catch (error) {
			return null; // unknown: never claim a row's receipt is missing when this happens
		}
	}

	function buildSummary(text, level, note) {
		return $(`<div id="${SUMMARY_ID}" class="adhd-receipt-summary adhd-receipt-${level}">
			${esc(text)}
			${note ? `<span class="adhd-receipt-summary-note">${esc(note)}</span>` : ""}
		</div>`);
	}

	function render(frm, evaluation) {
		removeAll(frm);
		if (!isActive() || !frm.doc.expenses || !frm.doc.expenses.length) return;

		const total = frm.doc.expenses.length;
		let $summary;

		if (evaluation.mode === "field") {
			const { withReceipt, missing } = evaluation;
			missing.forEach((row) => badgeRow(frm, row));
			const level = withReceipt === 0 ? "red" : withReceipt === total ? "green" : "amber";
			$summary = buildSummary(
				__("{0} of {1} expenses have a receipt attached.", [withReceipt, total]),
				level,
			);
		} else if (evaluation.mode === "unavailable") {
			$summary = buildSummary(__("Receipt status is not available right now."), "amber");
		} else {
			// document-level count: only "zero attachments" lets every row be badged honestly
			const count = evaluation.count;
			if (count === 0) {
				frm.doc.expenses.forEach((row) => badgeRow(frm, row));
				$summary = buildSummary(
					__("0 of {0} expenses have a receipt attached.", [total]),
					"red",
				);
			} else {
				$summary = buildSummary(
					__("{0} attachment(s) on this claim for {1} expense(s).", [count, total]),
					"amber",
					__(
						"Frappe attaches files to the whole claim, not to each row, so which expense has one can't be shown.",
					),
				);
			}
		}

		const field = frm.fields_dict.expenses;
		if (field && field.$wrapper && field.$wrapper.length) field.$wrapper.after($summary);
	}

	async function checkReceiptAttachments(frm, { forceFetch = true } = {}) {
		if (!frm || !frm.doc) return;
		if (!isActive()) {
			removeAll(frm);
			return;
		}
		if (!frm.doc.expenses || !frm.doc.expenses.length) {
			removeAll(frm);
			return;
		}

		const receiptField = findReceiptFieldname(frm);
		if (receiptField) {
			render(frm, evaluateWithField(frm, receiptField));
			return;
		}

		const sameDoc = frm._adhdExpenseCacheDocname === frm.docname;
		if (!forceFetch && sameDoc && frm._adhdExpenseCachedCount !== undefined) {
			const count = frm._adhdExpenseCachedCount;
			render(frm, count == null ? { mode: "unavailable" } : { mode: "count", count });
			return;
		}

		const token = (frm._adhdExpenseToken || 0) + 1;
		frm._adhdExpenseToken = token;
		const count = await fetchAttachmentCount(frm);
		if (token !== frm._adhdExpenseToken || !isActive()) return;

		frm._adhdExpenseCacheDocname = frm.docname;
		frm._adhdExpenseCachedCount = count;
		render(frm, count == null ? { mode: "unavailable" } : { mode: "count", count });
	}

	frappe.ui.form.on("Expense Claim", {
		refresh(frm) {
			checkReceiptAttachments(frm, { forceFetch: true });
		},
	});

	// The grid fires these on the CHILD doctype, named after the table fieldname ("expenses"); verified in
	// frappe/public/js/frappe/form/grid.js (add_new_row) and grid_row.js (remove()). Reuse the last known
	// attachment count instead of fetching again: adding or removing a row does not change what is attached
	// to the parent document.
	frappe.ui.form.on("Expense Claim Detail", {
		expenses_add(frm) {
			checkReceiptAttachments(frm, { forceFetch: false });
		},
		expenses_remove(frm) {
			checkReceiptAttachments(frm, { forceFetch: false });
		},
	});

	// Switching Focus Mode while the form is open takes effect at once, without reloading the form.
	erpnext.adhd.onStateChange?.((active) => {
		const frm = window.cur_frm;
		if (!frm || frm.doctype !== "Expense Claim") return;
		if (active) {
			checkReceiptAttachments(frm, { forceFetch: true });
		} else {
			removeAll(frm);
		}
	});

	erpnext.adhd.EXPENSE_RECEIPT_FILE_METHOD = FILE_METHOD;
	erpnext.adhd.checkExpenseReceiptAttachments = checkReceiptAttachments;
})();
