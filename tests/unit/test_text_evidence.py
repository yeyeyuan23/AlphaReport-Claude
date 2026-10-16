"""Quote locators remain available for reference tools, independently of screening."""
import pytest

from reportagent.services.text_evidence import locate_quote


@pytest.mark.parametrize(('raw', 'quote'), [
    ('The ﬁnal efﬁcient oﬀer remains conditional.', 'The final efficient offer remains conditional.'),
    ('The tem-\nperature must remain sufficiently small.', 'The temperature must remain sufficiently small.'),
])
def test_pdf_typesetting_normalization_retains_exact_raw_evidence(raw, quote):
    located = locate_quote(raw, quote, 'C001')
    assert located['raw_quote'] == raw
    assert raw[located['raw_start']:located['raw_end']] == raw


def test_cross_chunk_quote_is_located_without_losing_boundary():
    quote = 'The temperature must remain sufficiently small for convexity.'
    located = locate_quote('x' * 3980 + quote, quote, 'C009')
    assert located['chunk_ids'] == ['C001', 'C002']
    assert located['raw_start'] == 3980
    assert located['raw_quote'] == quote


@pytest.mark.parametrize('bad_quote', [
    'The method works. It applies universally.',
    'The temperature can be arbitrarily large.',
    'For all x, x2 = x^2 and α = a.',
])
def test_locator_does_not_accept_omitted_conditions_or_changed_math(bad_quote):
    text = ('The method works. Only for small temperatures. It applies universally.\n'
            'The temperature must be sufficiently small.\nFor all x, x² = x^2 and α = α.')
    assert locate_quote(text, bad_quote, 'C001') is None


def test_letter_names_in_line_broken_equations_are_not_dehyphenated():
    assert locate_quote('For this case alpha-\nbeta = 0.', 'For this case alphabeta = 0.') is None
