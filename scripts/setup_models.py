#!/usr/bin/env python3
"""Prepare the three local models for a source installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clippie.model_setup import ModelSetup, SetupError
from clippie.paths import data_root


def main():
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    setup = ModelSetup(root)
    for model in setup.models.values():
        print(f"\n{model['label']}\nSource: {model['source_url']}\nTerms: {model['terms_url']}")
    if input('\nReview the terms above. Download and prepare these models? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('No models downloaded.')
        return 1
    for ident in setup.models:
        try:
            setup.start(ident, accept_terms=True)
            while setup.thread and setup.thread.is_alive():
                setup.thread.join(timeout=2)
                state = setup.snapshot()
                model = next(item for item in state['models'] if item['id'] == ident)
                print(f"\r{model['label']}: {model.get('message') or model['state']}    ", end='', flush=True)
            model = next(item for item in setup.snapshot()['models'] if item['id'] == ident)
            print(f"\n{model['label']}: {model['state']}")
            if not model['ready']:
                print(model.get('error') or 'Preparation failed; run this command again to retry.', file=sys.stderr)
                return 1
        except (SetupError, KeyboardInterrupt) as error:
            setup.shutdown()
            print(f'\nSetup stopped: {error}', file=sys.stderr)
            return 1
    print('\nModels are ready. Run: uv run --no-sync clippie')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
