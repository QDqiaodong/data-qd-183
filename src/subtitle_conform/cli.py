import argparse
import json
from .io import load, publish
from .service import run


def main():
    parser = argparse.ArgumentParser(description="剪辑字幕对版")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"subtitle-conform: {exc}\n")


if __name__ == "__main__":
    main()
