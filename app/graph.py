"""Branch script graph: validation, traversal and waypoint route planning."""
from __future__ import annotations

from collections import deque
from typing import Any


# ---- validation -----------------------------------------------------------

def validate(content: dict[str, Any]) -> dict[str, list]:
    """Return validation markers for a script.

    Markers:
      cycles  : ids of nodes that lie on / lead into a directed cycle
      orphans : ids of nodes unreachable from the start node
      uncovered: node ids that have no options and no explicit terminal flag,
                 or an option whose target is missing/empty
    Missing-target options are additionally reported under `errors`.
    """
    nodes: list[dict] = content.get("nodes") or []
    start_id = content.get("start_node_id")
    node_ids = [n.get("id") for n in nodes]
    node_map = {n.get("id"): n for n in nodes}

    adjacency: dict[Any, list[Any]] = {nid: [] for nid in node_ids}
    dangling_options: list[dict] = []  # {"node_id", "option_id"}

    for node in nodes:
        for opt in node.get("options", []) or []:
            target = opt.get("target_id")
            if target and target in node_map:
                adjacency[node["id"]].append(target)
            else:
                dangling_options.append({"node_id": node["id"], "option_id": opt.get("id")})

    cycles = _find_cycle_nodes(node_ids, adjacency)

    orphans: list = []
    if start_id in node_map:
        reachable = _bfs_reachable(start_id, adjacency)
        orphans = [nid for nid in node_ids if nid not in reachable]
    else:
        # No valid start: every node is effectively orphaned.
        orphans = list(node_ids)

    uncovered = [d["node_id"] for d in dangling_options]
    errors = [
        {
            "type": "missing_target",
            "node_id": d["node_id"],
            "option_id": d["option_id"],
            "message": "选项未连接到任何节点",
        }
        for d in dangling_options
    ]
    if nodes and start_id not in node_map:
        errors.append({"type": "missing_start", "message": "起始节点不存在"})

    for node in nodes:
        options = node.get("options", []) or []
        if not options and not node.get("terminal"):
            uncovered.append(node["id"])

    return {
        "cycles": sorted(set(cycles)),
        "orphans": sorted(set(orphans)),
        "uncovered": sorted(set(uncovered)),
        "errors": errors,
        "ok": not cycles and not orphans and not errors and not uncovered,
    }


def _bfs_reachable(start: Any, adjacency: dict[Any, list]) -> set:
    seen = {start}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nxt in adjacency.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _find_cycle_nodes(node_ids: list, adjacency: dict[Any, list]) -> list:
    """Nodes that belong to a directed cycle (Tarjan SCC, size>1 or self-loop)."""
    index_counter = [0]
    stack: list = []
    on_stack: set = set()
    indices: dict = {}
    lowlink: dict = {}
    cycle_nodes: set = set()

    def strongconnect(v):
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in adjacency.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                cycle_nodes.update(component)
            elif v in adjacency.get(v, []):
                cycle_nodes.add(v)

    for nid in node_ids:
        if nid not in indices:
            strongconnect(nid)
    return list(cycle_nodes)


# ---- route planning -------------------------------------------------------

def plan_route(content: dict[str, Any], waypoints: list[dict]) -> dict[str, Any]:
    """Shortest choice sequence through every waypoint option at least once.

    Starts at ``start_node_id``; each step chooses one connected option and
    counts 1. A route is complete when every waypoint option has been chosen
    at least once AND the current node is terminal — either flagged
    ``terminal`` or simply having no options. Terminal-flagged nodes allow no
    further choices. Cycles and repeated visits are permitted: the search is
    a BFS over ``(node_id, collected-waypoints bitmask)`` states, so the first
    complete route found is shortest by choice count. When no route exists the
    result is ``solvable: False`` with a reason — never a partial path.
    """
    nodes: list[dict] = content.get("nodes") or []
    node_map = {n.get("id"): n for n in nodes}
    start_id = content.get("start_node_id")
    unsolvable = {"solvable": False, "choice_count": 0, "steps": []}
    if start_id not in node_map:
        return {**unsolvable, "reason": "起始节点未设置或不存在"}

    wp_index = {(w["node_id"], w["option_id"]): i for i, w in enumerate(waypoints)}
    full = (1 << len(waypoints)) - 1

    def is_end(node: dict) -> bool:
        return bool(node.get("terminal")) or not (node.get("options") or [])

    start_state = (start_id, 0)
    parent: dict[tuple, Any] = {start_state: None}  # state -> (prev_state, choice)
    queue = deque([start_state])
    goal = None
    while queue:
        cur_id, mask = queue.popleft()
        node = node_map[cur_id]
        if mask == full and is_end(node):
            goal = (cur_id, mask)
            break
        if node.get("terminal"):
            continue  # terminal nodes cannot continue
        for opt in node.get("options", []) or []:
            target = opt.get("target_id")
            if not target or target not in node_map:
                continue  # unconnected options are not selectable
            bit = wp_index.get((cur_id, opt.get("id")))
            nxt = (target, mask | (1 << bit) if bit is not None else mask)
            if nxt not in parent:
                parent[nxt] = ((cur_id, mask), (cur_id, opt.get("id"), target))
                queue.append(nxt)

    if goal is None:
        return {**unsolvable,
                "reason": "从起始节点出发，不存在经过全部勾选选项并到达终止节点（含无选项节点）的路线"}

    choices = []
    state = goal
    while parent[state] is not None:
        prev, choice = parent[state]
        choices.append(choice)
        state = prev
    choices.reverse()

    steps = []
    for node_id, option_id, target_id in choices:
        node = node_map[node_id]
        opt = next((o for o in node.get("options", []) if o.get("id") == option_id), {})
        target = node_map[target_id]
        steps.append({
            "node_id": node_id,
            "title": node.get("title", ""),
            "option_id": option_id,
            "option_label": opt.get("label", ""),
            "target_id": target_id,
            "target_title": target.get("title", ""),
            "waypoint": (node_id, option_id) in wp_index,
        })
    end_node = node_map[goal[0]]
    return {
        "solvable": True,
        "reason": None,
        "choice_count": len(steps),
        "steps": steps,
        "start_node_id": start_id,
        "start_title": node_map[start_id].get("title", ""),
        "end_node_id": goal[0],
        "end_title": end_node.get("title", ""),
        "end_terminal": bool(end_node.get("terminal")),
    }


# ---- revision impact replay -----------------------------------------------

def _display_title(node_map: dict, node_id: Any, fallback: str = "") -> str:
    """Human-friendly node name: current canvas title, else a recorded fallback."""
    node = node_map.get(node_id) if node_id is not None else None
    if node is not None:
        return node.get("title") or str(node_id)
    return fallback or (str(node_id) if node_id is not None else "—")


def replay_run(content: dict[str, Any], steps: list[dict] | None,
               finished: bool) -> dict[str, Any]:
    """Replay one recorded answer session against the CURRENT canvas content.

    Replay starts at the canvas start node and follows the recorded choices in
    order, comparing each jump target with the recorded path. Only the FIRST
    breakpoint is reported. Detected breakpoints:

      start_changed               : canvas start no longer matches the answer's
      node_missing (at/after start): a node the answer reached (or started at)
                                    no longer exists
      option_missing              : the chosen option disappeared from its node
      redirected                  : the option now jumps somewhere else (or is
                                    unconnected), though the recorded target
                                    still exists
      early_terminal              : a node mid-answer is now an end (terminal
                                    flag or no options) while more answers exist
      ended_no_longer_terminal    : a finished answer's last node is no longer
                                    an end on the current canvas

    Unfinished answers are only checked over their answered part: an exact
    replay is reported as ``compatible`` even when the paused-at node has since
    become terminal. Nothing here mutates anything — pure read-only analysis.
    """
    node_map = {n.get("id"): n for n in content.get("nodes", []) or []}
    steps = list(steps or [])
    choice_count = sum(1 for s in steps if s.get("option_id"))

    def is_end(node: dict) -> bool:
        return bool(node.get("terminal")) or not (node.get("options") or [])

    def verdict(reason: str, message: str, *, step_index, choice_index,
                node_id, locate_node_id=None, option_id=None, detail=None):
        locate = locate_node_id if locate_node_id in node_map else None
        return {"status": "affected", "choice_count": choice_count,
                "breakpoint": {
                    "reason": reason,
                    "message": message,
                    "step_index": step_index,      # 0-based index into steps
                    "choice_index": choice_index,  # 1-based choice ordinal, may be None
                    "node_id": node_id,
                    "option_id": option_id,
                    "locate_node_id": locate,      # existing canvas node to focus
                    "detail": detail or {},
                }}

    if not steps:
        return {"status": "affected", "choice_count": 0, "breakpoint": {
            "reason": "empty_record", "message": "答卷记录为空，无法重放核对。",
            "step_index": None, "choice_index": None, "node_id": None,
            "option_id": None, "locate_node_id": None, "detail": {}}}

    recorded_start = steps[0].get("node_id")
    recorded_start_title = steps[0].get("title") or str(recorded_start)
    canvas_start = content.get("start_node_id")

    if canvas_start not in node_map:
        # Canvas has no valid entry point at all.
        if recorded_start in node_map:
            return verdict(
                "start_changed",
                f"起点已改变：答卷从「{_display_title(node_map, recorded_start, recorded_start_title)}」开始，"
                f"但当前画布未设置有效的起始节点。",
                step_index=0, choice_index=None, node_id=recorded_start,
                locate_node_id=recorded_start,
                detail={"current_start_node_id": canvas_start, "current_start_title": "",
                        "recorded_start_node_id": recorded_start,
                        "recorded_start_title": recorded_start_title})
        return verdict(
            "node_missing",
            f"答卷的起始节点「{recorded_start_title}」已从画布消失，且当前画布未设置有效的起始节点。",
            step_index=0, choice_index=None, node_id=recorded_start,
            detail={"missing_node_id": recorded_start, "recorded_title": recorded_start_title})

    if canvas_start != recorded_start:
        return verdict(
            "start_changed",
            f"起点已改变：答卷从「{recorded_start_title}」开始，当前画布起点为"
            f"「{_display_title(node_map, canvas_start)}」。",
            step_index=0, choice_index=None, node_id=canvas_start,
            locate_node_id=canvas_start,
            detail={"current_start_node_id": canvas_start,
                    "current_start_title": _display_title(node_map, canvas_start),
                    "recorded_start_node_id": recorded_start,
                    "recorded_start_title": recorded_start_title})

    cur = node_map[canvas_start]
    answered = 0
    for i, step in enumerate(steps):
        option_id = step.get("option_id")
        if not option_id:
            continue  # start node and mid-restart entries carry no choice
        answered += 1
        n = answered
        next_step = steps[i + 1] if i + 1 < len(steps) else None
        recorded_next = next_step.get("node_id") if next_step else None
        node_desc = _display_title(node_map, cur["id"])
        answer_desc = step.get("answer") or option_id

        # 1. answer path continues, but the canvas flow stops here already
        if is_end(cur):
            return verdict(
                "early_terminal",
                f"第 {n} 次选择所在节点「{node_desc}」现在会提前结束"
                f"（已标记为终点或没有任何选项），答卷在此被截断，后续作答无法继续。",
                step_index=i, choice_index=n, node_id=cur["id"],
                locate_node_id=cur["id"], option_id=step.get("option_id"),
                detail={"node_title": node_desc, "option_answer": step.get("answer")})

        # 2. the chosen option no longer exists on the node
        opt = next((o for o in (cur.get("options") or [])
                    if o.get("id") == step.get("option_id")), None)
        if opt is None:
            return verdict(
                "option_missing",
                f"第 {n} 次选择的选项「{answer_desc}」已从节点「{node_desc}」删除，答卷路径无法继续。",
                step_index=i, choice_index=n, node_id=cur["id"],
                locate_node_id=cur["id"], option_id=step.get("option_id"),
                detail={"node_title": node_desc, "option_answer": step.get("answer")})

        # 3. the node the answer arrived at vanished from the canvas
        if recorded_next not in node_map:
            gone_title = (next_step.get("title") if next_step else None) or str(recorded_next)
            return verdict(
                "node_missing",
                f"第 {n} 次选择后到达的节点「{gone_title}」已从画布消失，答卷路径无法继续。",
                step_index=i + 1 if next_step else i, choice_index=n,
                node_id=recorded_next, locate_node_id=cur["id"],
                option_id=opt.get("id"),
                detail={"missing_node_id": recorded_next, "recorded_title": gone_title,
                        "source_node_id": cur["id"], "source_node_title": node_desc})

        # 4. option still exists, recorded node still exists, but the jump moved
        target = opt.get("target_id")
        if target != recorded_next:
            old_desc = _display_title(node_map, recorded_next,
                                      next_step.get("title") if next_step else "")
            if not target:
                new_desc = "未连接任何节点"
            elif target in node_map:
                new_desc = f"改指到「{_display_title(node_map, target)}」"
            else:
                new_desc = f"指向已消失的节点 {target}"
            return verdict(
                "redirected",
                f"第 {n} 次选择的选项「{opt.get('label') or answer_desc}」跳转已改指："
                f"原来到达「{old_desc}」，现在{new_desc}。",
                step_index=i, choice_index=n, node_id=cur["id"],
                locate_node_id=cur["id"], option_id=opt.get("id"),
                detail={"option_label": opt.get("label", ""),
                        "recorded_target_id": recorded_next, "recorded_target_title": old_desc,
                        "current_target_id": target,
                        "current_target_title": _display_title(node_map, target) if target in node_map else "",
                        "current_connected": bool(target and target in node_map)})

        cur = node_map[recorded_next]

    # 5. a finished answer must still end at its final node
    if finished and not is_end(cur):
        return verdict(
            "ended_no_longer_terminal",
            f"该答卷原在节点「{_display_title(node_map, cur['id'])}」结束，"
            f"但该节点现在不再是终点（仍有可继续选择的分支）。",
            step_index=len(steps) - 1, choice_index=None, node_id=cur["id"],
            locate_node_id=cur["id"],
            detail={"node_title": _display_title(node_map, cur["id"])})

    return {"status": "compatible", "choice_count": choice_count, "breakpoint": None}


# ---- runtime traversal ----------------------------------------------------

def step(content: dict, node_id: str, option_id: str | None = None) -> dict[str, Any]:
    """Resolve the next node from `node_id` via `option_id`.

    option_id None means "start": resolve from start_node_id.
    Returns {node, finished} or raises ValueError.
    """
    node_map = {n["id"]: n for n in content.get("nodes", [])}
    if option_id is None:
        start = content.get("start_node_id")
        if start not in node_map:
            raise ValueError("起始节点不存在")
        return {"node": node_map[start], "finished": False}

    current = node_map.get(node_id)
    if current is None:
        raise ValueError("当前节点不存在")
    opt = next((o for o in current.get("options", []) if o.get("id") == option_id), None)
    if opt is None:
        raise ValueError("该选项不存在")
    target = opt.get("target_id")
    if not target or target not in node_map:
        raise ValueError("选项未连接到目标节点")
    return {"node": node_map[target], "finished": False}
