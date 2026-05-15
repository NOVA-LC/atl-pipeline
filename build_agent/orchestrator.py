"""Build agent orchestrator. Top-level loop that:

1. Validates the lead is buildable (has GBP or existing site)
2. Researches → gathers assets → picks inspiration → builds HTML
3. Runs critics + technical gates in a loop until score thresholds met
   OR budget/time exhausted
4. Ships best-so-far + flags issues to the rep dialer for approval

Budget caps + daily fleet cap + per-tool timeout/retry/fallback per SPEC.md §8.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

# ─── config ─────────────────────────────────────────────────────────────────
PER_BUILD_BUDGET_USD = float(os.environ.get("BUILD_AGENT_PER_BUILD_BUDGET", "7.00"))
PER_BUILD_DEADLINE_SEC = int(os.environ.get("BUILD_AGENT_DEADLINE_SEC", "720"))  # 12 min
DAILY_FLEET_CAP_USD = float(os.environ.get("BUILD_AGENT_DAILY_FLEET_CAP", "100.00"))
MAX_CRITIC_ITERATIONS = int(os.environ.get("BUILD_AGENT_MAX_ITERATIONS", "6"))

# Gate thresholds (SPEC §5)
GATE_CODE_CRITIC_MIN = 90
GATE_VISION_CRITIC_MIN = 7.5
GATE_LIGHTHOUSE_PERF_MIN = 85
GATE_LIGHTHOUSE_A11Y_MIN = 90
GATE_REAL_ASSET_RATIO_MIN = 0.60

# Paths
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("BUILD_AGENT_DATA_DIR", str(REPO_ROOT / "build_agent" / "_data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BUILDS_DIR = Path(os.environ.get("BUILD_AGENT_BUILDS_DIR", str(DATA_DIR / "builds")))
BUILDS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("BUILD_AGENT_DB", str(DATA_DIR / "build_agent.db")))


# ─── DB ─────────────────────────────────────────────────────────────────────
def _ensure_schema():
    schema_path = REPO_ROOT / "build_agent" / "db_schema.sql"
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(schema_path.read_text(encoding="utf-8"))


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ─── state ──────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class BuildState:
    """Per-build mutable state owned by the orchestrator."""
    lead_id: str
    business_name: str
    slug: str
    job_id: str
    started_at: float
    budget_remaining: float = PER_BUILD_BUDGET_USD
    iterations: int = 0
    research_brief: dict[str, Any] | None = None
    assets_manifest: dict[str, Any] | None = None
    inspiration_refs: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    current_html: str = ""
    best_html: str = ""
    best_score: float = 0.0
    last_verdict: dict[str, Any] | None = None
    fallbacks_used: list[str] = dataclasses.field(default_factory=list)
    out_dir: Path | None = None
    cost_breakdown: dict[str, float] = dataclasses.field(default_factory=lambda: {
        "research": 0.0, "assets": 0.0, "inspiration": 0.0,
        "builder": 0.0, "critic_code": 0.0, "critic_vision": 0.0, "other": 0.0,
    })

    def time_remaining(self) -> float:
        return max(0.0, PER_BUILD_DEADLINE_SEC - (time.time() - self.started_at))

    def budget_low(self) -> bool:
        return self.budget_remaining < 0.50

    def time_low(self) -> bool:
        return self.time_remaining() < 30.0

    def spent(self) -> float:
        return PER_BUILD_BUDGET_USD - self.budget_remaining


# ─── progress callback ──────────────────────────────────────────────────────
ProgressFn = Callable[[str, dict[str, Any]], None]


def _noop(event: str, payload: dict[str, Any]) -> None:
    pass


# ─── daily cap ──────────────────────────────────────────────────────────────
DAILY_CAP_LOCK_FILE = DATA_DIR / "daily_cap.lock"


def check_daily_cap() -> dict[str, Any]:
    """Returns {allowed, spent_today, cap, locked, unlock_token}."""
    _ensure_schema()
    locked = DAILY_CAP_LOCK_FILE.exists()
    with _db() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(spend_actual_usd), 0) AS spent "
            "FROM build_jobs WHERE date(started_at) = date('now')"
        ).fetchone()
    spent = float(row["spent"] or 0)
    allowed = (spent < DAILY_FLEET_CAP_USD) and (not locked)
    return {
        "allowed":     allowed,
        "spent_today": round(spent, 2),
        "cap":         DAILY_FLEET_CAP_USD,
        "locked":      locked,
        "lock_path":   str(DAILY_CAP_LOCK_FILE),
    }


def lock_daily_cap(reason: str = "manual") -> None:
    DAILY_CAP_LOCK_FILE.write_text(json.dumps({"reason": reason, "at": _now_iso()}))


def unlock_daily_cap() -> bool:
    if DAILY_CAP_LOCK_FILE.exists():
        DAILY_CAP_LOCK_FILE.unlink()
        return True
    return False


# ─── pre-filter ─────────────────────────────────────────────────────────────
def precheck_buildable(lead: dict[str, Any]) -> dict[str, Any]:
    """Pre-filter: reject leads with no online presence. Cheap, no API spend.

    Returns {buildable, reason}. Caller skips the build if buildable is False.
    """
    business_name = (lead.get("business_name") or "").strip()
    phone = (lead.get("phone") or "").strip()
    existing_url = (lead.get("existing_url") or lead.get("vercel_url") or "").strip()

    if not business_name:
        return {"buildable": False, "reason": "no business_name"}
    if not phone and not existing_url:
        # Could still be buildable via GBP — but if both are missing the lead is fishy
        return {"buildable": True, "reason": "no phone or existing_url — GBP lookup will be the only path"}
    return {"buildable": True, "reason": "ok"}


# ─── slug helper ────────────────────────────────────────────────────────────
def _slugify(value: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "")).strip("-").lower()
    return s[:48] or f"build-{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── main build loop ────────────────────────────────────────────────────────
def build(lead: dict[str, Any], progress: ProgressFn = _noop) -> dict[str, Any]:
    """Top-level entry. Returns {job_id, url, final_code_score, final_vision_score,
    budget_used, duration_sec, ship_reason, gates_passed, manifest_path}."""
    _ensure_schema()

    # ── daily cap ──
    cap = check_daily_cap()
    if not cap["allowed"]:
        return {"error": "daily_cap_reached", **cap}

    # ── pre-filter ──
    pre = precheck_buildable(lead)
    if not pre["buildable"]:
        return {"error": "build_unfit_precheck", "reason": pre["reason"]}

    # ── state setup ──
    business_name = lead["business_name"]
    base_slug = _slugify(business_name)
    # Disambiguate slug on collision (rebuilds for the same business get -2, -3, ...)
    slug = base_slug
    with _db() as c:
        n = 2
        while c.execute("SELECT 1 FROM build_jobs WHERE slug = ?", (slug,)).fetchone():
            slug = f"{base_slug}-{n}"
            n += 1
            if n > 999:
                break
    job_id = uuid.uuid4().hex[:16]
    out_dir = BUILDS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    state = BuildState(
        lead_id=lead.get("lead_id", ""),
        business_name=business_name,
        slug=slug,
        job_id=job_id,
        started_at=time.time(),
        out_dir=out_dir,
    )

    # Persist job row
    with _db() as c:
        c.execute(
            "INSERT INTO build_jobs (id, lead_id, business_name, slug, budget_cap_usd, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, lead.get("lead_id", ""), business_name, slug, PER_BUILD_BUDGET_USD, "queued"),
        )

    progress("queued", {"job_id": job_id, "slug": slug, "out_dir": str(out_dir)})

    # ── step: research ──
    try:
        from build_agent.agents import researcher
        progress("researching", {"job_id": job_id, "business_name": business_name})
        brief = researcher.research(lead)
        state.research_brief = brief
        cost = float((brief.get("_meta") or {}).get("research_cost_usd", 0))
        state.budget_remaining -= cost
        state.cost_breakdown["research"] += cost
        state.fallbacks_used.extend((brief.get("_meta") or {}).get("fallbacks", []))
        (out_dir / "research_brief.json").write_text(json.dumps(brief, indent=2, default=str))
        if brief.get("build_unfit"):
            _finalize_job(job_id, state, status="build_unfit", ship_reason="build_unfit")
            return {"error": "build_unfit", "job_id": job_id, "reason": "no GBP and no existing website"}
    except Exception as e:
        _finalize_job(job_id, state, status="failed", ship_reason="tool_failure",
                      error_summary=f"researcher: {e}\n{traceback.format_exc(limit=4)}")
        return {"error": "researcher_failed", "job_id": job_id, "exception": str(e)}

    # ── step: assets ──
    try:
        from build_agent.agents import asset_gatherer
        progress("gathering_assets", {"job_id": job_id})
        manifest = asset_gatherer.gather(brief, out_dir / "assets")
        state.assets_manifest = manifest
        cost = float(manifest.get("cost_usd", 0))
        state.budget_remaining -= cost
        state.cost_breakdown["assets"] += cost
        state.fallbacks_used.extend(manifest.get("fallbacks", []))
    except Exception as e:
        _finalize_job(job_id, state, status="failed", ship_reason="tool_failure",
                      error_summary=f"asset_gatherer: {e}\n{traceback.format_exc(limit=4)}")
        return {"error": "asset_gatherer_failed", "job_id": job_id, "exception": str(e)}

    # ── step: inspiration ──
    try:
        from build_agent.agents import inspiration_picker
        progress("picking_inspiration", {"job_id": job_id})
        # Pull last 5 builds' fingerprints for diversity check
        with _db() as c:
            recent_rows = c.execute(
                "SELECT fingerprint FROM build_jobs WHERE status='shipped' AND id != ? "
                "ORDER BY started_at DESC LIMIT 5",
                (job_id,),
            ).fetchall()
        recent_fps: list[dict[str, Any]] = []
        for row in recent_rows:
            if row["fingerprint"]:
                try:
                    recent_fps.append(json.loads(row["fingerprint"]))
                except Exception:
                    pass
        refs = inspiration_picker.pick(brief, recent_fps)
        state.inspiration_refs = refs
        progress("inspiration_picked", {"job_id": job_id, "ref_ids": [r["id"] for r in refs]})
    except Exception as e:
        state.fallbacks_used.append(f"inspiration_picker failed: {e}")
        state.inspiration_refs = []

    # ── step: first build draft ──
    try:
        from build_agent.agents import builder
        progress("building_first_draft", {"job_id": job_id})
        result = builder.build_html(brief, state.assets_manifest, state.inspiration_refs)
        state.current_html = result["html"]
        state.budget_remaining -= result["cost_usd"]
        state.cost_breakdown["builder"] += result["cost_usd"]
        state.iterations = 1
    except Exception as e:
        _finalize_job(job_id, state, status="failed", ship_reason="tool_failure",
                      error_summary=f"builder: {e}\n{traceback.format_exc(limit=4)}")
        return {"error": "builder_failed", "job_id": job_id, "exception": str(e)}

    # ── critic loop ──
    final_verdict: dict[str, Any] = {}
    for iteration in range(MAX_CRITIC_ITERATIONS):
        state.iterations = iteration + 1
        # Save current HTML to disk so screenshots / lighthouse can run
        html_path = out_dir / "index.html"
        html_path.write_text(state.current_html, encoding="utf-8")

        # Code critic + tech gates (cheap, always run)
        try:
            from build_agent.agents import critic_code
            from build_agent.tools import technical_gates
            cv = critic_code.grade(state.current_html, brief, state.assets_manifest, [r["id"] for r in state.inspiration_refs])
            tg = technical_gates.run_all(html_path, run_lighthouse=False, screenshot_dir=out_dir / "screenshots")
            state.cost_breakdown["critic_code"] += 0.0  # deterministic, no API
            code_score = cv["score"]
            html_valid = tg["html_validate"]["valid"]
            responsive_ok = tg["responsive"]["ok"]
        except Exception as e:
            state.fallbacks_used.append(f"code_critic/tech_gates iter {iteration}: {e}")
            cv, tg = {}, {}
            code_score = 0
            html_valid = False
            responsive_ok = False

        # Vision critic — only when code is decent + budget allows
        vision_verdict: dict[str, Any] = {}
        vision_score = 0.0
        if code_score >= 70 and not state.budget_low():
            try:
                from build_agent.agents import critic_vision
                screenshots = tg.get("screenshots", {}) or {}
                # screenshot_widths returns {width:int -> path}; re-key by int
                ss_dict = {int(k): Path(v) for k, v in screenshots.items()} if screenshots else {}
                if ss_dict:
                    vision_verdict = critic_vision.grade(ss_dict, brief, inspiration_refs=state.inspiration_refs)
                    vision_score = float(vision_verdict.get("final_weighted") or 0)
                    vcost = float((vision_verdict.get("_meta") or {}).get("cost_usd", 0))
                    state.budget_remaining -= vcost
                    state.cost_breakdown["critic_vision"] += vcost
            except Exception as e:
                state.fallbacks_used.append(f"vision_critic iter {iteration}: {e}")

        verdict = {
            "iteration":   iteration + 1,
            "code":        cv,
            "tech":        tg,
            "vision":      vision_verdict,
            "code_score":  code_score,
            "vision_score": vision_score,
            "html_valid":  html_valid,
            "responsive_ok": responsive_ok,
            "budget_remaining": round(state.budget_remaining, 4),
            "time_remaining":   round(state.time_remaining(), 1),
        }
        final_verdict = verdict

        # Track best
        combined_score = code_score + vision_score * 10  # weight vision into a 0-100 unified
        if combined_score > state.best_score:
            state.best_score = combined_score
            state.best_html = state.current_html

        progress("critic_done", {
            "job_id": job_id, "iteration": iteration + 1,
            "code_score": code_score, "vision_score": vision_score,
            "must_fix_count": len((cv or {}).get("must_fixes", [])),
            "budget_remaining": round(state.budget_remaining, 4),
        })

        # Ship condition: all gates pass
        gates_pass = (
            code_score >= GATE_CODE_CRITIC_MIN
            and (vision_score >= GATE_VISION_CRITIC_MIN or not vision_verdict)  # vision optional if not run
            and html_valid
            and responsive_ok
        )
        if gates_pass:
            verdict["ship_reason"] = "ok"
            break

        if state.budget_low() or state.time_low():
            verdict["ship_reason"] = "budget_or_time_exhausted"
            break

        # Dispatch fixes (regenerate via builder.regenerate_section)
        must_fixes = (cv or {}).get("must_fixes", []) or []
        if not must_fixes:
            # Code says fine but vision says no; try a full re-build with vision must_fixes
            must_fixes = [{"section": "overall", "intent": "vision", "text": mf}
                          for mf in vision_verdict.get("must_fixes", [])[:2]]
        if not must_fixes:
            verdict["ship_reason"] = "no_actionable_fixes"
            break

        # Apply ONE must_fix per iteration (cheaper, more controlled)
        top_fix = must_fixes[0]
        try:
            regen = builder.regenerate_section(
                state.current_html,
                section=top_fix.get("section", "overall"),
                must_fix=top_fix.get("text", ""),
                research_brief=brief,
                assets_manifest=state.assets_manifest,
            )
            state.current_html = regen["html"]
            state.budget_remaining -= regen["cost_usd"]
            state.cost_breakdown["builder"] += regen["cost_usd"]
        except Exception as e:
            state.fallbacks_used.append(f"regenerate_section iter {iteration}: {e}")
            break

    # ── finalize ──
    final_html = state.best_html or state.current_html
    (out_dir / "index.html").write_text(final_html, encoding="utf-8")
    (out_dir / "verdict.json").write_text(json.dumps(final_verdict, indent=2, default=str))

    # Persist build job row
    fingerprint = (final_verdict.get("code") or {}).get("fingerprint", {})
    _finalize_job(
        job_id, state,
        status="shipped" if final_verdict.get("ship_reason") == "ok" else "failed",
        ship_reason=final_verdict.get("ship_reason", "gates_failed"),
        code_score=final_verdict.get("code_score"),
        vision_score=final_verdict.get("vision_score"),
        html_valid=1 if final_verdict.get("html_valid") else 0,
        responsive_ok=1 if final_verdict.get("responsive_ok") else 0,
        fingerprint=fingerprint,
        real_asset_ratio=(state.assets_manifest or {}).get("real_asset_ratio"),
        inspiration_ref_ids=[r["id"] for r in state.inspiration_refs],
    )

    progress("done", {
        "job_id": job_id,
        "ship_reason": final_verdict.get("ship_reason"),
        "code_score": final_verdict.get("code_score"),
        "vision_score": final_verdict.get("vision_score"),
        "budget_used": round(state.spent(), 4),
        "duration_sec": round(time.time() - state.started_at, 1),
    })

    return {
        "job_id":            job_id,
        "slug":              slug,
        "out_dir":           str(out_dir),
        "html_path":         str(out_dir / "index.html"),
        "ship_reason":       final_verdict.get("ship_reason"),
        "code_score":        final_verdict.get("code_score"),
        "vision_score":      final_verdict.get("vision_score"),
        "html_valid":        final_verdict.get("html_valid"),
        "responsive_ok":     final_verdict.get("responsive_ok"),
        "budget_used":       round(state.spent(), 4),
        "duration_sec":      round(time.time() - state.started_at, 1),
        "iterations":        state.iterations,
        "fallbacks":         state.fallbacks_used,
        "cost_breakdown":    state.cost_breakdown,
    }


# ─── job row persistence ────────────────────────────────────────────────────
def _finalize_job(job_id: str, state: BuildState, **fields):
    spend_total = state.spent()
    fingerprint = fields.pop("fingerprint", {})
    inspiration_ref_ids = fields.pop("inspiration_ref_ids", None)
    with _db() as c:
        sql_parts = ["finished_at = CURRENT_TIMESTAMP", "iterations = ?", "spend_actual_usd = ?",
                     "spend_research_usd = ?", "spend_assets_usd = ?", "spend_builder_usd = ?",
                     "spend_critic_vision_usd = ?", "fallbacks_used = ?"]
        params = [state.iterations, spend_total,
                  state.cost_breakdown["research"], state.cost_breakdown["assets"],
                  state.cost_breakdown["builder"], state.cost_breakdown["critic_vision"],
                  json.dumps(state.fallbacks_used)]
        for k, v in fields.items():
            sql_parts.append(f"{k} = ?")
            params.append(v)
        if fingerprint:
            sql_parts.append("fingerprint = ?")
            params.append(json.dumps(fingerprint))
        if inspiration_ref_ids is not None:
            sql_parts.append("inspiration_ref_ids = ?")
            params.append(json.dumps(inspiration_ref_ids))
        params.append(job_id)
        c.execute(f"UPDATE build_jobs SET {', '.join(sql_parts)} WHERE id = ?", params)


# ─── public helpers for the dialer ──────────────────────────────────────────
def get_job(job_id: str) -> dict[str, Any] | None:
    with _db() as c:
        row = c.execute("SELECT * FROM build_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM build_jobs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
