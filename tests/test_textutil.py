from xagent.textutil import clean_post, find_urls, similarity, weighted_length, with_utm


def test_ascii_counts_one_per_char():
    assert weighted_length("hello world") == 11


def test_urls_count_23_regardless_of_length():
    assert weighted_length("see https://example.com/a/very/long/path?x=1") == 4 + 23
    assert weighted_length("x.ai") == 23


def test_emoji_and_cjk_count_two():
    assert weighted_length("🔥") == 2
    assert weighted_length("👨‍👩‍👧") == 2  # ZWJ family is one glyph
    assert weighted_length("🇺🇸") == 2  # flag pair
    assert weighted_length("👍🏽") == 2  # skin tone modifier
    assert weighted_length("日本") == 4


def test_find_urls_bare_domains_but_not_versions():
    assert find_urls("try cursor.com today") == ["cursor.com"]
    assert find_urls("version 1.2 and Next.js") == []
    assert find_urls("email me @foo.com") == []


def test_similarity_detects_near_duplicates():
    a = "Most LLM apps don't have a model problem. They have an eval problem."
    b = "Most LLM apps don't have a model problem - they have an eval problem!"
    c = "Pricing page rewrites moved trial conversion more than any feature."
    assert similarity(a, b) > 0.9
    assert similarity(a, c) < 0.5


def test_with_utm_preserves_existing_params():
    url = with_utm("https://x.com/p?ref=1&utm_source=keep", "x", "social", "c", content="insight")
    assert "ref=1" in url and "utm_source=keep" in url and "utm_medium=social" in url and "utm_content=insight" in url


def test_clean_post_strips_wrapping_quotes_only():
    assert clean_post('"Ship it."') == "Ship it."
    assert clean_post('He said "no" and "yes"') == 'He said "no" and "yes"'
    assert clean_post("a\n\n\n\nb") == "a\n\nb"
