"""The clip score is a transparent, deterministic ranking aid; these tests pin its weights and wording."""
import pytest

from clippie import clip_scoring as scoring


def judged(**scores):
    return {name: {'score': scores.get(name, 3), 'evidence': f'{name} note.'} for name in scoring.COMPONENTS}


RANGE = (30.0, 60.0)


def test_weights_are_fixed_and_sum_to_one():
    assert scoring.WEIGHTS == {'hook': 0.30, 'standalone': 0.20, 'payoff': 0.20, 'density': 0.15, 'shareability': 0.15}
    assert sum(scoring.WEIGHTS.values()) == pytest.approx(1.0)
    assert scoring.COMPONENT_SHARE == 0.85 and scoring.SCORE_VERSION == 2


def test_bounds_and_each_weight_moves_the_score_by_its_share():
    flat = lambda value: scoring.score(judged(**{n: value for n in scoring.COMPONENTS}), 45, RANGE)['score']
    assert flat(5) == 100.0 and flat(1) == 15.0 and flat(3) == pytest.approx(57.5)
    low = {n: 1 for n in scoring.COMPONENTS}
    for name, weight in scoring.WEIGHTS.items():
        raised = scoring.score(judged(**{**low, name: 5}), 45, RANGE)['score']
        assert raised == pytest.approx(15 + 85 * weight, abs=0.06)


def test_score_is_deterministic_and_never_a_prediction():
    first = scoring.score(judged(hook=5, payoff=4), 42, RANGE)
    assert first == scoring.score(judged(hook=5, payoff=4), 42, RANGE)
    assert first['score_version'] == 2
    assert not any('viral' in reason.lower() or 'predict' in reason.lower() for reason in first['reasons'])


@pytest.mark.parametrize('duration,fit', [(45, 1.0), (30, 1.0), (60, 1.0), (15, 0.0), (22.5, 0.5), (90, 0.5), (120, 0.0), (0, 0.0)])
def test_duration_fit_is_one_inside_the_range_and_decays_outside(duration, fit):
    assert scoring.duration_fit(duration, RANGE) == pytest.approx(fit)


def test_duration_term_is_fifteen_percent():
    inside = scoring.score(judged(), 45, RANGE)['score']
    outside = scoring.score(judged(), 120, RANGE)['score']
    assert inside - outside == pytest.approx(15.0)


def test_reasons_name_strengths_then_the_main_weakness_in_plain_language():
    result = scoring.score({'hook': {'score': 5, 'evidence': 'Opens on a bold claim.'},
                            'standalone': {'score': 4, 'evidence': 'Self-contained.'},
                            'payoff': {'score': 4, 'evidence': 'Ends on the answer.'},
                            'density': {'score': 2, 'evidence': 'Two long pauses.'},
                            'shareability': {'score': 3, 'evidence': 'Niche topic.'}}, 45, RANGE)
    assert result['reasons'] == ['Strong opening: Opens on a bold claim', 'Makes sense on its own: Self-contained',
                                 'Clear ending: Ends on the answer', 'Some slack or rambling: Two long pauses']


def test_reasons_flag_length_when_nothing_else_is_weak():
    result = scoring.score(judged(**{n: 5 for n in scoring.COMPONENTS}), 12, RANGE)
    assert len(result['reasons']) == 4 and 'Runs short' in result['reasons'][-1] and '30-60 s' in result['reasons'][-1]
    assert not any('Runs' in r for r in scoring.score(judged(**{n: 5 for n in scoring.COMPONENTS}), 45, RANGE)['reasons'])


@pytest.mark.parametrize('bad', [{}, {'hook': {'score': 5}}, judged(hook=6), judged(hook=0), {**judged(), 'hook': {'score': True}},
                                 {**judged(), 'hook': {'score': 4.5}}])
def test_invalid_components_are_rejected(bad):
    with pytest.raises(ValueError):
        scoring.score(bad, 45, RANGE)


def test_preferred_range_follows_project_limits():
    assert scoring.preferred_range(60) == (20.0, 45.0)
    assert scoring.preferred_range(30) == (15.0, 30.0)
    assert scoring.preferred_range(180, 40) == (30.0, 50.0)
    assert scoring.preferred_range(35, 40) == (30.0, 35.0)
