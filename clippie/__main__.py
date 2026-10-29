import argparse
import uvicorn


def main():
    from clippie.desktop import instance_lock
    from clippie.paths import data_root
    from clippie.storage_safety import assert_compatible
    parser = argparse.ArgumentParser(description='Run Clippie locally')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--reload', action='store_true', help='Restart automatically when Clippie code changes')
    args = parser.parse_args()
    with instance_lock(data_root()):
        assert_compatible(data_root())
        serve(args)


def serve(args):
    if args.reload:
        from pathlib import Path
        package = Path(__file__).resolve().parent
        uvicorn.run('clippie.app:app', host='127.0.0.1', port=args.port, reload=True, reload_dirs=[str(package)],
                    reload_excludes=['static/*', '*.json'])
    else:
        uvicorn.run('clippie.app:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
