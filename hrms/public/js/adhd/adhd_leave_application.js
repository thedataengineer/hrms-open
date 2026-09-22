// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// ADHD-058: "Balance at a Glance" card on the Leave Application form. An ADHD user filling this form has no
// idea how many days they have left until the server rejects the save — this card says so up front, using the
// exact numbers and the exact insufficient-balance check the doctype itself uses
// (hrms.hr.services.adhd_leave_balance.get_leave_glance wraps get_leave_details / get_leave_balance_on /
// get_number_of_leave_days from hrms/hr/doctype/leave_application/leave_application.py, so nothing here is
// re-derived). Display only: nothing here blocks a save or a submit, and everything is removed the moment
// Focus Mode goes off.

frappe.provide("erpnext.adhd");

(() => {
	const CARD_ID = "adhd-leave-balance";
	const METHOD = "hrms.hr.services.adhd_leave_balance.get_leave_glance";
	const DEBOUNCE_MS = 400;

	// "Leave Balance and Receipt Checks" in Focus Settings, registered by adhd_focus_registrations.js and on by
	// default. An RTB whose settings do not know the switch keeps the card on.
	function aidsOn() {
		const settings = erpnext.adhd.ADHDSettings;
		const known = (erpnext.adhd.ADHD_FEATURES || []).some(
			(feature) => feature.key === "hr_form_aids",
		);
		return !settings || !known || Boolean(settings.get("hr_form_aids"));
	}

	function isActive() {
		return Boolean(frappe.boot && frappe.boot.adhd_mode) && aidsOn();
	}

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function fmt(days) {
		// Half days are the only fraction HRMS produces here; anything else still prints sensibly.
		const n = flt(days);
		return format_number(n, null, n % 1 === 0 ? 0 : 1);
	}

	function removeCard(frm) {
		// The card is always inserted somewhere under the form's own layout (see insertCard), so removing
		// it from there also covers the common case of it sitting right after the leave_type field.
		const $wrapper = frm && frm.layout && frm.layout.wrapper;
		if ($wrapper) $wrapper.find(`#${CARD_ID}`).remove();
	}

	// Row order matches the ticket: entitlement, used, remaining, this application.
	function buildCard(frm, data) {
		const leaveType = esc(frm.doc.leave_type);
		let level = "green";
		let lines = [];
		let warning = "";

		if (!data || !data.available) {
			level = "amber";
			lines.push(`<span>${esc(__("Balance is not available for this employee."))}</span>`);
		} else if (data.state === "recorded") {
			level = "green";
			lines.push(`<span>${esc(__("Entitlement"))}: ${fmt(data.total_leaves)}</span>`);
			lines.push(`<span>${esc(__("Used"))}: ${fmt(data.used)}</span>`);
			lines.push(
				`<span>${esc(__("This application"))}: ${fmt(data.days)} ${esc(
					__("days"),
				)}</span>`,
			);
			lines.push(
				`<span>${esc(
					data.counted
						? __("Already counted against the balance.")
						: __("Cancelled — not counted against the balance."),
				)}</span>`,
			);
		} else if (data.state === "lwp") {
			level = "green";
			lines.push(
				`<span>${esc(
					__("{0} is Leave Without Pay — no balance is tracked.", [leaveType]),
				)}</span>`,
			);
			if (data.days != null) {
				lines.push(
					`<span>${esc(__("This application"))}: ${fmt(data.days)} ${esc(
						__("days"),
					)}</span>`,
				);
			}
		} else if (data.state === "no_allocation") {
			level = "red";
			lines.push(
				`<span>${esc(
					__("No leave allocation covers this date for {0}.", [leaveType]),
				)}</span>`,
			);
			warning = esc(__("Insufficient balance"));
		} else if (data.state === "no_days") {
			level = "amber";
			lines.push(`<span>${esc(__("The selected days fall on holidays."))}</span>`);
		} else {
			lines.push(`<span>${esc(__("Entitlement"))}: ${fmt(data.total_leaves)}</span>`);
			lines.push(`<span>${esc(__("Used"))}: ${fmt(data.used)}</span>`);
			lines.push(`<span>${esc(__("Remaining"))}: ${fmt(data.remaining)}</span>`);
			if (data.days != null) {
				lines.push(
					`<span>${esc(__("This application"))}: ${fmt(data.days)} ${esc(
						__("days"),
					)}</span>`,
				);
			}
			if (data.state === "ok") {
				level = "green";
			} else if (data.state === "low") {
				level = "amber";
			} else if (data.state === "short") {
				level = "red";
				warning = esc(__("Insufficient balance"));
			} else if (data.state === "negative_allowed") {
				level = "amber";
				lines.push(
					`<span>${esc(
						__("{0} allows a negative balance, so this can still be applied for.", [
							leaveType,
						]),
					)}</span>`,
				);
			}
		}

		const warningHtml = warning ? `<span class="adhd-leave-warn">⚠ ${warning}</span>` : "";
		return $(`<div id="${CARD_ID}" class="adhd-leave-card adhd-leave-${level}">
			<strong>${leaveType}</strong>
			${lines.join("\n")}
			${warningHtml}
		</div>`);
	}

	function insertCard(frm, $card) {
		const field = frm.fields_dict && frm.fields_dict.leave_type;
		if (field && field.$wrapper && field.$wrapper.length) {
			field.$wrapper.after($card);
			return;
		}
		// leave_type is always on the form; this is only a fallback if the layout is not ready yet.
		const $layout = frm.layout && frm.layout.wrapper;
		if ($layout) $layout.append($card);
	}

	async function renderBalanceCard(frm, token) {
		if (token !== frm._adhdLeaveToken || !isActive()) return;
		removeCard(frm);
		if (!isActive()) return;

		const { employee, leave_type, from_date } = frm.doc;
		if (!employee || !leave_type || !from_date) return;

		let data;
		try {
			const response = await frappe.call({
				method: METHOD,
				args: {
					employee,
					leave_type,
					from_date,
					to_date: frm.doc.to_date || null,
					half_day: frm.doc.half_day,
					half_day_date: frm.doc.half_day_date || null,
					leave_application: frm.is_new() ? null : frm.doc.name,
				},
				// a permission or server problem must not pop a dialog over the form; the card just says so
				silent: true,
			});
			data = response && response.message;
		} catch (error) {
			data = null;
		}

		// the mode, the form or the fields may have moved on while this request was in flight
		if (
			token !== frm._adhdLeaveToken ||
			!isActive() ||
			frm.doc.employee !== employee ||
			frm.doc.leave_type !== leave_type ||
			frm.doc.from_date !== from_date
		) {
			return;
		}
		removeCard(frm);
		insertCard(frm, buildCard(frm, data));
	}

	function scheduleRender(frm) {
		removeCard(frm);
		if (!isActive()) return;
		if (!frm.doc.employee || !frm.doc.leave_type || !frm.doc.from_date) return;

		const token = (frm._adhdLeaveToken || 0) + 1;
		frm._adhdLeaveToken = token;

		if (!frm._adhdLeaveDebounced) {
			// one debounced function per form instance, so rapid field edits collapse into one request
			frm._adhdLeaveDebounced = frappe.utils.debounce(
				(t) => renderBalanceCard(frm, t),
				DEBOUNCE_MS,
			);
		}
		frm._adhdLeaveDebounced(token);
	}

	function fetchAndRenderBalance(frm) {
		if (!frm || !frm.doc) return;
		if (!isActive()) {
			removeCard(frm);
			return;
		}
		scheduleRender(frm);
	}

	frappe.ui.form.on("Leave Application", {
		refresh: fetchAndRenderBalance,
		employee: fetchAndRenderBalance,
		leave_type: fetchAndRenderBalance,
		from_date: fetchAndRenderBalance,
		to_date: fetchAndRenderBalance,
		half_day: fetchAndRenderBalance,
		half_day_date: fetchAndRenderBalance,
	});

	// Switching Focus Mode while the form is open takes effect at once, without reloading the form.
	erpnext.adhd.onStateChange?.((active) => {
		const frm = window.cur_frm;
		if (!frm || frm.doctype !== "Leave Application") return;
		if (active) {
			fetchAndRenderBalance(frm);
		} else {
			frm._adhdLeaveDebounced &&
				frm._adhdLeaveDebounced.cancel &&
				frm._adhdLeaveDebounced.cancel();
			removeCard(frm);
		}
	});

	// Flipping the switch in Focus Settings changes the form on screen at once. `detail` is { key, value } for
	// one switch and undefined for a reset, which can change all of them.
	if (typeof $ === "function" && typeof document !== "undefined") {
		$(document).on("adhd_setting_changed adhd_settings_reset", (_event, detail) => {
			if (detail && detail.key && detail.key !== "hr_form_aids") return;
			const frm = window.cur_frm;
			if (frm && frm.doctype === "Leave Application") fetchAndRenderBalance(frm);
		});
	}

	erpnext.adhd.LEAVE_BALANCE_METHOD = METHOD;
	erpnext.adhd.renderLeaveBalanceCard = fetchAndRenderBalance;
	erpnext.adhd.buildLeaveBalanceCardHtml = buildCard;
})();
