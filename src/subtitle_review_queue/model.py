"""请求校验：严格字段、非空标签、布尔不得冒充整数。"""
from subtitle_conform.io import fields, integer, label

MAX_FRAGMENTS = 2000
ACTIONS = ("complete", "return")


def request_id(value, name="request_id"):
    return label(value, name)


def reviewer(value):
    return label(value, "reviewer")


def ticket_id(value):
    return label(value, "ticket_id")


def max_fragments(value):
    return integer(value, "max_fragments", 1, MAX_FRAGMENTS)


def validate_register(data):
    fields(data, ("op", "conform"))
    if data["op"] != "register":
        raise ValueError("register input requires op \"register\"")
    if not isinstance(data["conform"], dict):
        raise ValueError("conform must be an object")
    return data["conform"]


def validate_issue(data):
    fields(data, ("op", "request_id", "reviewer", "max_fragments"))
    if data["op"] != "issue":
        raise ValueError("issue input requires op \"issue\"")
    return (request_id(data["request_id"]),
            reviewer(data["reviewer"]),
            max_fragments(data["max_fragments"]))


def validate_resolve(data):
    fields(data, ("op", "request_id", "ticket_id", "action"))
    if data["op"] != "resolve":
        raise ValueError("resolve input requires op \"resolve\"")
    action = data["action"]
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("action must be \"complete\" or \"return\"")
    return request_id(data["request_id"]), ticket_id(data["ticket_id"]), action


def validate_status(data):
    fields(data, ("op",))
    if data["op"] != "status":
        raise ValueError("status input requires op \"status\"")
