import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "guest_run.sh"

FAKE_PYTHON = """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then exit 0; fi
printf 'ENV=%s\\n' "$NYOANIME_WATCH_TOGETHER_NAME"
printf 'ARGS:'; printf ' %q' "$@"; echo
"""


def _sandbox(tmp_path):
    shutil.copy(SCRIPT, tmp_path / "guest_run.sh")
    script = tmp_path / "guest_run.sh"
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(FAKE_PYTHON, encoding="utf-8")
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "python3").symlink_to(bin_dir / "python")
    (bin_dir / "activate").write_text(
        f'export PATH="{bin_dir}:$PATH"\n', encoding="utf-8"
    )

    home = tmp_path / "home"
    home.mkdir()
    return script, home


def _run(script, home, *args, stdin=None):
    env = dict(os.environ, HOME=str(home))
    env.pop("NYOANIME_WATCH_TOGETHER_NAME", None)
    return subprocess.run(
        [str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def test_guest_run_forwards_room_code_and_guest_profile(tmp_path):
    script, home = _sandbox(tmp_path)

    result = _run(script, home, "378854", "--debug")

    assert result.returncode == 0, result.stderr
    assert "ENV=Guest" in result.stdout
    assert (
        f"main.py --session-dir {home}/.nyoanime/guest "
        "--join-room 378854 --debug"
    ) in result.stdout


def test_guest_run_prompts_and_trims_room_code(tmp_path):
    script, home = _sandbox(tmp_path)

    result = _run(script, home, stdin="  654321 \n")

    assert result.returncode == 0, result.stderr
    assert "--join-room 654321" in result.stdout


def test_guest_run_rejects_an_empty_room_code(tmp_path):
    script, home = _sandbox(tmp_path)

    result = _run(script, home, stdin="\n")

    assert result.returncode == 1
    assert "A room code is required to join." in result.stderr
