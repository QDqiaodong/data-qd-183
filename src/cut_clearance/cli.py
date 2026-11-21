"""cut-clearance 离线命令行入口。"""
import argparse
import json

from subtitle_conform.io import load, publish

from .clearance import run_clearance


def main():
    parser = argparse.ArgumentParser(
        description="剪辑发布素材授权核验")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        data = load(args.source)
        result = run_clearance(data)
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False,
                         sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"cut-clearance: {exc}\n")


if __name__ == "__main__":
    main()
