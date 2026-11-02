#!/usr/bin/env python3
"""BPA runner: create a run on the hub, run your solution against it, complete it, print the report.

Standard library only (Python 3.10+). Examples:

    python bpa_runner.py --hub URL --key KEY --project A --suite public -- python my_solution.py {base_url}
    python bpa_runner.py --hub URL --key KEY --project A --suite public --manual
    python bpa_runner.py --local --project A --suite public -- python my_solution.py {base_url}   # staff only

`{base_url}` in the command is replaced by the run's base URL, which is also set as BPA_BASE_URL in
the command's environment. `--hub` and `--key` default to the BPA_HUB and BPA_KEY environment variables.
The solution never gets a key: BPA_KEY and BPA_STAFF_KEY are removed from its environment (it needs only
the base URL, and a staff key would open every private report).

--manual   prints the base URL and waits for Enter before completing the run (for UiPath).
--local    staff only: starts a development hub on a free port. It works only inside the staff's course
           repository (it needs the hub package, or `uv` and the repo's pyproject.toml next to this
           file's folder); students have no offline hub and use the course hub.
--json     prints the raw report JSON on stdout (the solution's output then goes to stderr).
--change-request ID
           with --suite change-request: the change request to run. Teams can run a change request
           once staff have released it; its report shows only the total and the category totals.

Restart tests (the grade comes from the run's final state, so a restarted solution must not repeat
what is already recorded):

--invocations N
           start the solution N times, one after the other, on the same run; then complete it.
--kill-after-writes K
           first start the solution once and kill it, with every process it started, as soon as the
           run has K committed writes (the hub pauses the run at that point, so nothing after the K-th
           write is recorded); then resume the run and start the solution again (N times with
           --invocations N). With --manual the runner tells you when to stop and restart your robot.
--label TEXT
           a note stored with the run; the restart options are added to it automatically.

Staff grade a team's solution with `--staff-key KEY --team-id team-07 --suite private` (staff mode is
on whenever --team-id is given or the suite is private, and with --local also for change-request).
--local always uses the dev keys of the local hub. Staff may vary the config of a private or
change-request run with `--config-overrides FILE.json` (the keys the project allows).

Once the run exists it is always completed and its report printed, also when the solution fails,
cannot start or is interrupted with Ctrl+C (a second Ctrl+C leaves the run open).

Exit code: the solution's exit code if it failed (the first failing start, not counting the one the
runner killed; 124 on timeout, 130 on Ctrl+C), 2 for runner or hub errors (including a solution
command that cannot start), else 0.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEV_TEAM_KEY = "dev-team-key"
DEV_STAFF_KEY = "dev-staff-key"
DEV_TEAM_ID = "team-dev"
USER_AGENT = "bpa-runner/1.0"
POLL_SECONDS = 0.2  # how often the runner checks the solution (and, for a kill, the run's writes)


class RunnerError(Exception):
    """Anything that stops the runner before or after the solution ran."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def utf8_output() -> None:
    """Write UTF-8 to pipes and files too (Windows defaults to the ANSI code page, which lacks ș and ț)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def call(hub: str, key: str, method: str, path: str, body: Any = None, timeout: float = 120) -> Any:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(hub.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            error = json.loads(raw).get("error", {})
            detail = f"{error.get('code')}: {error.get('message')}"
            if error.get("details"):
                detail += f" {json.dumps(error['details'])}"
        except (ValueError, AttributeError):
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise RunnerError(f"{method} {path} failed with HTTP {err.code}: {detail}") from None
    except urllib.error.URLError as err:
        raise RunnerError(f"cannot reach the hub at {hub}: {err.reason}") from None


# Local development hub -------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hub_python() -> str:
    """A Python that can import the hub: this one, else the course repo's environment (found with uv).

    The hub is then started directly with that interpreter, never through `uv run`: stopping a
    `uv run` process on Windows would leave the hub running.
    """
    if importlib.util.find_spec("bpahub") is not None:
        return sys.executable
    repo = Path(__file__).resolve().parent.parent
    uv = shutil.which("uv")
    if not (uv and (repo / "pyproject.toml").is_file() and (repo / "hub" / "bpahub").is_dir()):
        raise RunnerError("--local needs the hub package: run it from the course repo with uv installed")
    probe = [uv, "run", "--project", str(repo), "python", "-c", "import bpahub, sys; print(sys.executable)"]
    try:
        found = subprocess.run(probe, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as err:
        raise RunnerError(f"cannot run uv: {err}") from None
    if found.returncode != 0 or not found.stdout.strip():
        raise RunnerError(f"uv cannot prepare the course environment:\n{found.stderr[-2000:]}")
    return found.stdout.strip().splitlines()[-1]


class LocalHub:
    """`python -m bpahub serve --dev` on a free port with a throw-away data directory."""

    def __init__(self) -> None:
        command = [hub_python(), "-m", "bpahub", "serve", "--dev", "--host", "127.0.0.1"]
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.data_dir = Path(tempfile.mkdtemp(prefix="bpa-local-"))
        self.log_path = self.data_dir / "hub.log"
        self._log_file = self.log_path.open("wb")
        env = {**os.environ, "BPA_DATA_DIR": str(self.data_dir)}
        self.process = subprocess.Popen(
            [*command, "--port", str(self.port)], env=env, stdout=self._log_file, stderr=subprocess.STDOUT
        )

    def wait_until_healthy(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RunnerError(f"the local hub exited with code {self.process.returncode}:\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.2)
        raise RunnerError(f"the local hub did not become healthy in {timeout:.0f}s:\n{self.log_tail()}")

    def log_tail(self) -> str:
        self._log_file.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-3000:]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._log_file.close()
        for _ in range(50):  # Windows releases the dead hub's file handles a moment later
            shutil.rmtree(self.data_dir, ignore_errors=True)
            if not self.data_dir.exists():
                break
            time.sleep(0.1)


# Run lifecycle ---------------------------------------------------------------


def run_label(args: argparse.Namespace) -> str | None:
    """The `--label` text plus the restart options, so reports and scores.csv show how the run was made."""
    parts = [args.label] if args.label else []
    if args.kill_after_writes:
        parts.append(f"kill-after-writes={args.kill_after_writes}")
    if args.invocations != 1:
        parts.append(f"invocations={args.invocations}")
    return "; ".join(parts) or None


def create_run(args: argparse.Namespace, hub: str) -> dict[str, Any]:
    body: dict[str, Any] = {"project": args.project, "suite": args.suite}
    if args.scenarios:
        body["scenarios"] = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    if args.seed is not None:
        body["seed"] = args.seed
    if args.change_request:
        body["change_request"] = args.change_request
    label = run_label(args)
    if label:
        body["label"] = label
    if args.staff:
        body["team_id"] = args.team_id
        if args.business_date:
            body["business_date"] = args.business_date
        if args.config_overrides_data is not None:
            body["config_overrides"] = args.config_overrides_data
        return call(hub, args.staff_key, "POST", "/v1/admin/runs", body)
    return call(hub, args.key, "POST", "/v1/runs", body)


class RunWatch:
    """The run's committed writes and its restart pause, through the control plane."""

    def __init__(self, hub: str, key: str, run_id: str) -> None:
        self.hub, self.key, self.run_id = hub, key, run_id

    def status(self) -> dict[str, Any]:
        return call(self.hub, self.key, "GET", f"/v1/runs/{self.run_id}")

    def pause(self, after_writes: int) -> dict[str, Any]:
        """From the `after_writes`-th committed write on, the hub answers every data-plane request of the run
        with 503 RUN_PAUSED and records nothing, until `resume()`."""
        return call(self.hub, self.key, "POST", f"/v1/runs/{self.run_id}/pause", {"after_writes": after_writes})

    def resume(self) -> dict[str, Any]:
        return call(self.hub, self.key, "POST", f"/v1/runs/{self.run_id}/resume")

    def paused(self) -> bool:
        return bool(self.status().get("paused"))


def start_process(argv: list[str], env: dict[str, str], json_mode: bool) -> subprocess.Popen[bytes]:
    """Start the solution in its own process group, so that it can be killed with all its children."""
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(argv, env=env, stdout=sys.stderr if json_mode else None, **options)


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the solution and every process it started (Windows: `taskkill /T`; elsewhere its process group),
    and wait until it is gone."""
    if process.poll() is None:
        if os.name == "nt":
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=30, check=False
                )
        else:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            process.kill()
    process.wait()


KEY_VARIABLES = ("BPA_KEY", "BPA_STAFF_KEY")


def solution_env(base_url: str) -> dict[str, str]:
    """The runner's environment for the solution, with BPA_BASE_URL set and without the keys: a solution needs
    only its base URL, and staff grading a team's code must not hand it the staff key."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in KEY_VARIABLES}
    env["BPA_BASE_URL"] = base_url
    return env


def run_solution(
    command: list[str], base_url: str, timeout: float, json_mode: bool, stop: Callable[[], bool] | None = None
) -> int | None:
    """Start the solution once and wait for it. Its exit code (124 on timeout), or None when `stop()` became
    true first and the runner killed it. RunnerError if it cannot start."""
    argv = [part.replace("{base_url}", base_url) for part in command]
    argv[0] = shutil.which(argv[0]) or argv[0]
    env = solution_env(base_url)
    log(f"$ {subprocess.list2cmdline(argv)}")
    started = time.monotonic()
    try:
        process = start_process(argv, env, json_mode)
    except OSError as err:
        raise RunnerError(f"cannot start {argv[0]!r}: {err}") from None
    try:
        while True:
            left = timeout - (time.monotonic() - started)
            if left <= 0:
                kill_tree(process)
                log(f"The solution did not finish within {timeout:.0f}s and was stopped.")
                return 124
            try:
                code = process.wait(timeout=min(POLL_SECONDS, left))
                break
            except subprocess.TimeoutExpired:
                pass
            if stop is not None and stop():
                kill_tree(process)
                log(f"Killed the solution and its child processes after {time.monotonic() - started:.1f}s.")
                return None
    except BaseException:  # Ctrl+C or a hub error while watching: never leave the solution running
        kill_tree(process)
        raise
    log(f"The solution exited with code {code} after {time.monotonic() - started:.1f}s.")
    return code


def run_starts(args: argparse.Namespace, command: list[str], base_url: str, watch: RunWatch | None) -> int:
    """Start the solution as many times as asked (a killed start first, with --kill-after-writes); the exit
    code of the first start that failed by itself, else 0."""
    point = args.kill_after_writes
    starts = args.invocations + (1 if point else 0)
    failed = 0
    number = 0
    if point:
        assert watch is not None
        number = 1
        watch.pause(point)
        log(f"Start 1 of {starts}: the solution is killed as soon as the run has {point} committed writes.")
        code = run_solution(command, base_url, args.timeout, args.json, stop=watch.paused)
        writes = watch.resume()["write_count"]
        if code is None:
            log(f"The run was paused at {writes} committed writes and is resumed now: a restart follows.")
        elif writes >= point:
            # It met RUN_PAUSED and stopped by itself, which API_CONVENTIONS.md allows: same as being killed.
            log(f"The solution stopped by itself at the kill point ({writes} committed writes): a restart follows.")
        else:
            log(f"The solution ended by itself (exit code {code}) with {writes} committed writes; kill point {point}.")
            failed = code
    for _ in range(args.invocations):
        number += 1
        if starts > 1:
            log(f"Start {number} of {starts}, on the same run.")
        code = run_solution(command, base_url, args.timeout, args.json)
        failed = failed or code or 0
    return failed


class EnterKey:
    """Enter presses, read by a helper thread so that the runner can watch the run meanwhile. The thread reads
    one line only when the runner waits for one, so normally no read is pending when the runner exits (a
    thread blocked on stdin at exit can crash the interpreter on Windows; see `pending_read`)."""

    pending_read = False  # a helper thread still waits for a line

    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        self._closed = False

    def _read_line(self) -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            line = ""
        self._lines.put(line)

    def pressed(self, timeout: float) -> bool:
        """True once Enter was pressed (or stdin closed) within `timeout` seconds."""
        if self._closed:
            return True
        if not EnterKey.pending_read:
            EnterKey.pending_read = True
            threading.Thread(target=self._read_line, daemon=True).start()
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return False
        EnterKey.pending_read = False
        self._closed = line == ""  # end of input
        return True

    def wait(self) -> None:
        while not self.pressed(0.5):
            pass


def manual_session(args: argparse.Namespace, base_url: str, watch: RunWatch | None) -> int:
    """--manual: print the base URL and let the person run the automation (with --kill-after-writes: tell them
    when to stop it and when to start it again)."""
    print(base_url, flush=True)
    point = args.kill_after_writes
    if not point:
        log("Run your automation against this BASE_URL, then press Enter to complete the run.")
        with contextlib.suppress(EOFError):
            input()
        return 0
    assert watch is not None
    watch.pause(point)
    enter = EnterKey()
    log(
        f"Start your automation against this BASE_URL. The hub pauses the run after {point} committed writes "
        "and this runner then tells you to stop it (press Enter if it finishes before that)."
    )
    while not watch.paused():
        if enter.pressed(0.5):
            break
    else:
        log(
            f"The run is paused after {point} committed writes: every request now answers 503 RUN_PAUSED and "
            "changes nothing. Stop your automation (as if it had crashed), then press Enter."
        )
        enter.wait()
    writes = watch.resume()["write_count"]
    log(
        f"The run is resumed with {writes} committed writes. Start your automation again on the same BASE_URL "
        "(a restart), and press Enter when it has finished to complete the run."
    )
    enter.wait()
    return 0


# Report output ---------------------------------------------------------------


def _short(value: Any, width: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 3] + "..."


def _percent(score: float, maximum: float) -> str:
    return f"{100 * score / maximum:.1f}%" if maximum else "-"


def format_report(report: dict[str, Any]) -> str:
    total = report["total"]
    lines = [
        f"Run {report['run_id']}  project {report['project']}  suite {report['suite']}"
        + (f" {report['change_request']}" if report.get("change_request") else "")
        + (f"  seed {report['seed']}  business date {report['business_date']}" if "seed" in report else ""),
        f"TOTAL {total['score']:.2f} / {total['max']:.2f}  ({_percent(total['score'], total['max'])})",
        "",
        f"{'Category':<16}{'Score':>8}{'Max':>8}{'Scenarios':>11}",
    ]
    for name, cat in report["categories"].items():
        lines.append(f"{name:<16}{cat['score']:>8.2f}{cat['max']:>8.2f}{cat['scenarios']:>11}")
    if report.get("visibility") == "summary":
        lines += ["", f"Only totals and categories are shown for the {report['suite']} suite."]
        return "\n".join(lines)

    lines += ["", f"{'Scenario':<10}{'Category':<16}{'Score':>8}{'Max':>8}  Title"]
    failed = []
    for scenario in report["scenarios"]:
        score = f"{scenario['score']:>8.2f}{scenario['max']:>8.2f}"
        lines.append(f"{scenario['id']:<10}{scenario['category']:<16}{score}  {scenario['title']}")
        failed += [(scenario["id"], check) for check in scenario["checks"] if not check["passed"]]
    if failed:
        lines += ["", "Failed checks:"]
        for scenario_id, check in failed:
            line = (
                f"  {scenario_id}  {check['name']}: expected {_short(check['expected'])}, got {_short(check['actual'])}"
            )
            if check.get("message"):
                line += f"  ({check['message']})"
            lines.append(line)
    return "\n".join(lines)


# CLI -------------------------------------------------------------------------


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    command: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(
        prog="bpa_runner.py",
        description="Run a solution against the BPA hub and print the report.",
        epilog="Put the solution command after `--`; {base_url} is replaced by the run's base URL.",
    )
    parser.add_argument("--hub", default=os.environ.get("BPA_HUB"), help="hub URL (default: $BPA_HUB)")
    parser.add_argument("--key", default=os.environ.get("BPA_KEY"), help="team key (default: $BPA_KEY)")
    parser.add_argument("--project", required=True, help="A, B, C or the slug")
    parser.add_argument("--suite", default="public", choices=["public", "sandbox", "private", "change-request"])
    parser.add_argument("--change-request", metavar="ID", help="with --suite change-request: which change request")
    parser.add_argument("--scenarios", help="comma-separated scenario ids (default: the whole suite)")
    parser.add_argument("--seed", type=int, help="sandbox seed (staff: any suite)")
    parser.add_argument("--manual", action="store_true", help="print the base URL and wait for Enter (UiPath)")
    parser.add_argument(
        "--local", action="store_true", help="staff only: start a development hub (needs the staff course repository)"
    )
    parser.add_argument("--timeout", type=float, default=900, help="seconds the solution may run (default 900)")
    parser.add_argument("--json", action="store_true", help="print the raw report JSON")
    parser.add_argument(
        "--staff-key", default=os.environ.get("BPA_STAFF_KEY"), help="staff key (default: $BPA_STAFF_KEY)"
    )
    parser.add_argument("--team-id", help="staff: the team the run is for")
    parser.add_argument("--business-date", help="staff: override the business date (YYYY-MM-DD)")
    parser.add_argument(
        "--config-overrides", metavar="FILE", help="staff: JSON file of config overrides (private, change-request)"
    )
    parser.add_argument(
        "--invocations", type=int, default=1, metavar="N", help="start the solution N times on the same run"
    )
    parser.add_argument(
        "--kill-after-writes",
        type=int,
        metavar="K",
        help="first start the solution once and kill it when the run has K committed writes, then restart it",
    )
    parser.add_argument("--label", help="a note stored with the run (restart options are added to it)")
    args = parser.parse_args(argv)

    args.staff = bool(args.team_id) or args.suite == "private" or (args.local and args.suite == "change-request")
    if args.change_request and args.suite != "change-request":
        parser.error("--change-request goes with --suite change-request")
    if args.suite == "change-request" and not args.staff and not args.change_request:
        parser.error("--suite change-request needs --change-request ID (a change request staff have released)")
    if args.local:
        args.key, args.staff_key = DEV_TEAM_KEY, DEV_STAFF_KEY
        args.team_id = args.team_id or (DEV_TEAM_ID if args.staff else None)
    elif not args.hub:
        parser.error("--hub (or BPA_HUB) is required unless --local is used")
    if args.staff and not (args.staff_key and args.team_id):
        parser.error("--team-id and the private suite need --staff-key (or BPA_STAFF_KEY) and --team-id")
    if not args.staff and not args.key:
        parser.error("--key (or BPA_KEY) is required")
    if args.business_date and not args.staff:
        parser.error("--business-date is for staff runs (with --team-id)")
    if args.invocations < 1:
        parser.error("--invocations must be at least 1")
    if args.kill_after_writes is not None and args.kill_after_writes < 1:
        parser.error("--kill-after-writes must be at least 1")
    if args.manual and args.invocations != 1:
        parser.error("--invocations needs a solution command; with --manual, start your automation yourself")
    args.config_overrides_data = None
    if args.config_overrides:
        if not args.staff or args.suite not in ("private", "change-request"):
            parser.error("--config-overrides is for staff runs of the private or change-request suite")
        try:
            args.config_overrides_data = json.loads(Path(args.config_overrides).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as err:
            parser.error(f"--config-overrides: cannot read {args.config_overrides}: {err}")
        if not isinstance(args.config_overrides_data, dict):
            parser.error("--config-overrides: the file must hold a JSON object")
    if not args.manual and not command:
        parser.error("give the solution command after `--`, or use --manual")
    return args, command


def exercise(args: argparse.Namespace, command: list[str], base_url: str, watch: RunWatch | None = None) -> int:
    """Run the solution (or wait for Enter in --manual mode); the exit code the runner will return."""
    try:
        if args.manual:
            return manual_session(args, base_url, watch)
        return run_starts(args, command, base_url, watch)
    except RunnerError as err:
        log(f"error: {err}")
        return 2
    except KeyboardInterrupt:
        log("Interrupted; completing the run anyway (Ctrl+C again leaves it open).")
        return 130


def main(argv: list[str] | None = None) -> int:
    utf8_output()
    args, command = parse_args(sys.argv[1:] if argv is None else argv)
    local: LocalHub | None = None
    try:
        if args.local:
            local = LocalHub()
            log(f"Starting a local hub on {local.url} ...")
            local.wait_until_healthy()
        hub = local.url if local else args.hub
        run = create_run(args, hub)
        details = [f"project {run['project']}", f"suite {run['suite']}"]
        if run.get("change_request"):
            details.append(f"change request {run['change_request']}")
        if "business_date" in run:
            details.append(f"business date {run['business_date']}")
        if "scenarios" in run:
            details.append(f"{len(run['scenarios'])} scenario(s)")
        log(f"Run {run['run_id']}: {', '.join(details)}.")
        log(f"BASE_URL = {run['base_url']}")
        key = args.staff_key if args.staff else args.key
        restarts = bool(args.kill_after_writes) or args.invocations != 1
        watch = RunWatch(hub, key, run["run_id"]) if restarts else None
        exit_code = exercise(args, command, run["base_url"], watch)
        if watch is not None:
            with contextlib.suppress(RunnerError):
                log(f"The run has {watch.status()['write_count']} committed writes.")
        report = call(hub, key, "POST", f"/v1/runs/{run['run_id']}/complete")
        # ASCII JSON (diacritics as \u escapes) survives any shell redirection; the table keeps them.
        print(json.dumps(report, indent=2) if args.json else format_report(report), flush=True)
        return exit_code
    except RunnerError as err:
        log(f"error: {err}")
        return 2
    except KeyboardInterrupt:
        log("Interrupted; the run was not completed.")
        return 130
    finally:
        if local:
            local.stop()


if __name__ == "__main__":
    exit_code = main()
    if EnterKey.pending_read:  # interrupted while waiting for Enter: skip the shutdown that would crash on it
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
    sys.exit(exit_code)
