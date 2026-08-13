"""Central, editable definitions for Student Talent Pool capability tracks."""

ROLE_DEFINITIONS = {
    "MERN": {"required": ["React", "Node.js", "Express", "MongoDB"], "preferred": ["JavaScript", "TypeScript", "Redux", "REST API"]},
    "Python Backend": {"required": ["Python", "Django", "Flask", "FastAPI"], "preferred": ["PostgreSQL", "SQL", "REST API", "Redis"]},
    "Python Full Stack": {"required": ["Python", "React", "Django", "Flask", "FastAPI"], "preferred": ["JavaScript", "HTML", "CSS", "PostgreSQL", "REST API"]},
    "Java Full Stack": {"required": ["Java", "Spring Boot"], "preferred": ["React", "JavaScript", "HTML", "CSS", "SQL", "REST API"]},
    "Frontend Development": {"required": ["React", "JavaScript", "HTML", "CSS"], "preferred": ["TypeScript", "Next.js", "Redux", "Tailwind"]},
    "Backend Development": {"required": ["Node.js", "Python", "Java", "Django", "FastAPI", "Spring Boot"], "preferred": ["SQL", "PostgreSQL", "MongoDB", "REST API", "Docker"]},
    "Mobile App Development": {"required": ["Flutter", "React Native", "Kotlin", "Swift"], "preferred": ["Firebase", "REST API", "JavaScript"]},
    "Testing / QA": {"required": ["Selenium"], "preferred": ["Python", "Java", "JavaScript", "REST API"]},
    "GenAI": {"required": ["GenAI", "LLM", "RAG", "LangChain", "OpenAI"], "preferred": ["LlamaIndex", "Hugging Face", "Vector Database", "NLP", "Prompt Engineering"]},
    "AI / Machine Learning": {"required": ["Machine Learning", "Deep Learning", "scikit-learn", "TensorFlow", "PyTorch"], "preferred": ["Python", "Pandas", "NumPy", "NLP", "Computer Vision"]},
    "Data Science": {"required": ["Python", "Pandas", "NumPy", "Data Science"], "preferred": ["SQL", "scikit-learn", "Machine Learning", "Jupyter"]},
    "Data Analytics": {"required": ["SQL", "Pandas", "Data Science"], "preferred": ["Python", "Excel", "Tableau", "Power BI"]},
    "Cybersecurity": {"required": ["Linux"], "preferred": ["Python", "Docker", "AWS", "Kubernetes"]},
    "DevOps / Cloud": {"required": ["Docker", "AWS", "Azure", "GCP", "CI/CD"], "preferred": ["Kubernetes", "Terraform", "Jenkins", "Linux", "Git"]},
}

COMBINED_TRACKS = (("MERN + GenAI", "MERN", "GenAI"), ("Python + GenAI", "Python Backend", "GenAI"), ("MERN + AI/ML", "MERN", "AI / Machine Learning"), ("Python + AI/ML", "Python Backend", "AI / Machine Learning"), ("Java + AI/ML", "Java Full Stack", "AI / Machine Learning"))
