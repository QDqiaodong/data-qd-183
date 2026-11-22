"""subtitle-review-queue 离线命令行入口。

退出码约定：

- ``0``：账本变更（如有）已原子提交，报告已原子新建；
- ``2``：请求非法、账本不存在或业务状态不允许——账本未被改动，未写报告；
- ``3``：账本变更已经提交，但报告未能写出（常见是目标已存在或目录不可写）。
  此时不得回滚已提交的业务变更；调用方用原请求和新的 ``--output`` 路径
  重取，重取会返回最初的回执且不重复做事。
"""
import argparse
import json

from subtitle_conform.io import load, publish

from .engine import run


def main():
    parser = argparse.ArgumentParser(
        description="字幕复核工单账本：登记待复核组、领取、归还与完成")
    parser.add_argument("--state", required=True,
                        help="账本文件路径；一个账本对应一个已锁定节目版")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        data = load(args.source)
        result, committed = run(data, args.state)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"subtitle-review-queue: {exc}\n")

    try:
        publish(args.output, result)
    except OSError as exc:
        if committed:
            parser.exit(3, "subtitle-review-queue: ledger change is committed "
                           "but the report was not written; retry the original "
                           "request with a new --output path: "
                           f"{exc}\n")
        parser.exit(2, "subtitle-review-queue: report was not written and the "
                       f"ledger was not changed; retry with a new --output path: "
                       f"{exc}\n")

    if result["op"] == "status":
        summary = {"op": "status", "group_count": result["group_count"],
                   "pending": sum(1 for row in result["groups"]
                                  if row["state"] == "pending"),
                   "assigned": sum(1 for row in result["groups"]
                                   if row["state"] == "assigned"),
                   "completed": sum(1 for row in result["groups"]
                                    if row["state"] == "completed")}
    elif result["op"] == "register":
        summary = {"op": "register",
                   "group_count": result["group_count"],
                   "fragment_count": sum(row["member_count"]
                                         for row in result["groups"])}
    else:
        summary = {"op": result["op"], "ticket_id": result["ticket_id"],
                   "group_count": len(result["groups"]),
                   "empty": result.get("empty", False)}
        if result["op"] == "issue":
            summary["reviewer"] = result["reviewer"]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
