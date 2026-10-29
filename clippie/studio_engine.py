"""Adapter over the timeline/render/analyzer modules owned by other streams.

Every import is lazy so the app starts when a component is not installed; callers then get
`EngineUnavailable`, which the Studio API turns into a 503.
"""
import importlib

MODULES = {
    'timeline_v3': 'clippie.timeline_v3',
    'render_v3': 'clippie.render_v3',
    'caption_templates': 'clippie.caption_templates',
    'fillers': 'clippie.analyzers.fillers',
    'reframe': 'clippie.reframe',
}


class EngineUnavailable(RuntimeError):
    """A required engine component is missing or cannot run."""


class InvalidTimeline(ValueError):
    """A timeline failed `timeline_v3.validate`; `path` locates the problem."""

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


def module(name):
    try:
        return importlib.import_module(MODULES[name])
    except ImportError as error:
        raise EngineUnavailable(f'engine component not installed: {name}') from error


def available(name):
    try:
        module(name)
    except EngineUnavailable:
        return False
    return True


def _unavailable(error):
    # Analyzers raise AnalyzerUnavailable when a model file or optional dependency is missing.
    return type(error).__name__ == 'AnalyzerUnavailable'


def _call(name, function, *args, **kwargs):
    target = module(name)
    try:
        return getattr(target, function)(*args, **kwargs)
    except Exception as error:
        if _unavailable(error):
            raise EngineUnavailable(str(error) or 'engine component unavailable') from error
        raise


def validate(timeline, sources):
    """Canonical deep copy of a v3 timeline; raises InvalidTimeline."""
    target = module('timeline_v3')
    try:
        return target.validate(timeline, sources)
    except target.TimelineError as error:
        path = getattr(error, 'path', None)
        raise InvalidTimeline(f'{path}: {error}' if path else str(error), path) from error


def from_v2(v2_timeline, sources):
    target = module('timeline_v3')
    try:
        return target.from_v2(v2_timeline, sources)
    except target.TimelineError as error:
        raise InvalidTimeline(str(error), getattr(error, 'path', None)) from error


def program_words(timeline, sources):
    return _call('timeline_v3', 'program_words', timeline, sources)


def render(timeline, sources, directory, **options):
    return _call('render_v3', 'render', timeline, sources, directory, **options)


def load_templates():
    return _call('caption_templates', 'load')


def resolve_caption(template_id, overrides):
    return _call('caption_templates', 'resolve', template_id, overrides)


def detect_fillers(words, **options):
    return _call('fillers', 'detect', words, **options)


def auto_shots(timeline, sources, **options):
    return _call('reframe', 'auto_shots', timeline, sources, **options)


def kept_ranges(segment):
    """Source ranges of a segment that survive its cuts."""
    position, ranges = segment['start'], []
    for cut in segment.get('cuts') or []:
        if cut['start'] > position:
            ranges.append((position, cut['start']))
        position = max(position, cut['end'])
    if segment['end'] > position:
        ranges.append((position, segment['end']))
    return ranges


def duration(timeline):
    """Program duration of a v3 timeline: segments minus cuts."""
    return round(sum(end - start for segment in timeline.get('segments', [])
                     for start, end in kept_ranges(segment)), 3)
