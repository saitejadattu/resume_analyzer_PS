import unittest

from resume_shortlisting.keyword_matcher import match_resume
from resume_shortlisting.models import Candidate, GithubLink, GithubStatus, JDSpec
from resume_shortlisting.parser import parse_resume
from resume_shortlisting.scorer import score_candidate
from resume_shortlisting.skills_kb import load_kb
from resume_shortlisting.tech_detector import enrich_projects


class EvidenceScoringTests(unittest.TestCase):
    def setUp(self):
        self.kb = load_kb()
        self.jd = JDSpec(required=["Python", "Django", "SQL"], search_modes={"Python": "skills", "Django": "project", "SQL": "skills_or_project"})
        self.resume = parse_resume("""Skills\nPython, SQL, Django\nProjects\nHospital Management System\nBuilt with Django.\nTech Stack: Django, PostgreSQL\nGitHub: https://github.com/example/hospital\n""")
        enrich_projects(self.resume.project_list, self.kb)

    def test_modes_and_project_precedence(self):
        report = match_resume(self.resume, self.jd, self.kb)
        found = {m.skill: m for m in report.matches}
        self.assertEqual(found["Python"].match_type, "skills")
        self.assertEqual(found["Django"].match_type, "project")
        self.assertEqual(found["Django"].source, "tech_stack")
        self.assertEqual(found["SQL"].match_type, "skills")

    def test_project_repository_earns_the_full_bonus_whether_or_not_it_was_checked(self):
        # The gate and the bonus both test for presence: skipping validation
        # must not cost a candidate points.
        report = match_resume(self.resume, self.jd, self.kb)
        skipped = score_candidate(Candidate(name="A"), self.resume, self.jd, report, GithubStatus.NONE, [], github_checked=False)
        working = score_candidate(Candidate(name="A"), self.resume, self.jd, report, GithubStatus.WORKING, [], github_checked=True, github_links=[GithubLink(url="https://github.com/example/hospital", status=GithubStatus.WORKING)])
        self.assertEqual(skipped.score_breakdown["project_evidence"], 15)
        self.assertEqual(working.score_breakdown["project_evidence"], 15)
        self.assertEqual(working.score, skipped.score)

    def test_a_disproved_repository_falls_back_to_a_weaker_tier(self):
        report = match_resume(self.resume, self.jd, self.kb)
        broken = score_candidate(
            Candidate(name="A"), self.resume, self.jd, report, GithubStatus.NOT_FOUND,
            self.resume.github_urls, github_checked=True,
            github_links=[GithubLink(url="https://github.com/example/hospital", status=GithubStatus.NOT_FOUND)],
        )
        # The repo is disproved, but a GitHub URL still exists in the resume,
        # so the candidate passes the gate on the weaker "elsewhere" tier.
        self.assertEqual(broken.score_breakdown["project_evidence"], 6)
        self.assertNotEqual(broken.recommendation, "Reject")

    def test_live_link_only_is_worth_seventy_percent(self):
        # The GitHub link sits in the header, not in the project block, so the
        # project itself offers only a deployment as evidence.
        resume = parse_resume(
            "ADA LOVELACE  https://github.com/ada\n"
            "Skills\nPython, SQL, Django\nProjects\nHospital System\n"
            "Built with Django.\nTech Stack: Django, PostgreSQL\n"
            "Live: https://hospital.example.app\n"
        )
        enrich_projects(resume.project_list, self.kb)
        report = match_resume(resume, self.jd, self.kb)
        result = score_candidate(Candidate(name="A"), resume, self.jd, report,
                                 GithubStatus.NONE, resume.github_urls, github_checked=False)
        self.assertEqual(result.score_breakdown["project_evidence"], 10.5)

    def test_missing_github_anywhere_is_rejected_regardless_of_score(self):
        report = match_resume(self.resume, self.jd, self.kb)
        resume = self.resume.model_copy(update={"github_urls": [], "candidate_github_urls": []})
        result = score_candidate(Candidate(name="A"), resume, self.jd, report,
                                 GithubStatus.NONE, [], github_checked=False)
        self.assertEqual(result.score_breakdown["required_keywords"], 80.0)
        self.assertEqual(result.recommendation, "Reject")
        self.assertIn("no GitHub link", result.remarks)

    def test_project_mode_does_not_accept_skills_only(self):
        resume = parse_resume("Skills\nPython, Django\nProjects\nPortfolio\nHTML, CSS")
        enrich_projects(resume.project_list, self.kb)
        report = match_resume(resume, JDSpec(required=["Django"], search_modes={"Django": "project"}), self.kb)
        self.assertEqual(report.matches, [])

    def test_experience_mode_isolated_from_skills_and_projects(self):
        resume = parse_resume("Skills\nn8n\nProjects\nReact Dashboard\nExperience\nWorked with Python.")
        enrich_projects(resume.project_list, self.kb)

        experience = match_resume(
            resume, JDSpec(required=["n8n"], search_modes={"n8n": "experience"}), self.kb
        )
        python = match_resume(
            resume, JDSpec(required=["Python"], search_modes={"Python": "experience"}), self.kb
        )
        react = match_resume(
            resume, JDSpec(required=["React"], search_modes={"React": "experience"}), self.kb
        )
        self.assertEqual(experience.matches, [])
        self.assertEqual(python.matches[0].match_type, "experience")
        self.assertEqual(react.matches, [])

    def test_whole_resume_mode_searches_all_resume_text(self):
        resume = parse_resume("Skills\nPython\nProjects\nReact Dashboard\nExperience\nWorked with n8n.")
        report = match_resume(
            resume,
            JDSpec(required=["n8n"], search_modes={"n8n": "whole_resume"}),
            self.kb,
        )
        self.assertEqual(report.matches[0].match_type, "whole_resume")
        self.assertEqual(report.matched_in["n8n"], ["Whole Resume"])

    def test_whole_resume_mode_returns_no_match_when_keyword_is_absent(self):
        resume = parse_resume("Skills\nPython\nProjects\nReact Dashboard\nExperience\nWorked with Java.")
        report = match_resume(
            resume,
            JDSpec(required=["n8n"], search_modes={"n8n": "whole_resume"}),
            self.kb,
        )
        self.assertEqual(report.matches, [])

    def test_discovery_matches_do_not_affect_score(self):
        resume = parse_resume("Skills\nPython\nProjects\nReact Dashboard\nExperience\nWorked with n8n.")
        candidate = Candidate(name="A")
        for mode in ("experience", "whole_resume"):
            with self.subTest(mode=mode):
                jd = JDSpec(required=["n8n"], search_modes={"n8n": mode})
                report = match_resume(resume, jd, self.kb)
                result = score_candidate(
                    candidate, resume, jd, report, GithubStatus.NONE, [], github_checked=False
                )
                self.assertEqual(result.score, 0.0)
                self.assertEqual(result.recommendation, "Reject")

    def test_discovery_and_scoring_evidence_can_coexist(self):
        resume = parse_resume(
            "Skills\nPython\nProjects\nReact Dashboard\nExperience\nWorked with n8n."
        )
        jd = JDSpec(
            required=["Python", "n8n"],
            search_modes={"Python": "skills", "n8n": "whole_resume"},
        )
        report = match_resume(resume, jd, self.kb)
        result = score_candidate(
            Candidate(name="A"), resume, jd, report, GithubStatus.NONE, [], github_checked=False
        )
        evidence = {item.skill: item for item in result.keyword_evidence}
        self.assertEqual(evidence["Python"].match_type, "skills")
        self.assertEqual(evidence["n8n"].match_type, "whole_resume")
        # Python earns its full half of the 80-point pool; the Whole Resume
        # discovery match still scores nothing.
        self.assertEqual(result.score_breakdown["required_keywords"], 40.0)
        self.assertEqual(result.score, 40.0)


if __name__ == "__main__":
    unittest.main()
