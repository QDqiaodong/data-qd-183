import argparse
import json
from .io import load, publish
from .service import run
from .broadcast import run_broadcast


def main():
    parser = argparse.ArgumentParser(description="剪辑字幕对版")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--broadcast", action="store_true",
                        help="播出端单轨字幕对版模式")
    args = parser.parse_args()
    try:
        data = load(args.source)
        result = run_broadcast(data) if args.broadcast else run(data)
        publish(args.output, result)
        if args.broadcast:
            print(json.dumps({"status": result["status"],
                              "entries": result["scores"]["entry_count"],
                              "must_keep_covered":
                                  result["scores"]["must_keep_fragments_covered"],
                              "must_keep_total":
                                  result["scores"]["must_keep_fragments_total"]},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"subtitle-conform: {exc}\n")


if __name__ == "__main__":
    main()
