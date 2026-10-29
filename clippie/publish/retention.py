"""Map an audience retention curve onto a clip's program words so the UI can show where viewers left."""

DROP_FLOOR = 0.01  # a word must lose at least 1 point of the audience to count as a drop-off
DROP_FACTOR = 1.5  # and at least this multiple of the average per-word loss


def _at(points, ratio, column):
    """Linear interpolation of a retention column at `ratio`; None when the column is missing there."""
    if not points:
        return None
    if ratio <= points[0][0]:
        return points[0][column]
    for before, after in zip(points, points[1:]):
        if ratio <= after[0]:
            low, high = before[column], after[column]
            if low is None or high is None:
                return low if high is None else high
            span = after[0] - before[0]
            return low + (high - low) * ((ratio - before[0]) / span if span else 0.0)
    return points[-1][column]


def watch_ratio_at(points, ratio):
    return _at(points, ratio, 1)


def map_words(points, words, duration):
    """Per-word retention for `words` (program times: `start`, `end`, `text`) of a clip lasting `duration` seconds.

    Returns {'duration', 'words': [{index, text, start, end, ratio, watch_ratio, relative, loss}], 'drops': [index]}.
    `loss` is the share of the starting audience that left while the word was on screen.
    """
    if not points or not words or not duration or duration <= 0:
        return {'duration': duration, 'words': [], 'drops': []}
    rows = []
    for index, word in enumerate(words):
        start, end = float(word['start']), float(word['end'])
        first, last = min(start / duration, 1.0), min(end / duration, 1.0)
        entering, leaving = watch_ratio_at(points, first), watch_ratio_at(points, last)
        middle = (first + last) / 2
        rows.append({'index': index, 'text': word['text'], 'start': round(start, 3), 'end': round(end, 3),
                     'ratio': round(first, 4), 'watch_ratio': round(_at(points, middle, 1), 4),
                     'relative': None if _at(points, middle, 2) is None else round(_at(points, middle, 2), 4),
                     'loss': round(max(entering - leaving, 0.0), 4)})
    average = sum(r['loss'] for r in rows) / len(rows)
    floor = max(DROP_FLOOR, DROP_FACTOR * average)
    return {'duration': duration, 'words': rows, 'drops': [r['index'] for r in rows if r['loss'] >= floor]}
