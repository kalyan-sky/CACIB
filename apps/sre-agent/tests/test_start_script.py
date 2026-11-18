"""bin/start: restore-then-replicate wrapper around the agent (durable history).

A stand-in `litestream` on PATH records each call, so these tests check the wrapper's
decisions (when to restore, when to refuse to start, what config it writes) without
needing S3. The real Litestream is exercised on the cluster by scripts/sre-agent-verify.sh.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

START = Path(__file__).resolve().parents[1] / "bin" / "start"

FAKE_LITESTREAM = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  restore)
    for last; do :; done
    case "$FAKE_RESTORE" in
      restore) echo restored > "$last" ;;
      none) ;;
      fail) exit 1 ;;
    esac ;;
  replicate)
    # run the -exec command, like the real one
    while [ $# -gt 0 ]; do [ "$1" = "-exec" ] && exec sh -c "$2"; shift; done ;;
esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "litestream"
    fake.write_text(FAKE_LITESTREAM)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_RESTORE": "restore",
        "SRE_AGENT_DB": str(tmp_path / "data" / "sre-agent.db"),
        "SRE_AGENT_APP_CMD": f"touch {tmp_path / 'app-started'}",
        "SRE_AGENT_REPLICA_URL": "s3://bucket/sre-agent/bank-sim",
    }


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(START)], env=env, capture_output=True, text=True, timeout=20)


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["FAKE_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def started(env: dict[str, str]) -> bool:
    return (Path(env["TMPDIR"]) / "app-started").exists()


def test_no_replica_url_runs_the_agent_directly_with_a_warning(env):
    env["SRE_AGENT_REPLICA_URL"] = ""
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert started(env) and calls(env) == []
    assert "lost with it" in r.stderr


def test_restores_then_replicates_when_no_local_database(env):
    r = run(env)
    assert r.returncode == 0, r.stderr
    c = calls(env)
    assert c[0].startswith("restore -no-expand-env -config ") and "-if-db-not-exists -if-replica-exists" in c[0]
    assert c[0].endswith(env["SRE_AGENT_DB"])
    assert c[1].startswith("replicate -no-expand-env -config ") and "-exec" in c[1]
    assert Path(env["SRE_AGENT_DB"]).read_text().strip() == "restored"
    assert started(env)
    assert "restored from the replica" in r.stderr


def test_first_run_with_empty_replica_starts_fresh(env):
    env["FAKE_RESTORE"] = "none"
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert started(env) and "empty history" in r.stderr


def test_existing_local_database_is_kept_not_overwritten(env):
    db = Path(env["SRE_AGENT_DB"])
    db.parent.mkdir(parents=True)
    db.write_text("local")
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("restore") for c in calls(env))
    assert db.read_text() == "local" and started(env)


def test_failed_restore_refuses_to_start_by_default(env):
    env["FAKE_RESTORE"] = "fail"
    r = run(env)
    assert r.returncode == 75
    assert not started(env)
    assert not any(c.startswith("replicate") for c in calls(env))
    assert "not starting" in r.stderr


def test_failed_restore_can_be_overridden(env):
    env["FAKE_RESTORE"] = "fail"
    env["SRE_AGENT_REPLICA_REQUIRED"] = "false"
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert started(env)


def test_config_names_the_database_replica_region_and_metrics(env):
    env["SRE_AGENT_REPLICA_REGION"] = "ap-southeast-2"
    assert run(env).returncode == 0
    cfg = (Path(env["TMPDIR"]) / "sre-agent-litestream.yml").read_text()
    assert f'path: "{env["SRE_AGENT_DB"]}"' in cfg
    assert 'url: "s3://bucket/sre-agent/bank-sim"' in cfg
    assert 'region: "ap-southeast-2"' in cfg
    assert 'addr: ":9091"' in cfg
    mode = os.stat(Path(env["TMPDIR"]) / "sre-agent-litestream.yml").st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize("url", ["file:///shared/hpc/sre-agent", "s3://b/p?endpoint=https://minio.internal:9000",
                                 "gs://b/p", "sftp://user@host/p"])
def test_other_replica_types_for_on_prem_and_hpc_sites(env, url):
    env["SRE_AGENT_REPLICA_URL"] = url
    assert run(env).returncode == 0


@pytest.mark.parametrize("url", ["http://x/y", "/data/copy", "s3://b/p\" injected", "s3://b/p x"])
def test_rejects_bad_replica_urls(env, url):
    env["SRE_AGENT_REPLICA_URL"] = url
    r = run(env)
    assert r.returncode == 64 and not started(env)


def test_missing_litestream_binary_is_an_error_not_a_silent_fallback(env):
    env["PATH"] = "/usr/bin:/bin"
    r = run(env)
    assert r.returncode == 69 and not started(env)
