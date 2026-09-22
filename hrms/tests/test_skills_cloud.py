# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Plain unittest for hrms/hr/skills_cloud.py: the matcher, the scoring formulas, gap analysis,
permissions, and the incremental suggestion engine.

Deliberately does NOT import hrms.tests.utils / erpnext.tests.utils or use HRMSTestSuite /
ERPNextTestSuite (those bootstrap and commit fixtures into the site) - and, less obviously,
deliberately does NOT import erpnext.setup.doctype.employee.test_employee either: that module's
own top-level `from erpnext.tests.utils import ERPNextTestSuite` runs the same bootstrap as a side
effect of just importing it (verified by trying it: it commits fixture Users at import time and can
fail outright under this site's password policy). Employees are built by hand below instead, with
no password set (frappe.core.doctype.user.user.py only runs its password-strength check when a
password is actually given).

Every fixture is built here, inside the test's own transaction, and rolled back in tearDown.
frappe.db.commit is patched to raise for the whole suite except the one test that specifically
checks the scheduler wrapper commits.

Run with the copy of the shared runner, e.g.:
    ../env/bin/python <scratch_copy_of_run_tests.py> hrms.tests.test_skills_cloud
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from hrms.hr import skills_cloud as sc

WATERMARK_SOURCES = ["Employee Performance Feedback", "Goal", "Training Result", "Timesheet"]


class SkillsCloudTestCase(unittest.TestCase):
	"""Shared fixtures for the whole suite: an isolated company/department/designation and three
	employees (a manager, their report, and an unrelated stranger), plus a handful of skills with
	names chosen so a naive substring matcher would get them wrong."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		self._clear_watermarks()

		self._commit_patch = patch.object(
			frappe.db, "commit", side_effect=AssertionError("a Skills Cloud test tried to commit")
		)
		self._commit_patch.start()

		self.suffix = frappe.generate_hash(length=6)
		self.company = frappe.db.get_single_value("Global Defaults", "default_company")
		self.department = (
			frappe.get_doc(
				{
					"doctype": "Department",
					"department_name": f"ZZ Skills Dept {self.suffix}",
					"company": self.company,
				}
			)
			.insert()
			.name
		)
		self.designation = (
			frappe.get_doc({"doctype": "Designation", "designation_name": f"ZZ Skills Role {self.suffix}"})
			.insert()
			.name
		)

		self.skill_python = self._make_skill(f"ZZPython{self.suffix}", aliases="py")
		self.skill_js = self._make_skill(
			f"ZZJavaScript{self.suffix}", aliases=f"JS{self.suffix}, ECMAScript{self.suffix}"
		)
		self.skill_java = self._make_skill(f"ZZJava{self.suffix}")
		self.skill_excel = self._make_skill(f"ZZExcel{self.suffix}")

		self.manager = self._make_employee(
			f"zz-manager-{self.suffix}@example.com", department=self.department, designation=self.designation
		)
		self.employee = self._make_employee(
			f"zz-emp-{self.suffix}@example.com",
			department=self.department,
			designation=self.designation,
			reports_to=self.manager,
		)
		self.stranger = self._make_employee(
			f"zz-stranger-{self.suffix}@example.com", department=self.department
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._commit_patch.stop()
		frappe.db.rollback()
		self._clear_watermarks()
		self.assertEqual(frappe.db.count("Email Queue"), self._email_queue_count_before, "a test queued mail")

	def _clear_watermarks(self):
		for source in WATERMARK_SOURCES:
			frappe.cache().delete_value(sc._CACHE_PREFIX + source)
		self._email_queue_count_before = frappe.db.count("Email Queue")

	def _make_skill(self, name, aliases=None):
		doc = frappe.get_doc({"doctype": "Skill", "skill_name": name, "aliases": aliases})
		doc.insert()
		return doc.name

	def _make_user(self, email, roles=None):
		if frappe.db.exists("User", email):
			return email
		# No `new_password` is set: User.validate() only runs the site's password-strength check
		# (frappe/core/doctype/user/user.py:793, guarded by `if self.new_password`) when one is given.
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in (roles or ["Employee"])],
			}
		)
		doc.insert(ignore_permissions=True)
		return email

	def _make_employee(self, email, **kwargs):
		user = self._make_user(email)
		existing = frappe.db.get_value("Employee", {"user_id": user}, "name")
		if existing:
			return existing
		doc = frappe.get_doc(
			{
				"doctype": "Employee",
				"naming_series": "EMP-",
				"first_name": email,
				"company": self.company,
				"user_id": user,
				"date_of_birth": "1990-05-08",
				"date_of_joining": "2013-01-01",
				"department": self.department,
				"gender": "Female",
				"status": "Active",
			}
		)
		doc.update(kwargs)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _make_goal(self, employee, description, end_date=None):
		return frappe.get_doc(
			{
				"doctype": "Goal",
				"employee": employee,
				"goal_name": "Quarterly goal",
				"description": description,
				"start_date": add_days(end_date or nowdate(), -30),
				"end_date": end_date or nowdate(),
				"status": "In Progress",
				"company": self.company,
			}
		).insert()

	def _make_feedback(self, employee, feedback_text):
		"""A submitted feedback row for the read side only: the doctype's own validate() wants a whole
		appraisal chain (cycle, template, appraisal, a different reviewer) that has nothing to do with
		what the matcher reads (`name`, `employee`, `feedback`, `added_on`, `docstatus`)."""
		doc = frappe.get_doc(
			{
				"doctype": "Employee Performance Feedback",
				"employee": employee,
				"reviewer": self.manager,
				"feedback": feedback_text,
				"added_on": now_datetime(),
				"company": self.company,
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		frappe.db.set_value("Employee Performance Feedback", doc.name, "docstatus", 1, update_modified=False)
		doc.reload()
		return doc


class TestMatcher(SkillsCloudTestCase):
	def test_word_boundary_avoids_substring_false_positive(self):
		pattern, terms = sc.get_skill_matcher()
		hits = sc.find_skill_evidence(f"I write ZZJavaScript{self.suffix} for a living.", pattern, terms)
		self.assertNotIn(self.skill_java, [h[0] for h in hits], "java matched inside javascript")
		self.assertIn(self.skill_js, [h[0] for h in hits])

	def test_java_and_javascript_both_match_when_both_present(self):
		pattern, terms = sc.get_skill_matcher()
		hits = dict(
			sc.find_skill_evidence(
				f"I know ZZJava{self.suffix} and ZZJavaScript{self.suffix} well.", pattern, terms
			)
		)
		self.assertIn(self.skill_java, hits)
		self.assertIn(self.skill_js, hits)

	def test_excel_does_not_match_inside_excellent(self):
		pattern, terms = sc.get_skill_matcher()
		hits = sc.find_skill_evidence(f"She is ZZExcel{self.suffix}lent at everything.", pattern, terms)
		self.assertEqual(hits, [])

	def test_excel_matches_as_its_own_word(self):
		pattern, terms = sc.get_skill_matcher()
		hits = sc.find_skill_evidence(f"She is great with ZZExcel{self.suffix}.", pattern, terms)
		self.assertEqual([h[0] for h in hits], [self.skill_excel])

	def test_alias_is_matched(self):
		pattern, terms = sc.get_skill_matcher()
		hits = sc.find_skill_evidence("Wrote some py scripts for this.", pattern, terms)
		# "py" alone would collide with unrelated fixtures on other tests' skills; use the employee's
		# own unique alias instead.
		hits = sc.find_skill_evidence(f"Built the frontend in JS{self.suffix} last sprint.", pattern, terms)
		self.assertEqual([h[0] for h in hits], [self.skill_js])

	def test_each_skill_matched_once_per_text_even_if_mentioned_twice(self):
		pattern, terms = sc.get_skill_matcher()
		text = f"ZZExcel{self.suffix} is great. Honestly, ZZExcel{self.suffix} is the best."
		hits = sc.find_skill_evidence(text, pattern, terms)
		self.assertEqual(len(hits), 1)

	def test_no_skills_returns_no_matcher(self):
		# Isolate from the site's real Skill catalogue by checking the empty-terms branch directly.
		self.assertIsNone(sc._compile_matcher({}))


class TestConfidenceFormula(SkillsCloudTestCase):
	def test_fresh_evidence_scores_the_full_source_weight(self):
		self.assertEqual(
			sc._confidence("Employee Performance Feedback", nowdate()),
			sc.SOURCE_WEIGHT["Employee Performance Feedback"],
		)

	def test_confidence_decays_with_age(self):
		fresh = sc._confidence("Goal", nowdate())
		old = sc._confidence("Goal", add_days(nowdate(), -sc.RECENCY_HALF_LIFE_DAYS))
		self.assertAlmostEqual(old, round(fresh * 0.5), delta=1)

	def test_confidence_never_drops_below_the_floor(self):
		very_old = sc._confidence("Timesheet", add_days(nowdate(), -100000))
		self.assertGreaterEqual(very_old, 1)
		self.assertLessEqual(very_old, sc.SOURCE_WEIGHT["Timesheet"] * sc.MIN_RECENCY_FACTOR + 1)

	def test_confidence_is_bounded_0_to_100(self):
		for source in sc.SOURCE_WEIGHT:
			self.assertLessEqual(sc._confidence(source, nowdate()), 100)
			self.assertGreaterEqual(sc._confidence(source, add_days(nowdate(), -100000)), 1)


class TestMatchFormula(unittest.TestCase):
	def test_no_skill_rows_scores_zero(self):
		self.assertEqual(sc.compute_match([], [], {"Python": 1.0}), 0)

	def test_full_required_match_scores_100(self):
		rows = [{"skill": "Python", "minimum_proficiency": 0.6, "requirement": "Required"}]
		self.assertEqual(sc.compute_match(rows, [], {"Python": 0.8}), 100)

	def test_missing_required_skill_scores_zero_for_that_skill(self):
		rows = [{"skill": "Python", "minimum_proficiency": 0.6, "requirement": "Required"}]
		self.assertEqual(sc.compute_match(rows, [], {}), 0)

	def test_partial_proficiency_gives_partial_credit(self):
		rows = [{"skill": "Python", "minimum_proficiency": 0.8, "requirement": "Required"}]
		self.assertEqual(sc.compute_match(rows, [], {"Python": 0.4}), 50)

	def test_required_weighted_75_nice_to_have_25(self):
		required = [{"skill": "Python", "minimum_proficiency": 0.5, "requirement": "Required"}]
		nice = [{"skill": "Excel", "minimum_proficiency": 0.5, "requirement": "Nice to have"}]
		# has required fully, not the nice-to-have at all
		self.assertEqual(sc.compute_match(required, nice, {"Python": 1.0}), 75)

	def test_no_minimum_proficiency_gives_full_credit_for_any_level(self):
		rows = [{"skill": "Python", "minimum_proficiency": 0, "requirement": "Required"}]
		self.assertEqual(sc.compute_match(rows, [], {"Python": 0.01}), 100)

	def test_over_proficient_is_capped_not_bonus(self):
		rows = [{"skill": "Python", "minimum_proficiency": 0.4, "requirement": "Required"}]
		self.assertEqual(sc.compute_match(rows, [], {"Python": 1.0}), 100)


class TestGapAnalysis(SkillsCloudTestCase):
	def setUp(self):
		super().setUp()
		designation_doc = frappe.get_doc("Designation", self.designation)
		designation_doc.append("skills", {"skill": self.skill_python})
		designation_doc.save()

	def test_missing_skill_reported_without_a_minimum(self):
		gaps = sc.analyze_skill_gaps(self.employee, designation=self.designation)
		self.assertEqual(len(gaps), 1)
		self.assertEqual(gaps[0]["skill"], self.skill_python)
		self.assertEqual(gaps[0]["gap_type"], "missing")
		self.assertIsNone(gaps[0]["minimum_proficiency"])

	def test_no_gap_once_the_skill_is_on_the_map(self):
		self._give_skill(self.employee, self.skill_python, 0.6)
		gaps = sc.analyze_skill_gaps(self.employee, designation=self.designation)
		self.assertEqual(gaps, [])

	def test_opportunity_gap_can_report_a_shortfall(self):
		opp = self._make_opportunity(self.manager, skills=[(self.skill_python, 0.8, "Required")])
		self._give_skill(self.employee, self.skill_python, 0.4)
		gaps = sc.analyze_skill_gaps(self.employee, opportunity=opp.name)
		self.assertEqual(gaps[0]["gap_type"], "shortfall")
		self.assertEqual(gaps[0]["minimum_proficiency"], 0.8)

	def _give_skill(self, employee, skill, proficiency):
		doc = sc._employee_skill_map_doc(employee)
		doc.append(
			"employee_skills", {"skill": skill, "proficiency": proficiency, "evaluation_date": nowdate()}
		)
		doc.save(ignore_permissions=True)

	def _make_opportunity(self, owner_employee, skills):
		doc = frappe.get_doc(
			{
				"doctype": "Talent Opportunity",
				"title": f"ZZ Opportunity {frappe.generate_hash(length=4)}",
				"type": "Project",
				"owner_employee": owner_employee,
				"status": "Open",
				"skills": [{"skill": s, "minimum_proficiency": p, "requirement": r} for s, p, r in skills],
			}
		)
		doc.insert(ignore_permissions=True)
		return doc


class TestPermissions(SkillsCloudTestCase):
	def test_employee_can_view_own_data(self):
		self.assertTrue(
			sc.can_view_employee_data(
				self.employee, user=frappe.db.get_value("Employee", self.employee, "user_id")
			)
		)

	def test_manager_can_view_reports_data(self):
		manager_user = frappe.db.get_value("Employee", self.manager, "user_id")
		self.assertTrue(sc.can_view_employee_data(self.employee, user=manager_user))

	def test_stranger_cannot_view_others_data(self):
		stranger_user = frappe.db.get_value("Employee", self.stranger, "user_id")
		self.assertFalse(sc.can_view_employee_data(self.employee, user=stranger_user))

	def test_report_cannot_view_managers_data(self):
		employee_user = frappe.db.get_value("Employee", self.employee, "user_id")
		self.assertFalse(sc.can_view_employee_data(self.manager, user=employee_user))

	def test_hr_user_can_view_anyones_data(self):
		hr_user = self._make_user(f"zz-hr-{self.suffix}@example.com", roles=["HR User"])
		self.assertTrue(sc.can_view_employee_data(self.stranger, user=hr_user))

	def test_manager_cannot_act_on_behalf_of_a_report(self):
		manager_user = frappe.db.get_value("Employee", self.manager, "user_id")
		self.assertFalse(sc.can_act_as_employee(self.employee, user=manager_user))

	def test_employee_can_act_for_self(self):
		employee_user = frappe.db.get_value("Employee", self.employee, "user_id")
		self.assertTrue(sc.can_act_as_employee(self.employee, user=employee_user))

	def test_require_view_access_throws_for_a_stranger(self):
		stranger_user = frappe.db.get_value("Employee", self.stranger, "user_id")
		with self.assertRaises(frappe.PermissionError):
			sc._require_view_access(self.employee, user=stranger_user)

	def test_get_employee_skill_summary_denies_a_stranger(self):
		stranger_user = frappe.db.get_value("Employee", self.stranger, "user_id")
		frappe.set_user(stranger_user)
		with self.assertRaises(frappe.PermissionError):
			sc.get_employee_skill_summary(employee=self.employee)


class TestRefreshSuggestions(SkillsCloudTestCase):
	def test_creates_a_suggestion_with_its_evidence_sentence(self):
		self._make_goal(self.employee, f"Get better at ZZPython{self.suffix} this quarter.")
		result = sc.refresh_suggestions(employee=self.employee)
		self.assertGreaterEqual(result["created"], 1)

		rows = frappe.get_all(
			"Skill Suggestion",
			filters={"employee": self.employee, "skill": self.skill_python},
			fields=["evidence", "source_doctype", "confidence", "status"],
		)
		self.assertEqual(len(rows), 1)
		self.assertIn(f"ZZPython{self.suffix}", rows[0].evidence)
		self.assertEqual(rows[0].source_doctype, "Goal")
		self.assertEqual(rows[0].status, "Suggested")

	def test_does_not_suggest_a_skill_the_employee_already_has(self):
		doc = sc._employee_skill_map_doc(self.employee)
		doc.append(
			"employee_skills", {"skill": self.skill_python, "proficiency": 0.6, "evaluation_date": nowdate()}
		)
		doc.save(ignore_permissions=True)

		self._make_goal(self.employee, f"Keep sharpening ZZPython{self.suffix}.")
		sc.refresh_suggestions(employee=self.employee)
		self.assertFalse(
			frappe.db.exists("Skill Suggestion", {"employee": self.employee, "skill": self.skill_python})
		)

	def test_never_resuggests_a_dismissed_skill(self):
		self._make_goal(self.employee, f"Improve ZZExcel{self.suffix} skills.")
		sc.refresh_suggestions(employee=self.employee)
		name = frappe.db.get_value(
			"Skill Suggestion", {"employee": self.employee, "skill": self.skill_excel}, "name"
		)
		frappe.set_user(frappe.db.get_value("Employee", self.employee, "user_id"))
		sc.dismiss_suggestion(name)
		frappe.set_user("Administrator")

		self._make_goal(self.employee, f"Really want to improve ZZExcel{self.suffix} this time.")
		sc.refresh_suggestions(employee=self.employee)
		count = frappe.db.count("Skill Suggestion", {"employee": self.employee, "skill": self.skill_excel})
		self.assertEqual(count, 1)

	def test_incremental_scan_only_advances_past_the_watermark(self):
		# Deterministic regardless of how much pre-existing demo data the site holds: pretend the
		# global scan is already caught up to just now, then create evidence newer than that.
		sc._set_watermark("Goal", add_to_date(now_datetime(), seconds=-2))
		self._make_goal(self.employee, f"Learn ZZJava{self.suffix} basics.")

		first = sc.refresh_suggestions()
		self.assertEqual(first["created"], 1)

		second = sc.refresh_suggestions()
		self.assertEqual(
			second["created"], 0, "re-running the global scan should not recreate the same suggestion"
		)

	def test_a_skill_seen_in_two_sources_is_suggested_once_from_the_strongest(self):
		# feedback outweighs a goal (SOURCE_WEIGHT), and a skill is never pending twice for one person:
		# the person reviews one suggestion, carrying the sentence that justified it best
		self._make_goal(self.employee, f"Learn ZZExcel{self.suffix} this quarter.")
		self._make_feedback(self.employee, f"Shows real strength in ZZExcel{self.suffix} work.")
		sc.refresh_suggestions(employee=self.employee)
		rows = frappe.get_all(
			"Skill Suggestion",
			filters={"employee": self.employee, "skill": self.skill_excel},
			fields=["source_doctype", "evidence"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].source_doctype, "Employee Performance Feedback")
		self.assertIn("real strength", rows[0].evidence)

		# a second scan finds the same evidence again and adds nothing
		sc.refresh_suggestions(employee=self.employee)
		self.assertEqual(
			frappe.db.count("Skill Suggestion", {"employee": self.employee, "skill": self.skill_excel}), 1
		)

	def test_max_suggestions_per_employee_is_bounded(self):
		skills = [
			self._make_skill(f"ZZBound{self.suffix}{i}") for i in range(sc.MAX_SUGGESTIONS_PER_EMPLOYEE + 5)
		]
		sentence = " ".join(skills) + "."
		self._make_goal(self.employee, sentence)
		sc.refresh_suggestions(employee=self.employee)
		count = frappe.db.count("Skill Suggestion", {"employee": self.employee})
		self.assertLessEqual(count, sc.MAX_SUGGESTIONS_PER_EMPLOYEE)

	def test_scheduled_wrapper_commits(self):
		self._commit_patch.stop()
		with patch.object(frappe.db, "commit", MagicMock()) as mock_commit:
			sc.scheduled_refresh_suggestions()
			mock_commit.assert_called_once()
		self._commit_patch.start()


class TestReviewSuggestions(SkillsCloudTestCase):
	def _make_suggestion(self, employee=None, skill=None, source=None):
		"""A suggestion whose source is a real Goal of the employee (the Dynamic Link is validated on
		every save, so a made-up name would fail the moment the suggestion is reviewed)."""
		employee = employee or self.employee
		skill = skill or self.skill_python
		source = source or self._make_goal(employee, "Some evidence sentence.")
		doc = frappe.get_doc(
			{
				"doctype": "Skill Suggestion",
				"employee": employee,
				"skill": skill,
				"confidence": 80,
				"evidence": "Some evidence sentence.",
				"evidence_date": nowdate(),
				"source_doctype": "Goal",
				"source_name": source.name,
				"status": "Suggested",
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_accept_writes_the_employee_skill_map(self):
		name = self._make_suggestion()
		frappe.set_user(frappe.db.get_value("Employee", self.employee, "user_id"))
		sc.accept_suggestion(name)
		frappe.set_user("Administrator")

		self.assertTrue(frappe.db.exists("Skill Suggestion", {"name": name, "status": "Accepted"}))
		skill_map = frappe.get_doc("Employee Skill Map", self.employee)
		self.assertIn(self.skill_python, [r.skill for r in skill_map.employee_skills])

	def test_dismiss_does_not_touch_the_skill_map(self):
		name = self._make_suggestion()
		frappe.set_user(frappe.db.get_value("Employee", self.employee, "user_id"))
		sc.dismiss_suggestion(name)
		frappe.set_user("Administrator")

		self.assertTrue(frappe.db.exists("Skill Suggestion", {"name": name, "status": "Dismissed"}))
		self.assertFalse(frappe.db.exists("Employee Skill Map", self.employee))

	def test_accepting_retires_sibling_suggestions_for_the_same_skill(self):
		# the same skill seen in two different goals: accepting one review settles both
		name_1 = self._make_suggestion()
		name_2 = self._make_suggestion(source=self._make_goal(self.employee, "Another sentence."))
		frappe.set_user(frappe.db.get_value("Employee", self.employee, "user_id"))
		sc.accept_suggestion(name_1)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Skill Suggestion", name_2, "status"), "Accepted")

	def test_a_stranger_cannot_accept_someone_elses_suggestion(self):
		name = self._make_suggestion()
		frappe.set_user(frappe.db.get_value("Employee", self.stranger, "user_id"))
		with self.assertRaises(frappe.PermissionError):
			sc.accept_suggestion(name)
		frappe.set_user("Administrator")

	def test_cannot_review_an_already_reviewed_suggestion_twice(self):
		name = self._make_suggestion()
		frappe.set_user(frappe.db.get_value("Employee", self.employee, "user_id"))
		sc.accept_suggestion(name)
		with self.assertRaises(frappe.ValidationError):
			sc.dismiss_suggestion(name)
		frappe.set_user("Administrator")


if __name__ == "__main__":
	unittest.main()
