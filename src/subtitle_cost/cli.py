"""subtitle-cost 离线命令行入口。"""
import argparse
import json

from subtitle_conform.io import load, publish

from .cost import run


def main():
    parser = argparse.ArgumentParser(
        description="跨节目字幕译制费按实际源用量分账")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        data = load(args.source)
        result = run(data)
        publish(args.output, result)
        if result["op"] == "settle":
            print(json.dumps(result["summary"], ensure_ascii=False,
                             sort_keys=True))
        else:
            print(json.dumps(
                {"op": result["op"],
                 "program_count": len(result["programs"]),
                 "withdrawn_program_count": sum(
                     1 for row in result["programs"] if row["withdrawn"])},
                ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"subtitle-cost: {exc}\n")


if __name__ == "__main__":
    main()
