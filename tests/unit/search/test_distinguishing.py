from src.search.layers.distinguishing import _PROMPT_HEADER, _build_user_msg
from tests._support.factories import candidate, raw_candidate


def test_distinguishing_prompt_includes_url_and_gallery_instruction():
    item = candidate(
        raw=raw_candidate(
            title="Product",
            url="https://example.com/item/1",
            snippet="One product",
        )
    )

    message = _build_user_msg("Product", [], {}, [item])

    assert "url: https://example.com/item/1" in message
    assert "single-product page" in _PROMPT_HEADER
    assert "never infer the product from a snippet" in _PROMPT_HEADER
