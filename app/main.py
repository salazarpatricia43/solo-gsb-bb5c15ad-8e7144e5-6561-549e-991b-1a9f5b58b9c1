"""FastAPI application: script CRUD, validation, sharing, runtime preview runs."""
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import get_db, init_db
from .graph import plan_route
from .graph import replay_run
from .graph import step as graph_step
from .graph import validate as validate_content

app = FastAPI(title="分支访谈脚本编辑器", version="1.0.0")

STATIC_DIR = "static"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- models ---------------------------------------------------------------

class OptionIn(BaseModel):
    id: str
    label: str = ""
    target_id: str | None = None


class NodeIn(BaseModel):
    id: str
    type: str = "question"          # question | info
    title: str = ""
    text: str = ""
    terminal: bool = False
    x: float = 0
    y: float = 0
    options: list[OptionIn] = Field(default_factory=list)


class ContentIn(BaseModel):
    start_node_id: str | None = None
    nodes: list[NodeIn] = Field(default_factory=list)


class ScriptIn(BaseModel):
    name: str = "未命名访谈脚本"
    content: ContentIn | None = None


class ScriptUpdate(BaseModel):
    name: str | None = None
    content: ContentIn | None = None


class RunStart(BaseModel):
    script_id: int | None = None
    share_token: str | None = None
    invite_code: str | None = None


class RunChoice(BaseModel):
    option_id: str


class ShareResume(BaseModel):
    resume_token: str


class InviteToggle(BaseModel):
    invite_only: bool


class InviteBatch(BaseModel):
    count: int = Field(ge=1, le=500)


class RouteOptionRef(BaseModel):
    node_id: str
    option_id: str


class RoutePlanIn(BaseModel):
    content: ContentIn
    options: list[RouteOptionRef] = Field(default_factory=list)


class ImpactIn(BaseModel):
    content: ContentIn


# ---- lifecycle ------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    init_db()


# ---- script CRUD ----------------------------------------------------------

@app.get("/api/scripts")
def list_scripts():
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, created_at, updated_at FROM scripts ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/scripts", status_code=201)
def create_script(payload: ScriptIn):
    content = payload.content.model_dump() if payload.content else {"start_node_id": None, "nodes": []}
    ts = now_iso()
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO scripts (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (payload.name, json.dumps(content, ensure_ascii=False), ts, ts),
        )
        sid = cur.lastrowid
    return {"id": sid, "name": payload.name, "content": content,
            "created_at": ts, "updated_at": ts}


def _fetch_script(script_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "脚本不存在")
    return {"id": row["id"], "name": row["name"], "content": json.loads(row["content"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.get("/api/scripts/{script_id}")
def get_script(script_id: int):
    return _fetch_script(script_id)


@app.put("/api/scripts/{script_id}")
def update_script(script_id: int, payload: ScriptUpdate):
    existing = _fetch_script(script_id)
    name = payload.name if payload.name is not None else existing["name"]
    content = payload.content.model_dump() if payload.content is not None else existing["content"]
    ts = now_iso()
    with get_db() as db:
        db.execute(
            "UPDATE scripts SET name = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(content, ensure_ascii=False), ts, script_id),
        )
    return {"id": script_id, "name": name, "content": content, "updated_at": ts}


@app.delete("/api/scripts/{script_id}", status_code=204)
def delete_script(script_id: int):
    _fetch_script(script_id)
    with get_db() as db:
        db.execute("DELETE FROM scripts WHERE id = ?", (script_id,))


# ---- validation -----------------------------------------------------------

@app.post("/api/scripts/{script_id}/validate")
def validate_saved_script(script_id: int):
    script = _fetch_script(script_id)
    result = validate_content(script["content"])
    return result


# ---- branch coverage ------------------------------------------------------

@app.get("/api/scripts/{script_id}/coverage")
def script_coverage(script_id: int):
    """Aggregate how often each option of the *current* script was chosen.

    Saved runs from both sources are counted:
      * editor preview sessions (runs.mode = 'script', script_id matched)
      * answer sessions created via read-only share links whose snapshot
        belongs to this script (runs.mode = 'share', joined via shares)

    Every recorded choice is a (node_id, option_id) pair. Pairs whose node or
    option no longer exists in the current content are ignored, so editing the
    script never produces statistics for vanished options. Options without a
    connected target are not selectable in a run, so they are reported but
    excluded from the coverage rate / uncovered list.
    """
    script = _fetch_script(script_id)
    content = script["content"]
    with get_db() as db:
        rows = db.execute(
            "SELECT r.mode AS mode, r.finished AS finished, r.steps AS steps "
            "FROM runs r LEFT JOIN shares s ON r.share_token = s.token "
            "WHERE (r.mode = 'script' AND r.script_id = ?) "
            "   OR (r.mode = 'share' AND s.script_id = ?)",
            (script_id, script_id),
        ).fetchall()

    counts: dict[tuple[str, str], int] = {}
    preview_runs = share_runs = finished_runs = total_choices = 0
    for row in rows:
        if row["mode"] == "share":
            share_runs += 1
        else:
            preview_runs += 1
        finished_runs += 1 if row["finished"] else 0
        for step in json.loads(row["steps"]):
            node_id, option_id = step.get("node_id"), step.get("option_id")
            # option_id is recorded on the step of the node the choice was made at
            if node_id and option_id:
                counts[(node_id, option_id)] = counts.get((node_id, option_id), 0) + 1
                total_choices += 1

    node_map = {n.get("id"): n for n in content.get("nodes", [])}
    has_runs = bool(rows)
    selectable_total = covered_total = 0
    uncovered_options: list[dict] = []
    nodes_out: list[dict] = []
    for node in content.get("nodes", []):
        options_out = []
        for opt in node.get("options", []) or []:
            target = opt.get("target_id")
            selectable = bool(target and target in node_map)
            count = counts.get((node["id"], opt.get("id")), 0)
            covered = count > 0
            if selectable:
                selectable_total += 1
                if covered:
                    covered_total += 1
                elif has_runs:
                    uncovered_options.append({"node_id": node["id"], "option_id": opt.get("id")})
            options_out.append({
                "option_id": opt.get("id"),
                "label": opt.get("label", ""),
                "target_id": target,
                "selectable": selectable,
                "count": count,
                "covered": covered,
            })
        nodes_out.append({
            "node_id": node.get("id"),
            "title": node.get("title", ""),
            "type": node.get("type", "question"),
            "terminal": bool(node.get("terminal")),
            "options": options_out,
        })

    return {
        "script_id": script_id,
        "has_runs": has_runs,
        "run_count": len(rows),
        "preview_runs": preview_runs,
        "share_runs": share_runs,
        "finished_runs": finished_runs,
        "total_choices": total_choices,
        "nodes": nodes_out,
        "option_count": selectable_total,
        "covered_option_count": covered_total,
        "uncovered_options": uncovered_options,
        "coverage_rate": round(covered_total / selectable_total, 4) if selectable_total else 0.0,
    }


# ---- route planning -------------------------------------------------------

@app.post("/api/route-plan")
def plan_route_endpoint(payload: RoutePlanIn):
    """Shortest route through the checked options — pure read-only computation.

    The posted ``content`` is the caller's current canvas (saved or not).
    Nothing is written to the database, so planning never saves the script,
    never creates run records and never affects branch coverage statistics.
    """
    refs = list(dict.fromkeys((o.node_id, o.option_id) for o in payload.options))
    if not 1 <= len(refs) <= 6:
        raise HTTPException(400, "请勾选 1～6 个已连接目标的选项")
    content = payload.content.model_dump()
    node_map = {n["id"]: n for n in content.get("nodes", [])}
    for node_id, option_id in refs:
        node = node_map.get(node_id)
        opt = next((o for o in (node or {}).get("options", [])
                    if o.get("id") == option_id), None)
        if opt is None:
            raise HTTPException(400, "勾选的选项不存在，请刷新画布后重试")
        target = opt.get("target_id")
        if not target or target not in node_map:
            raise HTTPException(400, "勾选的选项未连接目标节点")
    waypoints = [{"node_id": n, "option_id": o} for n, o in refs]
    return plan_route(content, waypoints)


# ---- revision impact analysis ---------------------------------------------

@app.post("/api/scripts/{script_id}/impact")
def script_impact(script_id: int, payload: ImpactIn):
    """Replay every recorded answer session against the posted CURRENT canvas.

    Both kinds of saved sessions belonging to this script are checked, exactly
    like coverage aggregation:
      * editor preview sessions (runs.mode = 'script', script_id matched)
      * answer sessions created via read-only share links whose snapshot
        belongs to this script (runs.mode = 'share', joined via shares)

    The posted ``content`` is the caller's current canvas — saved or carrying
    unsaved edits. For each session the server replays from the canvas start
    node through the recorded choices, verifying each jump target, and reports
    only the first breakpoint (start changed, node/option vanished, jump
    redirected, premature end, or a finished answer whose end is no longer
    terminal). Unfinished answers are only checked over their answered part; a
    full match is reported as compatible. Read-only: nothing is written, no run
    record changes and coverage statistics are never touched.
    """
    _fetch_script(script_id)  # 404 when the script itself is gone
    content = payload.content.model_dump()
    with get_db() as db:
        rows = db.execute(
            "SELECT r.id AS id, r.mode AS mode, r.finished AS finished, "
            "r.steps AS steps, r.created_at AS created_at "
            "FROM runs r LEFT JOIN shares s ON r.share_token = s.token "
            "WHERE (r.mode = 'script' AND r.script_id = ?) "
            "   OR (r.mode = 'share' AND s.script_id = ?) "
            "ORDER BY r.id DESC",
            (script_id, script_id),
        ).fetchall()

    sessions = []
    affected = compatible = 0
    preview_runs = share_runs = finished_runs = 0
    reason_counts: dict[str, int] = {}
    for row in rows:
        if row["mode"] == "share":
            share_runs += 1
        else:
            preview_runs += 1
        finished = bool(row["finished"])
        finished_runs += 1 if finished else 0
        steps = json.loads(row["steps"])
        result = replay_run(content, steps, finished)
        if result["status"] == "affected":
            affected += 1
            reason = result["breakpoint"]["reason"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            compatible += 1
        sessions.append({
            "run_id": row["id"],
            "mode": row["mode"],
            "finished": finished,
            "created_at": row["created_at"],
            "choice_count": result["choice_count"],
            "status": result["status"],
            "breakpoint": result["breakpoint"],
            "steps": steps,
        })

    return {
        "script_id": script_id,
        "has_runs": bool(rows),
        "total": len(rows),
        "compatible_count": compatible,
        "affected_count": affected,
        "preview_runs": preview_runs,
        "share_runs": share_runs,
        "finished_runs": finished_runs,
        "reason_counts": reason_counts,
        "sessions": sessions,
    }


# ---- import / export ------------------------------------------------------

@app.get("/api/scripts/{script_id}/export")
def export_script(script_id: int):
    script = _fetch_script(script_id)
    return {
        "format": "branch-interview-script",
        "version": 1,
        "name": script["name"],
        "content": script["content"],
        "exported_at": now_iso(),
    }


@app.post("/api/import")
def import_script(payload: dict[str, Any]):
    """Import a JSON document previously produced by /export (or a raw content object)."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON 顶层必须是对象")
    if "content" in payload:
        content, name = payload["content"], payload.get("name", "导入的访谈脚本")
    else:
        content, name = payload, payload.get("name", "导入的访谈脚本")
        # Raw content documents only carry start_node_id / nodes.
        if "start_node_id" not in content or "nodes" not in content:
            raise HTTPException(400, "缺少 content 或 start_node_id/nodes 字段")
    try:
        ContentIn.model_validate(content)
    except Exception as exc:
        raise HTTPException(400, f"脚本格式不正确: {exc}")
    ts = now_iso()
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO scripts (name, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (str(name), json.dumps(content, ensure_ascii=False), ts, ts),
        )
        sid = cur.lastrowid
    return {"id": sid, "name": name, "content": content, "created_at": ts}


# ---- read-only sharing ----------------------------------------------------

@app.post("/api/scripts/{script_id}/share")
def create_share(script_id: int):
    script = _fetch_script(script_id)
    token = secrets.token_urlsafe(8)
    ts = now_iso()
    with get_db() as db:
        db.execute(
            "INSERT INTO shares (token, script_id, script_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, script_id, script["name"],
             json.dumps(script["content"], ensure_ascii=False), ts),
        )
    return {"token": token, "url": f"/share.html?token={token}"}


@app.get("/api/share/{token}")
def get_share(token: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise HTTPException(404, "分享链接不存在或已失效")
    return {"token": row["token"], "name": row["script_name"],
            "content": json.loads(row["content"]), "created_at": row["created_at"],
            "invite_only": bool(row["invite_only"]), "read_only": True}


# ---- invite-only sharing ---------------------------------------------------
# An invite code belongs to exactly ONE share snapshot. When a snapshot has
# invite_only=1, starting a new answer session requires an unused code of that
# snapshot: verification, consumption and session creation happen inside one
# SQLite write transaction, so cross-snapshot, repeated or concurrent use is
# rejected without creating a session. Resuming a paused session still uses
# the one-time resume token only and never touches invite codes.

_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L lookalikes


def _new_invite_code() -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))


def _normalize_invite(code: str | None) -> str:
    """Canonical form: uppercase alphanumerics only (dashes/spaces ignored)."""
    return re.sub(r"[^0-9A-Za-z]", "", code or "").upper()


def _invite_state(db, token: str) -> dict[str, Any]:
    """Invite-only flag + used/unused counts + full code list of a snapshot."""
    share = db.execute("SELECT invite_only FROM shares WHERE token = ?", (token,)).fetchone()
    if share is None:
        raise HTTPException(404, "分享链接不存在或已失效")
    rows = db.execute(
        "SELECT code, used, used_run_id, created_at, used_at FROM invite_codes "
        "WHERE share_token = ? ORDER BY created_at DESC, code DESC",
        (token,),
    ).fetchall()
    used = sum(1 for r in rows if r["used"])
    return {
        "token": token,
        "invite_only": bool(share["invite_only"]),
        "total": len(rows),
        "used": used,
        "unused": len(rows) - used,
        "codes": [{"code": r["code"], "used": bool(r["used"]),
                   "used_run_id": r["used_run_id"],
                   "created_at": r["created_at"], "used_at": r["used_at"]}
                  for r in rows],
    }


@app.get("/api/share/{token}/invite")
def get_invites(token: str):
    with get_db() as db:
        return _invite_state(db, token)


@app.put("/api/share/{token}/invite")
def toggle_invite(token: str, payload: InviteToggle):
    with get_db() as db:
        share = db.execute("SELECT 1 FROM shares WHERE token = ?", (token,)).fetchone()
        if share is None:
            raise HTTPException(404, "分享链接不存在或已失效")
        db.execute("UPDATE shares SET invite_only = ? WHERE token = ?",
                   (int(payload.invite_only), token))
        return _invite_state(db, token)


@app.post("/api/share/{token}/invite/codes", status_code=201)
def generate_invites(token: str, payload: InviteBatch):
    """Batch-generate random one-time invite codes bound to this snapshot."""
    ts = now_iso()
    with get_db() as db:
        share = db.execute("SELECT 1 FROM shares WHERE token = ?", (token,)).fetchone()
        if share is None:
            raise HTTPException(404, "分享链接不存在或已失效")
        generated: list[str] = []
        while len(generated) < payload.count:
            code = _new_invite_code()
            try:
                db.execute(
                    "INSERT INTO invite_codes (code, share_token, used, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (code, token, ts),
                )
                generated.append(code)
            except sqlite3.IntegrityError:
                continue  # primary-key collision: retry with a fresh random code
        return {**_invite_state(db, token), "generated": generated}


def _claim_invite(db, share_token: str, code: str) -> None:
    """Atomically consume one invite code for this snapshot (single-use claim).

    The conditional UPDATE is the first write of the surrounding transaction:
    it only succeeds while the row is still unused AND bound to this very
    snapshot, so a repeated or concurrent claim affects 0 rows. The caller
    raises on failure, rolling the whole transaction back — no session row is
    ever created for a rejected code.
    """
    claimed = db.execute(
        "UPDATE invite_codes SET used = 1, used_at = ? "
        "WHERE code = ? AND share_token = ? AND used = 0",
        (now_iso(), code, share_token),
    ).rowcount
    if claimed:
        return
    row = db.execute("SELECT share_token, used FROM invite_codes WHERE code = ?",
                     (code,)).fetchone()
    if row is None:
        raise HTTPException(404, "邀请码不存在，请检查后重新输入")
    if row["share_token"] != share_token:
        # A code may only ever open the snapshot it was issued for.
        raise HTTPException(403, "邀请码不属于当前分享链接，无法在此使用")
    raise HTTPException(400, "邀请码已被使用，每个邀请码只能开启一次新会话")


@app.post("/api/share/{token}/resume")
def resume_share(token: str, payload: ShareResume):
    """Resume a paused share answer session with a one-time resume token.

    The resume token is bound to one specific share snapshot and one run; it
    can only be consumed while opening that very share link (path token must
    match ``resume_tokens.share_token``). The token is single-use: its ``used``
    flag flips to 1 atomically with the lookup (a conditional UPDATE), so a
    repeated or concurrent reuse is rejected. Finished sessions cannot be
    resumed either; start a new answer instead.
    """
    resume_token = (payload.resume_token or "").strip()
    if not resume_token:
        raise HTTPException(400, "请输入续答令牌")
    with get_db() as db:
        share = db.execute("SELECT 1 FROM shares WHERE token = ?", (token,)).fetchone()
        if share is None:
            raise HTTPException(404, "分享链接不存在或已失效")
        rt = db.execute(
            "SELECT * FROM resume_tokens WHERE token = ?", (resume_token,)
        ).fetchone()
        if rt is None:
            raise HTTPException(404, "续答令牌不存在，请检查后重新输入")
        if rt["share_token"] != token:
            # A token may only ever touch the snapshot it was issued for.
            raise HTTPException(403, "续答令牌不属于当前分享链接，无法在此使用")
        if rt["used"]:
            raise HTTPException(400, "续答令牌已使用，一次性令牌无法再次恢复；请开始新的作答")
        row = db.execute("SELECT * FROM runs WHERE id = ?", (rt["run_id"],)).fetchone()
        if row is None:
            raise HTTPException(404, "原作答会话已不存在，无法恢复")
        if row["finished"]:
            raise HTTPException(400, "该访谈已结束，无法续答；请开始新的作答")
        # Atomic single-use claim: only succeeds while used is still 0.
        claimed = db.execute(
            "UPDATE resume_tokens SET used = 1 WHERE token = ? AND used = 0",
            (resume_token,),
        ).rowcount
        if not claimed:
            raise HTTPException(400, "续答令牌已使用，一次性令牌无法再次恢复；请开始新的作答")
        return _run_response(db, row)


# ---- runtime preview ------------------------------------------------------

def _run_content(db, row) -> dict[str, Any]:
    if row["mode"] == "share":
        share = db.execute("SELECT content FROM shares WHERE token = ?",
                           (row["share_token"],)).fetchone()
        if share is None:
            raise HTTPException(404, "分享不存在")
        return json.loads(share["content"])
    script = db.execute("SELECT content FROM scripts WHERE id = ?",
                        (row["script_id"],)).fetchone()
    if script is None:
        raise HTTPException(404, "脚本不存在")
    return json.loads(script["content"])


@app.post("/api/runs", status_code=201)
def start_run(payload: RunStart):
    if not payload.script_id and not payload.share_token:
        raise HTTPException(400, "需要 script_id 或 share_token")
    invite_code = _normalize_invite(payload.invite_code)
    with get_db() as db:
        claimed_code = None
        if payload.share_token:
            share = db.execute(
                "SELECT content, invite_only FROM shares WHERE token = ?",
                (payload.share_token,),
            ).fetchone()
            if share is None:
                raise HTTPException(404, "分享不存在")
            if share["invite_only"]:
                # Invite-only snapshot: verify + consume the code and create the
                # session in ONE write transaction. Any failure raises and rolls
                # everything back, so a rejected code never leaves a session row.
                if not invite_code:
                    raise HTTPException(400, "该分享已开启邀请制，请输入邀请码后开始新会话")
                _claim_invite(db, payload.share_token, invite_code)
                claimed_code = invite_code
            content = json.loads(share["content"])
        else:
            row = db.execute("SELECT content FROM scripts WHERE id = ?",
                             (payload.script_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "脚本不存在")
            content = json.loads(row["content"])
        try:
            result = graph_step(content, "", None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = [{"node_id": node["id"], "title": node.get("title", ""),
                  "question": node.get("text", ""), "option_id": None, "answer": None}]
        finished = bool(node.get("terminal")) or not node.get("options")
        cur = db.execute(
            "INSERT INTO runs (mode, script_id, share_token, current_node_id, steps, finished, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("share" if payload.share_token else "script", payload.script_id,
             payload.share_token, node["id"], json.dumps(steps, ensure_ascii=False),
             int(finished), now_iso()),
        )
        run_id = cur.lastrowid
        if claimed_code is not None:
            # Same transaction: link the freshly created session to the code.
            db.execute("UPDATE invite_codes SET used_run_id = ? WHERE code = ?",
                       (run_id, claimed_code))
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


def _load_run(db, run_id: int):
    row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "预览会话不存在")
    return row


def _run_response(db, row) -> dict[str, Any]:
    """Serialize a run row into the API shape (current node + path)."""
    content = _run_content(db, row)
    node = next((n for n in content["nodes"] if n["id"] == row["current_node_id"]), None)
    steps = json.loads(row["steps"])
    return {"run_id": row["id"], "current_node": node, "steps": steps,
            "finished": bool(row["finished"])}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    with get_db() as db:
        row = _load_run(db, run_id)
        return _run_response(db, row)


@app.post("/api/runs/{run_id}/choice")
def choose(run_id: int, payload: RunChoice):
    with get_db() as db:
        row = _load_run(db, run_id)
        if row["finished"]:
            raise HTTPException(400, "访谈已结束，请重新开始")
        content = _run_content(db, row)
        try:
            result = graph_step(content, row["current_node_id"], payload.option_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = json.loads(row["steps"])
        current_node = next((n for n in content["nodes"] if n["id"] == row["current_node_id"]), None)
        if current_node is None:
            raise HTTPException(400, "当前节点已被编辑删除，请重新开始预览")
        opt = next((o for o in current_node.get("options", [])
                    if o["id"] == payload.option_id), None)
        steps[-1]["option_id"] = payload.option_id
        steps[-1]["answer"] = opt.get("label") if opt else payload.option_id
        steps.append({"node_id": node["id"], "title": node.get("title", ""),
                      "question": node.get("text", ""), "option_id": None, "answer": None})
        finished = bool(node.get("terminal")) or not node.get("options")
        db.execute("UPDATE runs SET current_node_id = ?, steps = ?, finished = ? WHERE id = ?",
                   (node["id"], json.dumps(steps, ensure_ascii=False), int(finished), run_id))
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


@app.post("/api/runs/{run_id}/restart")
def restart_run(run_id: int):
    with get_db() as db:
        row = _load_run(db, run_id)
        content = _run_content(db, row)
        try:
            result = graph_step(content, "", None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        node = result["node"]
        steps = [{"node_id": node["id"], "title": node.get("title", ""),
                  "question": node.get("text", ""), "option_id": None, "answer": None}]
        finished = bool(node.get("terminal")) or not node.get("options")
        db.execute("UPDATE runs SET current_node_id = ?, steps = ?, finished = ? WHERE id = ?",
                   (node["id"], json.dumps(steps, ensure_ascii=False), int(finished), run_id))
        # Restart wipes the paused state: outstanding one-time tokens can no
        # longer restore a path that has been cleared.
        db.execute("UPDATE resume_tokens SET used = 1 WHERE run_id = ? AND used = 0", (run_id,))
    return {"run_id": run_id, "current_node": node, "steps": steps, "finished": finished}


@app.post("/api/runs/{run_id}/pause", status_code=201)
def pause_run(run_id: int):
    """Mint a one-time resume token at the current (unfinished) node.

    Only answer sessions started from a read-only share link can be paused:
    the token stores the share snapshot it belongs to and may only be consumed
    via that snapshot's resume endpoint. A session that has already reached its
    end cannot be paused (there is nothing left to answer). The interview is
    not blocked by pausing — the respondent may keep answering; the token just
    captures the session, which resumes at whatever state it is in when the
    token is used.
    """
    with get_db() as db:
        row = _load_run(db, run_id)
        if row["mode"] != "share" or not row["share_token"]:
            raise HTTPException(400, "只有只读分享链接中的作答会话可以暂停续答")
        if row["finished"]:
            raise HTTPException(400, "访谈已结束，无需续答；请重新开始一次作答")
        share = db.execute("SELECT token FROM shares WHERE token = ?",
                           (row["share_token"],)).fetchone()
        if share is None:
            raise HTTPException(404, "所属分享快照已不存在，无法生成续答令牌")
        token = secrets.token_urlsafe(16)
        db.execute(
            "INSERT INTO resume_tokens (token, run_id, share_token, used, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (token, run_id, row["share_token"], now_iso()),
        )
        steps = json.loads(row["steps"])
    answered = sum(1 for s in steps if s.get("option_id"))
    return {"resume_token": token, "run_id": run_id,
            "share_token": row["share_token"], "answered": answered,
            "current_node_id": row["current_node_id"]}


# ---- static frontend ------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/share.html", include_in_schema=False)
def share_page():
    return FileResponse(f"{STATIC_DIR}/share.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
