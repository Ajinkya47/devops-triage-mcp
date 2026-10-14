from duplicates import tokenize, jaccard, similarity, find_similar

TEMPLATE_ONLY = """### Checked other resources

- [x] I posted a self-contained, minimal, reproducible example.

### Reproduction Steps / Example Code (Python)

_No response_
"""


def iss(number, title, body=""):
    return {"number": number, "title": title, "body_excerpt": body}


# ---------- Tokenizing ----------

def test_stopwords_and_endings_are_removed():
    assert tokenize("The app crashes when uploading files") == ["app", "crash", "upload", "file"]

def test_digits_and_units_are_split():
    assert tokenize("over 10MB") == ["10", "mb"]

def test_empty_and_none():
    assert tokenize("") == []
    assert tokenize(None) == []

def test_jaccard_basics():
    assert jaccard(["a1", "b1"], ["a1", "b1"]) == 1.0
    assert jaccard(["a1"], ["b1"]) == 0.0
    assert jaccard([], ["b1"]) == 0.0


# ---------- Title-only similarity ----------

def test_identical_titles_are_duplicates():
    r = similarity(iss(1, "File upload crashes for files over 10 MB"),
                   iss(2, "File upload crashes for files over 10MB"))
    assert r["verdict"] == "likely_duplicate"
    assert r["score"] == 1.0

def test_extra_words_still_duplicate():
    r = similarity(iss(1, "File upload crashes for files over 10 MB"),
                   iss(2, "File upload crashes for files over 10 MB on Windows"))
    assert r["verdict"] == "likely_duplicate"

def test_partial_overlap_is_related():
    r = similarity(iss(1, "Login fails with special characters in password"),
                   iss(2, "Login fails with password containing unicode"))
    assert r["verdict"] == "possibly_related"

def test_unrelated_titles_are_different():
    r = similarity(iss(1, "Typo in README installation section"),
                   iss(2, "Memory leak when streaming large responses"))
    assert r["verdict"] == "different"


# ---------- Body handling ----------

def test_same_title_same_body_is_duplicate():
    body = "Steps: upload a 15 MB file then the server returns a 500 error and crash"
    r = similarity(iss(1, "Crash on upload", body), iss(2, "Crash on upload", body))
    assert r["verdict"] == "likely_duplicate"
    assert r["used_body"] is True

def test_same_title_different_bodies_is_only_related():
    r = similarity(iss(1, "Crash on upload", "Alpha beta gamma delta epsilon zeta"),
                   iss(2, "Crash on upload", "One two three four five six"))
    assert r["verdict"] == "possibly_related"

def test_shared_template_does_not_create_similarity():
    r = similarity(iss(1, "Memory leak in streaming", TEMPLATE_ONLY),
                   iss(2, "Typo in docs", TEMPLATE_ONLY))
    assert r["verdict"] == "different"
    assert r["used_body"] is False


# ---------- find_similar ----------

def test_find_similar_excludes_itself_and_sorts():
    target = iss(5, "File upload crashes for files over 10 MB")
    pool = [
        target,
        iss(1, "File upload crashes for files over 10 MB on Windows"),
        iss(2, "Typo in README"),
        iss(3, "File upload crashes for files over 10 MB"),
    ]
    out = find_similar(target, pool)
    assert [m["number"] for m in out] == [3, 1]
    assert all(m["number"] != 5 for m in out)

def test_top_n_limits_results():
    target = iss(9, "File upload crashes for files over 10 MB")
    pool = [target] + [iss(i, "File upload crashes for files over 10 MB") for i in range(1, 5)]
    assert len(find_similar(target, pool, top_n=2)) == 2

def test_missing_fields_do_not_crash():
    out = find_similar({"number": 1, "title": None, "body_excerpt": None},
                       [{"number": 2, "title": None, "body_excerpt": None}])
    assert out == []