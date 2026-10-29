"""Compact transport references preserve exact evidence and reject scope escapes."""
import copy
import json

from clippie import collection_editor as editor
from clippie.source_evidence import prepare_evidence


def test_reference_roundtrip_keeps_qualifications_late_units_and_canonical_results(tmp_path):
    source = {'id': 's', 'duration': 40, 'segments': [
        {'start': 0, 'end': 8, 'text': 'It worked in a dish only, not in humans.'},
        {'start': 12, 'end': 20, 'text': 'The qualification must stay in the clip.'},
        {'start': 25, 'end': 35, 'text': 'This later evidence introduces a distinct question.'}]}
    evidence = prepare_evidence(source, tmp_path)
    units = evidence['artifact']['units']
    ids = [u['id'] for u in units]
    components = {name: {'score': 4, 'evidence': 'Bounded source evidence.'} for name in editor.clip_scoring.COMPONENTS}
    base = {'id': 'a', 'title': 'Qualified result', 'angle': 'Scope of the result', 'form': 'moment',
            'edit_format': 'explanation', 'hook_line': 'It worked in a dish only', 'components': components}
    recorded = []
    def invoke(stage, prompt, schema, tokens, **kwargs):
        sent = json.loads(prompt.split('\n', 1)[1]);recorded.append(sent)
        refs = [row[0] for row in sent['transcript']]
        return {'proposal': {'candidates': [{**base, 'unit_ids': refs[:2]}],
                             'rejected': [], 'duplicate_groups': [], 'explanation': 'Qualified result.'}}
    maps = [{'source_id': 's', 'topics': [{'angle': 'Scope', 'unit_ids': ids}]}]
    original = copy.deepcopy((maps, evidence))
    result = editor.discover(maps, [evidence], {'locked_unit_ids': ids[:1]}, [], invoke)
    canonical = editor.discover(maps, [evidence], {'locked_unit_ids': ids[:1]}, [], lambda *args, **kwargs: {
        'proposal': {'candidates': [{**base, 'unit_ids': ids[:2]}],
                     'rejected': [], 'duplicate_groups': [], 'explanation': 'Qualified result.'}})
    assert result == canonical
    assert result['candidates'][0]['unit_ids'] == ids[:2]
    assert result['evidence_provenance']['unit_ids'] == ids[:2]
    assert [row[1:] for row in recorded[0]['transcript']] == [
        [round(float(u['start']), 1), u['text']] for u in units]
    assert recorded[0]['constraints']['locked_unit_ids'] == ['e1_0']
    assert recorded[0]['topic_maps'][0]['topics'][0]['unit_ids'] == ['e1_0', 'e1_1', 'e1_2']
    assert (maps, evidence) == original
    assert 'e1_' not in json.dumps(result)


def test_unknown_duplicate_and_cross_window_references_never_resolve():
    ids = ['u-canonical-one', 'u-canonical-two']
    window = [[ids[0], 0, 'First sentence.'], [ids[1], 1, 'Only in a dish.']]
    compact, maps, constraints, history, refs = editor._discovery_references(window, [], {}, [], 2)
    assert [row[0] for row in compact] == ['e2_0', 'e2_1']
    part = {'candidates': [{'unit_ids': value} for value in [
        ['e1_0'], ['e2_99'], ['u-other-window'], [True], ['e2_0', ids[0]], ['e2_0', 'e2_1']]]}
    expanded = editor._expand_discovery_references(part, refs)
    assert [c['unit_ids'] for c in expanded['candidates'][:4]] == [[], [], [], []]
    assert expanded['candidates'][4]['unit_ids'] == [ids[0], ids[0]]  # Existing _refs rejects duplicates.
    assert expanded['candidates'][5]['unit_ids'] == ids
    assert part['candidates'][0]['unit_ids'] == ['e1_0']
