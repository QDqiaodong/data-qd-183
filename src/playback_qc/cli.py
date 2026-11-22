"""subtitle-playback-qc 离线命令行入口。"""
import argparse
import json

from subtitle_conform.io import load, publish

from .qc import run_playback_qc


def main():
    parser = argparse.ArgumentParser(
        description="离线复盘播放器字幕质检")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        data = load(args.source)
        result = run_playback_qc(data)
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False,
                         sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"subtitle-playback-qc: {exc}\n")


if __name__ == "__main__":
    main()
