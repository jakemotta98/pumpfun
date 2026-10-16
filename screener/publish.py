"""Copy the daily summary, a status file and a full data export to a private GitHub repo, so Claude can read them.

Runs in a worker thread with its own database connection. Uses a deploy key that can write to that one repo only.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .db import DB
from .report import daily_summary, export_csv

log = logging.getLogger("publish")


def _git(args: list[str], cwd: Path, key: Path, known_hosts: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                              f"-o UserKnownHostsFile={known_hosts}")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-c", "user.name=pumpscreener", "-c", "user.email=pumpscreener@localhost",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


def publish(db_path: Path, repo: str, data_dir: Path, tz_name: str = "Australia/Perth") -> str:
    """Write summary/status/export into the data repo and push. Returns a short result string."""
    key = data_dir / "deploy_key"
    known = data_dir / "known_hosts"
    work = data_dir / "publish"
    if not key.exists():
        return "no deploy key (run setup-publish.sh)"
    if not (work / ".git").exists():
        if work.exists():
            shutil.rmtree(work)
        r = _git(["clone", "--depth", "1", repo, str(work)], data_dir, key, known)
        if r.returncode != 0:
            return f"clone failed: {r.stderr.strip()[-300:]}"
    else:
        _git(["pull", "--ff-only", "--depth", "1", "origin", "main"], work, key, known)  # empty repo: harmless fail

    db = DB(db_path)  # own connection (thread-safe use)
    now = int(time.time())
    from zoneinfo import ZoneInfo
    local = dt.datetime.now(ZoneInfo(tz_name))
    summary = daily_summary(db, now)
    (work / "summaries").mkdir(exist_ok=True)
    (work / "summaries" / f"{local:%Y-%m-%d}.txt").write_text(summary + "\n")
    (work / "latest_summary.txt").write_text(f"Generated {local:%Y-%m-%d %H:%M} {tz_name}\n\n{summary}\n")

    c = db.conn
    status = {
        "generated_local": local.isoformat(timespec="minutes"),
        "graduates_total": c.execute("SELECT COUNT(*) n FROM migrations").fetchone()["n"],
        "first_graduate_ts": c.execute("SELECT MIN(block_time) t FROM migrations").fetchone()["t"],
        "snapshots": {r["status"]: r["n"] for r in
                      c.execute("SELECT status, COUNT(*) n FROM snapshots GROUP BY status")},
        "deep_snapshots": c.execute("SELECT COUNT(*) n FROM snapshots WHERE kind='deep'").fetchone()["n"],
        "price_rows": c.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"],
        "events_last_24h": {r["kind"]: r["n"] for r in c.execute(
            "SELECT kind, COUNT(*) n FROM events WHERE ts>=? GROUP BY kind", (now - 86400,))},
        "prereg_hash": db.get_meta("last_start_msg_hash"),
    }
    (work / "status.json").write_text(json.dumps(status, indent=2) + "\n")

    tmp = data_dir / "export_tmp.csv"
    n = export_csv(db, tmp)
    if n:
        with open(tmp, "rb") as src, gzip.open(work / "export_latest.csv.gz", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        tmp.unlink()

    _git(["add", "-A"], work, key, known)
    r = _git(["commit", "-m", f"data {local:%Y-%m-%d %H:%M}"], work, key, known)
    if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
        return "no changes"
    r = _git(["push", "origin", "HEAD:main"], work, key, known)
    if r.returncode != 0:
        return f"push failed: {r.stderr.strip()[-300:]}"
    return f"published ({n} export rows)"
