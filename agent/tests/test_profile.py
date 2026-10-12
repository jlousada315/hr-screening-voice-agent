"""The profile data that tools serve to the model."""

from urllib.parse import urlsplit

from hireme_agent.tools import profile


def test_every_example_role_is_one_spoken_sentence() -> None:
    assert profile.ROLES
    for role in profile.ROLES:
        assert role.text.endswith(".")
        assert role.text.count(". ") == 0


def test_timeline_lists_every_company_with_dates_and_provenance() -> None:
    text = profile.render_experience()
    assert "not independently verified" in text
    for company in ("Example Corp", "Sample Labs", "State University"):
        assert company in text
    assert "Jan 2023–present" in text and "Jun 2020–Dec 2022" in text


def test_links_are_unique_https_and_rendered_as_markdown() -> None:
    urls = [link.url for link in profile.LINKS]
    assert len(urls) == len(set(urls))
    assert all(urlsplit(url).scheme == "https" for url in urls)
    text = profile.render_online_presence()
    for link in profile.LINKS:
        assert f"[{link.label}]({link.url})" in text
    assert "linkedin.com" in text and "arxiv.org/abs/0000.00000" in text


def test_every_link_has_a_topic_and_every_topic_has_links() -> None:
    topics = {link.topic for link in profile.LINKS}
    assert topics == {"profile", "code", "papers", "articles"}


def test_render_links_is_filtered_by_topic_and_all_is_the_full_list() -> None:
    assert profile.render_links("all") == profile.render_online_presence()
    papers = profile.render_links("papers")
    assert papers.startswith("Here it is: [Example paper](https://arxiv.org/abs/0000.00000)")
    assert "linkedin" not in papers and "example.com/blog" not in papers
    single = profile.render_links("profile")
    assert single.startswith("Here it is: [LinkedIn](") and "\n" not in single
    for link in profile.LINKS:
        assert link.url in profile.render_links("all")


def test_highlights_are_a_short_answer_with_profile_code_and_a_paper() -> None:
    highlights = profile.render_links("highlights")
    assert highlights.count("](") == 3
    for needle in ("linkedin.com", "github.com/example)", "arxiv.org/abs/0000.00000"):
        assert needle in highlights


def test_timeline_leads_with_the_current_role_and_its_outcome() -> None:
    current = profile.ROLES[0].text
    assert current.startswith("Senior Software Engineer, Example Corp")
    assert any(ch.isdigit() for ch in current.split(":", 1)[1])  # a concrete, numbered outcome
    first_line = profile.render_experience().splitlines()[1]
    assert first_line.startswith("- Senior Software Engineer, Example Corp")


def test_notes_are_quoted_under_their_headings() -> None:
    assert profile.render_notes() == (
        "Alex's own notes, in their words:\n\n"
        "Open to:\nSenior backend or platform roles, remote or hybrid in Europe."
    )
