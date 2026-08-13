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


if __name__ == "__main__":
    unittest.main()
