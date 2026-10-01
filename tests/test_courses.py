from jobhunter.models import Job
from jobhunter.tailor import courses, engine, snippet_bank

ALL = ["Image Processing", "3D Graphics Algorithms", "Data Science", "Computer Architecture",
       "Biomedical Imaging", "Pattern Recognition and Machine Learning for Image Understanding",
       "Advanced Methods for Computer Vision/Image Analysis"]
PR = "Pattern Recognition and Machine Learning for Image Understanding"


def test_a_vision_job_keeps_the_image_courses():
    kept = courses.choose(ALL, "computer vision engineer: image segmentation and 3d point cloud work")
    assert len(kept) == courses.KEEP
    assert "Image Processing" in kept and "3D Graphics Algorithms" in kept
    assert "Advanced Methods for Computer Vision/Image Analysis" in kept


def test_a_medical_job_keeps_biomedical_imaging():
    assert "Biomedical Imaging" in courses.choose(ALL, "clinical imaging scientist, medical devices")


def test_a_job_with_no_matching_words_keeps_the_broadly_useful_courses():
    kept = courses.choose(ALL, "backend developer for a payments platform")
    assert PR in kept and "Data Science" in kept
    assert "3D Graphics Algorithms" not in kept


def test_kept_courses_stay_in_the_cvs_own_order():
    kept = courses.choose(ALL, "machine learning, medical imaging, data science, computer vision")
    assert kept == [c for c in ALL if c in kept]


def test_a_short_list_is_left_alone_and_unknown_courses_rank_last():
    assert courses.choose(["A", "B"], "anything") == ["A", "B"]
    kept = courses.choose(ALL + ["Underwater Basket Weaving"], "backend developer")
    assert "Underwater Basket Weaving" not in kept


def test_keywords_match_on_word_starts_not_inside_other_words():
    assert courses._score("3D Graphics Algorithms", "enmeshed in politics") == 0
    assert courses._score("3D Graphics Algorithms", "mesh generation") == 1


def test_apply_rewrites_only_the_major_modules_bullet():
    doc = r"\resumeItem{\textit{Major Modules}: " + ", ".join(ALL) + r"} \resumeItem{Graduated with Mention.}"
    out = courses.apply(doc, "backend developer")
    assert out.count(",") == courses.KEEP - 1
    assert out.endswith(r"\resumeItem{Graduated with Mention.}")
    assert courses.apply("no modules here", "x") == "no modules here"


def test_the_real_base_cv_has_a_trimmable_modules_bullet():
    doc = snippet_bank.parse(engine.BASE_CV).document
    for name in ALL:
        assert name in doc
    assert courses.apply(doc, "backend developer") != doc


def test_tailoring_trims_the_modules_for_the_job(monkeypatch):
    monkeypatch.setattr(engine.provider, "available", lambda: False)
    job = Job(source="x", external_id="1", title="Computer Vision Engineer", company="Acme",
              description="image segmentation and 3d point cloud registration")
    tex = engine.tailor_tex(job, role_category="CV")
    line = next(l for l in tex.splitlines() if "Major Modules" in l)
    assert line.count(",") == courses.KEEP - 1
    assert "Image Processing" in line and "Computer Architecture" not in line
