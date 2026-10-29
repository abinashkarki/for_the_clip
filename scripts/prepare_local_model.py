from clippie.local_models import convert_cached_small

if __name__ == '__main__':
    path = convert_cached_small()
    if path is None:
        raise SystemExit('No existing ~/.cache/whisper/small.pt found. No model was downloaded.')
    print(path)
