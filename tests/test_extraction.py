from app.extraction import sentence_split, find_acronym_candidates, find_definition_in_text

def test_sentence_split():
    s = sentence_split("Hello world. This is a test! Acronym (ABC).")
    assert len(s) >= 3

def test_find_acronym_candidates():
    text = "We used Synthetic Aperture Radar (SAR) in the trial. The SAR images were good."
    cands = find_acronym_candidates(text)
    assert 'SAR' in cands

def test_find_definition_in_text():
    sents = sentence_split("We used Synthetic Aperture Radar (SAR) in the trial.")
    hit = find_definition_in_text('SAR', sents)
    assert hit is not None
    phrase, conf, excerpt = hit
    assert phrase.startswith('Synthetic Aperture Radar')
    assert conf > 0.8
