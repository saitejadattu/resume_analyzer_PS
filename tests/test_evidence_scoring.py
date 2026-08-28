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

    def test_project_github_and_skip_are_distinct(self):
        report = match_resume(self.resume, self.jd, self.kb)
        skipped = score_candidate(Candidate(name="A"), self.resume, self.jd, report, GithubStatus.NONE, [], github_checked=False)
        working = score_candidate(Candidate(name="A"), self.resume, self.jd, report, GithubStatus.WORKING, [], github_checked=True, github_links=[GithubLink(url="https://github.com/example/hospital", status=GithubStatus.WORKING)])
        self.assertEqual(skipped.score_breakdown["project_github"], 5)
        self.assertEqual(working.score_breakdown["project_github"], 10)
        self.assertGreater(working.score, skipped.score)

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
        self.assertEqual(result.score, 20.0)


if __name__ == "__main__":
    unittest.main()
