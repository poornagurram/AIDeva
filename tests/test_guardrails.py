from xagent.guardrails import check_text, check_thread


def test_clean_post_passes(cfg):
    assert check_text("Evals beat vibes. Write ten test cases before touching the prompt again.", cfg, kind="post")


def test_hashtags_links_mentions_blocked_in_posts(cfg):
    v = check_text("Great tool #AI https://example.com @someone", cfg, kind="post")
    joined = " ".join(v.problems)
    assert not v and "hashtags" in joined and "links" in joined and "@mentions" in joined


def test_cta_reply_may_have_one_link(cfg):
    assert check_text("I write about this weekly: https://example.com", cfg, kind="cta_reply")
    assert not check_text("a https://a.com b https://b.com", cfg, kind="cta_reply")


def test_banned_phrases_and_bait(cfg):
    assert not check_text("This is a game-changer for agents.", cfg, kind="post")
    assert not check_text("Like and retweet if you agree", cfg, kind="post")
    assert not check_text("🚨 BREAKING: new model", cfg, kind="post")


def test_placeholders_rejected(cfg):
    assert not check_text("Check out [link] for more", cfg, kind="post")
    assert not check_text("Built by {product_name}", cfg, kind="post")


def test_length_limit(cfg):
    assert not check_text("a" * 300, cfg, kind="post")
    assert check_text("a" * 300, cfg, kind="post", max_length=2500)


def test_duplicate_of_history_rejected(cfg):
    history = ["Most LLM apps don't have a model problem. They have an eval problem."]
    assert not check_text("Most LLM apps don't have a model problem, they have an eval problem.", cfg, kind="post",
                          history=history)


def test_thread_rules(cfg):
    assert not check_thread(["only one"], cfg)
    assert check_thread(["First part with a hook.", "Second part with detail.", "Third part with the payoff."], cfg)
    assert not check_thread(["ok", "x" * 300], cfg)
