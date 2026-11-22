"""subtitle-review-queue 引擎：登记、领取、处置与状态，全在账本内完成。

请求处理顺序固定为：先做结构校验，再打开账本查重——包含 ``request_id``
的请求按规范身份（键序不计）命中就原样返回最初回执，先于任何当前状态
判断；不同请求使用同一 ``request_id`` 报错。未命中才在最新状态上计算业务
变更。请求身份、可重取的回执、请求 ID 占用和全部组变更在同一份账本快照
里一次提交。报告只负责交付回执，不是事务标志。
"""
import copy

from . import store
from .model import (validate_issue, validate_register, validate_resolve,
                    validate_status)
from .queue import registration_state

STATE_TEXT = {"pending": "pending", "assigned": "assigned",
              "completed": "completed"}


# ---------------------------------------------------------------------------
# 回执
# ---------------------------------------------------------------------------

def _member_view(member):
    return copy.deepcopy(member)


def _group_view(group_id, member_indexes, members):
    return {
        "group_id": copy.deepcopy(group_id),
        "member_count": len(member_indexes),
        "members": [_member_view(members[index]) for index in member_indexes],
    }


def _ticket_view(ticket, state):
    members = state["members"]
    ledger_groups = {tuple(group["group_id"]): group
                     for group in state["groups"]}
    views = []
    for row in sorted(ticket["groups"], key=lambda item: item["position"]):
        group = ledger_groups[tuple(row["group_id"])]
        views.append(_group_view(row["group_id"],
                                 group["member_indexes"], members))
    return {
        "schema_version": 1,
        "mode": "subtitle-review-queue",
        "op": "issue",
        "request_id": ticket["ticket_id"],
        "ticket_id": ticket["ticket_id"],
        "reviewer": ticket["reviewer"],
        "max_fragments": ticket["max_fragments"],
        "fragment_count": ticket["fragment_count"],
        "groups": views,
        "empty": False,
    }


def _empty_receipt(request_id, reviewer, capacity):
    return {
        "schema_version": 1,
        "mode": "subtitle-review-queue",
        "op": "issue",
        "request_id": request_id,
        "ticket_id": None,
        "reviewer": reviewer,
        "max_fragments": capacity,
        "fragment_count": 0,
        "groups": [],
        "empty": True,
    }


def _resolve_receipt(request_id, ticket, action):
    return {
        "schema_version": 1,
        "mode": "subtitle-review-queue",
        "op": "resolve",
        "request_id": request_id,
        "ticket_id": ticket["ticket_id"],
        "action": action,
        "groups": [{"group_id": copy.deepcopy(row["group_id"]),
                    "member_count": row["member_count"]}
                   for row in sorted(ticket["groups"],
                                     key=lambda row: row["position"])],
    }


def _status_report(state):
    rows = []
    for group in state["groups"]:
        row = {
            "group_id": copy.deepcopy(group["group_id"]),
            "member_count": len(group["member_indexes"]),
            "state": STATE_TEXT[group["state"]],
            "ticket_id": None,
            "reviewer": None,
        }
        if group["state"] == "assigned" and group["assignment"] is not None:
            row["ticket_id"] = group["assignment"]["ticket_id"]
            row["reviewer"] = group["assignment"]["reviewer"]
        rows.append(row)
    return {
        "schema_version": 1,
        "mode": "subtitle-review-queue",
        "op": "status",
        "group_count": len(rows),
        "groups": rows,
    }


def _register_receipt(state):
    return copy.deepcopy(state["register_receipt"])


def _register_receipt_of(state):
    """对刚登记的账本现算一次登记回执。"""
    members = state["members"]
    return {
        "schema_version": 1,
        "mode": "subtitle-review-queue",
        "op": "register",
        "group_count": len(state["groups"]),
        "groups": [{"group_id": copy.deepcopy(group["group_id"]),
                    "member_count": len(group["member_indexes"]),
                    "members": [_member_view(members[index])
                                for index in group["member_indexes"]]}
                   for group in state["groups"]],
    }


# ---------------------------------------------------------------------------
# 请求身份与提交
# ---------------------------------------------------------------------------

def _request_key(data):
    """规范请求身份：全部字段纳入，键序不计。"""
    return store.canonical(data)


def _request_ids(state):
    """账本里的请求 ID 占用表：request_id -> 规范请求身份。"""
    return state.setdefault("request_ids", {})


def _replay_or_reject(state, data, request_id):
    """先于当前状态判断的重取；同一 ID 不同请求则报错。"""
    key = _request_key(data)
    owner = _request_ids(state).get(request_id)
    if owner is None:
        return None
    if owner != key:
        raise ValueError(
            f"request_id {request_id!r} was already used by another request")
    receipt = state["requests"].get(key)
    if receipt is None:
        # 占用表与回执必须一起提交，正常账本不会出现这种断裂
        raise ValueError(f"request_id {request_id!r} has no stored receipt")
    return copy.deepcopy(receipt)


def _commit(state, data, request_id, receipt):
    key = _request_key(data)
    _request_ids(state)[request_id] = key
    state["requests"][key] = copy.deepcopy(receipt)


def _register_key(state):
    """登记请求的规范身份；账本里只有一份登记。"""
    return state["register_key"]


def _register_commit(state, data, receipt):
    # 登记没有 request_id；把规范输入存为身份键，回执单独存一份，重送
    # 同一份登记输入时原样返回，不重复计算普通对版。
    state["register_key"] = _request_key(data)
    state["register_receipt"] = copy.deepcopy(receipt)


# ---------------------------------------------------------------------------
# 业务
# ---------------------------------------------------------------------------

def _issue(state, request_id, reviewer, capacity):
    members = state["members"]
    chosen, used = [], 0
    for position, group in enumerate(state["groups"]):  # 登记顺序
        size = len(group["member_indexes"])
        if group["state"] != "pending" or size > capacity - used:
            continue  # 放不进剩余容量的整组跳过，不拆组
        chosen.append((position, group))
        used += size

    if not chosen:
        # 无组可领：记住空回执（账本确有变更，必须提交以便日后原样重取），
        # 但不产生可处置工单。
        receipt = _empty_receipt(request_id, reviewer, capacity)
        return receipt, True

    ticket = {
        "ticket_id": request_id,
        "reviewer": reviewer,
        "max_fragments": capacity,
        "fragment_count": used,
        "closed": False,
        # group_id 在账本内唯一；连同登记位置一起固定领取时的顺序。
        "groups": [{"group_id": copy.deepcopy(group["group_id"]),
                    "member_count": len(group["member_indexes"]),
                    "position": position}
                   for position, group in chosen],
    }
    state["tickets"][request_id] = ticket
    for _position, group in chosen:
        group["state"] = "assigned"
        group["assignment"] = {"ticket_id": request_id,
                               "reviewer": reviewer}
        group["generation"] += 1
    return _ticket_view(ticket, state), True


def _resolve(state, request_id, ticket_id, action):
    ticket = state["tickets"].get(ticket_id)
    if ticket is None or ticket.get("closed"):
        raise ValueError(f"unknown or closed ticket: {ticket_id}")

    # 只处置当前仍归属于该有效工单的组。若这些组已被退回并重新派出，
    # 它们现在属于别的工单，旧工单不能处置新归属。
    affected = []
    for group in state["groups"]:
        assignment = group["assignment"]
        if assignment is not None and assignment["ticket_id"] == ticket_id:
            affected.append(group)
    if not affected:
        raise ValueError(f"ticket has no current groups: {ticket_id}")

    ticket["closed"] = True
    for group in affected:
        if action == "complete":
            group["state"] = "completed"
        else:
            group["state"] = "pending"
        group["assignment"] = None
        # 旧工单已关闭；组再次派出会换新工单。旧回执只用于重取，不再
        # 代表当前归属，内部的 generation 也随之推进。
        group["generation"] += 1
    return _resolve_receipt(request_id, ticket, action)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _load_or_missing(path):
    try:
        return store.read(path)
    except store.MissingLedger:
        return None


def _run_issue(state, data):
    request_id, reviewer, capacity = validate_issue(data)
    receipt = _replay_or_reject(state, data, request_id)
    if receipt is not None:
        return receipt, False
    receipt, changed = _issue(state, request_id, reviewer, capacity)
    _commit(state, data, request_id, receipt)
    return receipt, changed


def _run_resolve(state, data):
    request_id, ticket_id, action = validate_resolve(data)
    receipt = _replay_or_reject(state, data, request_id)
    if receipt is not None:
        return receipt, False
    receipt = _resolve(state, request_id, ticket_id, action)
    _commit(state, data, request_id, receipt)
    return receipt, True


def run(data, state_path):
    """执行一次请求，返回 (报告, 是否已提交账本变更)。"""
    op = data.get("op")
    if not isinstance(op, str):
        raise ValueError("op requires a nonempty string")

    if op == "register":
        conform_data = validate_register(data)
        with store.locked(state_path, exclusive=True):
            existing = _load_or_missing(state_path)
            if existing is not None:
                # 登记请求不含 request_id；同一份登记输入重送返回原回执，
                # 不同输入一律拒绝，已锁定的内容不能被替换。
                key = _register_key(existing)
                if key == _request_key(data):
                    return _register_receipt(existing), False
                raise ValueError("state ledger already registered")
            state = registration_state(conform_data)
            receipt = _register_receipt_of(state)
            _register_commit(state, data, receipt)
            store.write(state_path, state)
            return receipt, True

    if op == "status":
        validate_status(data)
        with store.locked(state_path, exclusive=False):
            return _status_report(store.read(state_path)), False

    if op == "issue":
        with store.locked(state_path, exclusive=True):
            state = store.read(state_path)
            receipt, changed = _run_issue(state, data)
            if changed:
                store.write(state_path, state)
            return receipt, changed

    if op == "resolve":
        with store.locked(state_path, exclusive=True):
            state = store.read(state_path)
            receipt, changed = _run_resolve(state, data)
            if changed:
                store.write(state_path, state)
            return receipt, changed

    raise ValueError(f"unknown op: {op!r}")
