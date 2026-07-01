"""
core/portal_scheduler.py — Developer Portal scheduled agent (Phase 4).

Runs the read-only portal agent on a cron cadence (Asia/Kolkata), reusing the
shared APScheduler from core.scheduler. A scheduled run asks the agent a stored
prompt; the agent may file requests into the approval queue, but it still cannot
change live data — every resulting request awaits human approval.

All job functions swallow their own errors and record a run row, so a failing
job never blocks anything else. Jobs live under the id prefix `portal_agent_`.
"""
import json
import logging
import datetime

logger = logging.getLogger("eko_recon.portal_scheduler")

_JOB_PREFIX = "portal_agent_"


def _job_id(jid: str) -> str:
    return f"{_JOB_PREFIX}{jid}"


def run_job(job_id: str, trigger: str = "schedule") -> str:
    """Execute one scheduled-agent job. Returns the run id. Self-contained DB session."""
    from models.database import SessionLocal, generate_id, PortalAgentJob, PortalAgentRun
    from core import portal_agent

    db = SessionLocal()
    run_id = generate_id()
    try:
        job = db.query(PortalAgentJob).filter(PortalAgentJob.id == job_id).first()
        if not job:
            return run_id
        run = PortalAgentRun(id=run_id, job_id=job_id, trigger=trigger,
                             started_at=datetime.datetime.utcnow(), status="running")
        db.add(run)
        db.commit()

        answer, tools, err = "", [], None
        if not portal_agent.is_enabled():
            err = "Agent not configured (ANTHROPIC_API_KEY missing or no credits)."
        else:
            try:
                for ev in portal_agent.stream_chat([], job.prompt,
                                                   actor="portal-scheduler", session_id=None):
                    t = ev.get("type")
                    if t == "text":
                        answer += ev.get("text", "")
                    elif t == "tool":
                        tools.append(ev.get("summary", ""))
                    elif t == "error":
                        err = ev.get("error")
            except Exception as e:  # pragma: no cover - defensive
                err = str(e)

        filed = sum(1 for s in tools if str(s).startswith("file_request"))
        now = datetime.datetime.utcnow()
        run.finished_at = now
        run.status = "error" if err else "ok"
        run.summary = answer[:20000] or None
        run.tools_used = json.dumps(tools) if tools else None
        run.requests_filed = filed
        run.error = err
        job.last_run_at = now
        job.last_status = run.status
        job.last_summary = (err or answer)[:2000] or None
        db.commit()
        logger.info("portal agent job %s -> %s (%d requests filed)", job_id, run.status, filed)
    except Exception as e:
        logger.exception("portal agent job %s crashed: %s", job_id, e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    return run_id


def _trigger_for(job):
    from apscheduler.triggers.cron import CronTrigger
    if job.frequency == "weekly":
        dow = job.day_of_week if job.day_of_week is not None else 0
        return CronTrigger(day_of_week=dow, hour=job.hour, minute=job.minute, timezone="Asia/Kolkata")
    return CronTrigger(hour=job.hour, minute=job.minute, timezone="Asia/Kolkata")


def register_job(job):
    """(Re)register a job on the shared scheduler. No-op if disabled (it's removed)."""
    from core.scheduler import get_scheduler
    sched = get_scheduler()
    jid = _job_id(job.id)
    if sched.get_job(jid):
        sched.remove_job(jid)
    if not job.is_enabled:
        return
    sched.add_job(run_job, _trigger_for(job), id=jid, args=[job.id],
                  replace_existing=True, misfire_grace_time=3600)


def unregister_job(job_id: str):
    from core.scheduler import get_scheduler
    sched = get_scheduler()
    jid = _job_id(job_id)
    if sched.get_job(jid):
        sched.remove_job(jid)


def reload_all_portal_jobs(db):
    """Called at startup — register every enabled job from the DB."""
    from models.database import PortalAgentJob
    try:
        for job in db.query(PortalAgentJob).filter(PortalAgentJob.is_enabled == True).all():
            try:
                register_job(job)
            except Exception as e:
                logger.warning("could not register portal job %s: %s", job.id, e)
    except Exception as e:
        logger.warning("reload_all_portal_jobs skipped: %s", e)
