#!/usr/bin/env python3
"""
command-enumerator

Run a command template against every word in a wordlist, substituting the
literal token [[ENUM]] with each word. Commands run in parallel (configurable
concurrency), their live output streams to the terminal, and when a command
finishes:
  - if it succeeded, it's promoted to a persistent "successful" list
  - if it failed, its live panel is cleared without a trace

At the end, a summary of all successful words/commands is printed.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ENUM_TOKEN = "[[ENUM]]"


@dataclass
class TaskState:
    word: str
    argv: list[str]
    lines: deque = field(default_factory=lambda: deque())
    status: str = "running"  # running | success | failed | timeout
    returncode: Optional[int] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append_line(self, line: str, tail_lines: int) -> None:
        with self.lock:
            self.lines.append(line)
            while len(self.lines) > tail_lines:
                self.lines.popleft()


class Throttle:
    """Rate-limits command launches across all worker threads.

    Guarantees that successive launches are spaced at least `delay` seconds
    apart, regardless of how many threads are running concurrently. This
    prevents the enumerator from hammering the target command/system even
    when --concurrency is high.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.last: Optional[float] = None

    def wait(self) -> None:
        if self.delay <= 0:
            return
        with self.lock:
            now = time.monotonic()
            if self.last is not None:
                elapsed = now - self.last
                remaining = self.delay - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            self.last = time.monotonic()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="command-enumerator",
        description="Run a command against every word in a wordlist, substituting [[ENUM]].",
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command template containing the %s placeholder, e.g. 'ping %s'" % (ENUM_TOKEN, ENUM_TOKEN),
    )
    parser.add_argument(
        "--wordlist",
        required=True,
        type=Path,
        help="Path to a newline-separated wordlist file",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of commands to run in parallel (default: 1)",
    )
    parser.add_argument(
        "--success-mode",
        choices=["exit-code", "exit-and-stderr"],
        default="exit-code",
        help=(
            "How to determine success: 'exit-code' (return code 0) or "
            "'exit-and-stderr' (return code 0 AND empty stderr). Default: exit-code"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional per-command timeout in seconds. A timeout counts as failure.",
    )
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=10,
        help="Number of trailing output lines to show per live command panel (default: 10)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=(
            "Minimum delay in seconds between successive command launches, to avoid "
            "overwhelming the target command/system (default: 0, no delay)"
        ),
    )
    return parser.parse_args()


def load_wordlist(path: Path) -> list[str]:
    if not path.exists():
        print(f"error: wordlist not found: {path}", file=sys.stderr)
        sys.exit(1)
    words = []
    for raw in path.read_text().splitlines():
        word = raw.strip()
        if word:
            words.append(word)
    if not words:
        print(f"error: wordlist is empty: {path}", file=sys.stderr)
        sys.exit(1)
    return words


def build_argv(command_template: str, word: str) -> list[str]:
    try:
        tokens = shlex.split(command_template)
    except ValueError as exc:
        print(f"error: could not parse --command: {exc}", file=sys.stderr)
        sys.exit(1)
    if ENUM_TOKEN not in tokens:
        print(f"error: --command must contain the {ENUM_TOKEN} placeholder", file=sys.stderr)
        sys.exit(1)
    return [word if tok == ENUM_TOKEN else tok for tok in tokens]


def run_task(
    state: TaskState,
    timeout: Optional[float],
    success_mode: str,
    tail_lines: int,
) -> None:
    stderr_buf: list[str] = []
    proc = None
    try:
        proc = subprocess.Popen(
            state.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        state.append_line("error: command not found", tail_lines)
        state.status = "failed"
        return
    except Exception as exc:  # noqa: BLE001
        state.append_line(f"error: {exc}", tail_lines)
        state.status = "failed"
        return

    def pump(stream, collect_stderr: bool) -> None:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n")
            if line:
                state.append_line(line, tail_lines)
                if collect_stderr:
                    stderr_buf.append(line)
        stream.close()

    t_out = threading.Thread(target=pump, args=(proc.stdout, False), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, True), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        state.status = "timeout"
        state.returncode = None
        t_out.join(timeout=1)
        t_err.join(timeout=1)
        return

    t_out.join(timeout=2)
    t_err.join(timeout=2)
    state.returncode = proc.returncode

    if success_mode == "exit-code":
        success = proc.returncode == 0
    else:  # exit-and-stderr
        success = proc.returncode == 0 and not stderr_buf

    state.status = "success" if success else "failed"


def render(
    tasks: list[TaskState],
    successes: list[TaskState],
    failures: list[TaskState],
    done_count: int,
    total: int,
) -> Table:
    table = Table.grid(expand=True)

    header = Text(f"Running {done_count}/{total} done", style="bold")
    table.add_row(header)

    for t in tasks:
        if t.status != "running":
            continue
        with t.lock:
            body = "\n".join(t.lines) if t.lines else "(no output yet)"
        table.add_row(Panel(body, title=t.word, border_style="yellow"))

    if successes:
        succ_lines = "\n".join(f"[green]\u2713[/green] {s.word}" for s in successes)
        table.add_row(Panel(succ_lines, title="Successful", border_style="green"))

    if failures:
        fail_lines = "\n".join(f"[red]\u2717[/red] {f.word} ({f.status})" for f in failures)
        table.add_row(Panel(fail_lines, title="Failed", border_style="red"))

    return table


def main() -> None:
    args = parse_args()
    words = load_wordlist(args.wordlist)

    tasks = [TaskState(word=w, argv=build_argv(args.command, w)) for w in words]

    console = Console()
    successes: list[TaskState] = []
    failures: list[TaskState] = []
    completed = 0
    completed_lock = threading.Lock()
    throttle = Throttle(args.delay)

    def worker(state: TaskState) -> None:
        nonlocal completed
        throttle.wait()
        run_task(state, args.timeout, args.success_mode, args.tail_lines)
        with completed_lock:
            completed += 1
            if state.status == "success":
                successes.append(state)
            else:
                failures.append(state)

    with Live(console=console, refresh_per_second=10, transient=False) as live:
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(worker, t) for t in tasks]

            while any(not f.done() for f in futures):
                with completed_lock:
                    done = completed
                    succ_snapshot = list(successes)
                    fail_snapshot = list(failures)
                live.update(render(tasks, succ_snapshot, fail_snapshot, done, len(tasks)))
                time.sleep(0.1)

            live.update(render(tasks, successes, failures, completed, len(tasks)))

    console.print()
    console.print(
        f"[bold]Summary: {len(successes)}/{len(tasks)} succeeded, "
        f"{len(failures)}/{len(tasks)} failed[/bold]"
    )
    if successes:
        console.print("\n[bold green]Successful:[/bold green]")
        for s in successes:
            cmd_str = " ".join(shlex.quote(a) for a in s.argv)
            console.print(f"  - {s.word}   ({cmd_str})")
    else:
        console.print("[yellow]No successful commands.[/yellow]")

    if failures:
        console.print("\n[bold red]Failed:[/bold red]")
        for f in failures:
            cmd_str = " ".join(shlex.quote(a) for a in f.argv)
            console.print(f"  - {f.word}   ({f.status}, {cmd_str})")


if __name__ == "__main__":
    main()
