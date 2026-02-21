#!/usr/bin/env python3
"""Standalone crab tracker — monitors Claude Code instances with animated crabs.

Zero dependencies beyond Python 3.11+ stdlib.
"""

from __future__ import annotations

import curses
import math
import os
import random
import re
import time
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Constants & animation frames
# ---------------------------------------------------------------------------

CRAB_WIDTH = 10
HEADER_LINES = 3
CPU_THRESHOLD = 5.0
SCAN_INTERVAL = 3.0
FRAME_INTERVAL = 0.1

WALK_FRAMES = [
    [
        " ▐▛███▜▌  ",
        "▝▜█████▛▘ ",
        "  ▘▘ ▝▝   ",
    ],
    [
        " ▐▛███▜▌  ",
        "▝▜█████▛▘ ",
        " ▘▘   ▝▝  ",
    ],
    [
        " ▐▛███▜▌  ",
        "▝▜█████▛▘ ",
        "  ▝▝ ▘▘   ",
    ],
    [
        " ▐▛███▜▌  ",
        "▝▜█████▛▘ ",
        " ▘▘   ▝▝  ",
    ],
]

SLEEP_FRAMES = [
    [
        "          ",
        "   z      ",
        " ▐█████▌  ",
        "▝▜█████▛▘ ",
        "  ▘▘ ▝▝   ",
    ],
    [
        "      z   ",
        "     Z    ",
        " ▐█████▌  ",
        "▝▜█████▛▘ ",
        "  ▘▘ ▝▝   ",
    ],
]

WAITING_CRAB = [
    " ▐▛███▜▌  ",
    "▝▜█████▛▘ ",
    "  ▘▘ ▝▝   ",
]

COLOR_NAMES = ["red", "green", "yellow", "blue", "magenta", "cyan", "white"]

_COLOR_MAP = {
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}


# ---------------------------------------------------------------------------
# ProcessInfo + ProcessScanner — detect Claude instances via /proc
# ---------------------------------------------------------------------------

@dataclass
class ProcessInfo:
    pid: int
    type: str  # "cli", "vs", "runner"
    cpu_pct: float
    cwd: str
    branch: str


class ProcessScanner:
    """Scans /proc for running Claude Code instances."""

    def __init__(self) -> None:
        self.own_pid = os.getpid()
        self._prev_samples: dict[int, tuple[int, float]] = {}
        self._clk_tck = os.sysconf("SC_CLK_TCK")

    def scan(self) -> list[ProcessInfo]:
        results: list[ProcessInfo] = []
        seen_pids: set[int] = set()

        try:
            entries = os.listdir("/proc")
        except OSError:
            return results

        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == self.own_pid:
                continue

            try:
                cmdline = self._read_cmdline(pid)
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue

            proc_type = self._classify(cmdline, pid)
            if proc_type is None:
                continue

            cpu_pct = self._measure_cpu(pid)
            cwd = self._read_cwd(pid)
            branch = self._read_branch(cwd)
            results.append(ProcessInfo(
                pid=pid, type=proc_type, cpu_pct=cpu_pct, cwd=cwd, branch=branch,
            ))
            seen_pids.add(pid)

        # Purge stale samples
        for pid in list(self._prev_samples):
            if pid not in seen_pids:
                del self._prev_samples[pid]

        return results

    def _read_cmdline(self, pid: int) -> str:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    def _classify(self, cmdline: str, pid: int = 0) -> str | None:
        if not cmdline:
            return None
        if ".vscode-server/extensions/anthropic.claude-code" in cmdline:
            return "vs"
        if re.search(r"(^|/)claude(\s|$)", cmdline):
            if any(skip in cmdline for skip in ("pgrep", "grep", "crab_tracker", "crab-tracker", "crab_canvas", "crab.py")):
                return None
            if pid and self._is_runner_parent(pid):
                return "runner"
            return "cli"
        return None

    def _read_ppid(self, pid: int) -> int:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        return int(line.split()[1])
        except (FileNotFoundError, PermissionError, ProcessLookupError,
                IndexError, ValueError, OSError):
            pass
        return 0

    def _is_runner_parent(self, pid: int) -> bool:
        current = pid
        for _ in range(5):
            ppid = self._read_ppid(current)
            if ppid <= 1:
                break
            try:
                parent_cmd = self._read_cmdline(ppid)
                if "runner" in parent_cmd and "work" in parent_cmd:
                    return True
                if "tmux" in parent_cmd:
                    return True
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                break
            current = ppid
        try:
            cwd = self._read_cwd(pid)
            if cwd != "?" and os.path.exists(os.path.join(cwd, ".runner-marker")):
                return True
        except OSError:
            pass
        return False

    def _read_cwd(self, pid: int) -> str:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            return "?"

    def _read_branch(self, cwd: str) -> str:
        if cwd == "?":
            return ""
        path = cwd
        for _ in range(20):
            head = os.path.join(path, ".git", "HEAD")
            try:
                with open(head) as f:
                    content = f.read().strip()
                if content.startswith("ref: refs/heads/"):
                    return content[16:]
                return content[:12]
            except (FileNotFoundError, PermissionError, OSError):
                parent = os.path.dirname(path)
                if parent == path:
                    break
                path = parent
        return ""

    def _measure_cpu(self, pid: int) -> float:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat_raw = f.read()
            close_paren = stat_raw.rfind(")")
            remainder = stat_raw[close_paren + 2:].split()
            utime = int(remainder[11])
            stime = int(remainder[12])
        except (FileNotFoundError, PermissionError, ProcessLookupError,
                IndexError, ValueError, OSError):
            return 0.0

        total_ticks = utime + stime
        now = time.monotonic()
        prev = self._prev_samples.get(pid)
        self._prev_samples[pid] = (total_ticks, now)

        if prev is None:
            return 0.0

        prev_ticks, prev_time = prev
        dt = now - prev_time
        if dt <= 0:
            return 0.0

        return (total_ticks - prev_ticks) / self._clk_tck / dt * 100.0


# ---------------------------------------------------------------------------
# CrabEntity — one animated crab per Claude instance
# ---------------------------------------------------------------------------

@dataclass
class CrabEntity:
    pid: int
    proc_type: str
    color: str
    active: bool = False
    cpu_pct: float = 0.0
    cwd: str = "?"
    branch: str = ""

    walk_frame: int = 0
    sleep_frame: int = 0
    tick: int = 0
    just_slept: bool = False

    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    scr_w: int = 80
    scr_h: int = 24

    def __post_init__(self) -> None:
        play_w = max(1, self.scr_w - CRAB_WIDTH)
        play_h = max(1, self.scr_h - HEADER_LINES - 8)
        self.x = random.uniform(1, play_w)
        self.y = random.uniform(HEADER_LINES + 1, HEADER_LINES + 1 + play_h)
        angle = random.uniform(0, 2 * math.pi)
        speed = random.uniform(0.3, 0.7)
        self.dx = math.cos(angle) * speed
        self.dy = math.sin(angle) * speed

    def set_active(
        self,
        active: bool,
        cpu_pct: float,
        cwd: str | None = None,
        branch: str | None = None,
    ) -> None:
        was = self.active
        self.active = active
        self.cpu_pct = cpu_pct
        if cwd is not None:
            self.cwd = cwd
        if branch is not None:
            self.branch = branch
        if active and not was:
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(0.3, 0.7)
            self.dx = math.cos(angle) * speed
            self.dy = math.sin(angle) * speed
            self.just_slept = False
        if not active and was:
            self.just_slept = True
        elif not (not active and was):
            self.just_slept = False

    def update(self) -> None:
        self.tick += 1
        if self.active:
            self._walk()
        else:
            self._sleep()

    def _walk(self) -> None:
        if self.tick % 3 == 0:
            self.walk_frame = (self.walk_frame + 1) % len(WALK_FRAMES)
        self.x += self.dx
        self.y += self.dy
        crab_h = len(WALK_FRAMES[0]) + 2
        min_y = HEADER_LINES
        max_y = self.scr_h - crab_h - 1
        max_x = self.scr_w - CRAB_WIDTH
        if self.x < 0:
            self.x = 0
            self.dx = abs(self.dx)
            self._jitter()
        elif self.x > max_x:
            self.x = max_x
            self.dx = -abs(self.dx)
            self._jitter()
        if self.y < min_y:
            self.y = min_y
            self.dy = abs(self.dy)
            self._jitter()
        elif self.y > max_y:
            self.y = float(max(min_y, max_y))
            self.dy = -abs(self.dy)
            self._jitter()
        if random.random() < 0.02:
            self._jitter()

    def _sleep(self) -> None:
        if self.tick % 5 == 0:
            self.sleep_frame = (self.sleep_frame + 1) % len(SLEEP_FRAMES)

    def _jitter(self) -> None:
        speed = max(0.3, min(0.7, math.hypot(self.dx, self.dy)))
        angle = math.atan2(self.dy, self.dx) + random.uniform(-0.4, 0.4)
        self.dx = math.cos(angle) * speed
        self.dy = math.sin(angle) * speed

    def update_bounds(self, scr_w: int, scr_h: int) -> None:
        self.scr_w = scr_w
        self.scr_h = scr_h
        max_x = scr_w - CRAB_WIDTH
        max_y = scr_h - len(SLEEP_FRAMES[1]) - 2
        self.x = max(0, min(self.x, max_x))
        self.y = max(HEADER_LINES, min(self.y, max_y))

    def get_lines(self) -> list[str]:
        if self.active:
            return WALK_FRAMES[self.walk_frame]
        return SLEEP_FRAMES[self.sleep_frame]

    def label(self) -> str:
        tag = self.proc_type.upper()
        return f"[{tag}:{self.pid}] {self.cpu_pct:4.1f}%"

    def dir_label(self) -> str:
        home = os.path.expanduser("~")
        cwd = self.cwd
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        return cwd


# ---------------------------------------------------------------------------
# Curses rendering
# ---------------------------------------------------------------------------

def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLUE, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_CYAN, -1)
    curses.init_pair(7, curses.COLOR_WHITE, -1)


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    max_len = w - x
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


def _draw_header(stdscr, total: int, active: int, idle: int) -> None:
    h, w = stdscr.getmaxyx()
    header = " CRAB RAVE "
    header_line = header.center(w)
    _safe_addstr(stdscr, 0, 0, header_line, curses.A_BOLD | curses.A_REVERSE)

    stats = f" Instances: {total}  |  Active: {active}  |  Idle: {idle}  |  'q' quit "
    _safe_addstr(stdscr, 1, 0, stats.ljust(w), curses.color_pair(4))


def _draw_waiting(stdscr) -> None:
    h, w = stdscr.getmaxyx()
    cy = max(3, h // 2 - 3)
    cx = max(0, (w - CRAB_WIDTH) // 2)

    for i, line in enumerate(WAITING_CRAB):
        _safe_addstr(stdscr, cy + i, cx, line)

    msg = "No Claude instances detected... waiting"
    msg_x = max(0, (w - len(msg)) // 2)
    _safe_addstr(stdscr, cy + len(WAITING_CRAB) + 1, msg_x, msg, curses.A_DIM)


def _draw_crab(stdscr, crab: CrabEntity) -> None:
    pair = _COLOR_MAP.get(crab.color, 7)
    attr = curses.color_pair(pair)

    ix = int(round(crab.x))
    iy = int(round(crab.y))

    lines = crab.get_lines()

    if crab.branch:
        ball = f"( {crab.branch} )"
        bx = ix + CRAB_WIDTH
        _safe_addstr(stdscr, iy, bx, ball, curses.A_DIM)

    for offset, line in enumerate(lines):
        _safe_addstr(stdscr, iy + offset, ix, line, attr | curses.A_BOLD)

    lbl = crab.label()
    lbl_y = iy + len(lines)
    _safe_addstr(stdscr, lbl_y, ix, lbl, attr)

    dir_lbl = crab.dir_label()
    _safe_addstr(stdscr, lbl_y + 1, ix, dir_lbl, curses.A_DIM)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(FRAME_INTERVAL * 1000))
    _init_colors()

    scanner = ProcessScanner()
    crabs: dict[int, CrabEntity] = {}
    next_color = 0
    last_scan = 0.0

    while True:
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch in (ord("q"), ord("Q"), 27):
            break

        now = time.monotonic()
        h, w = stdscr.getmaxyx()

        if now - last_scan >= SCAN_INTERVAL:
            last_scan = now
            results = scanner.scan()
            current: set[int] = set()

            for info in results:
                current.add(info.pid)
                is_active = info.cpu_pct > CPU_THRESHOLD

                if info.pid in crabs:
                    crabs[info.pid].set_active(is_active, info.cpu_pct, info.cwd, info.branch)
                else:
                    color = COLOR_NAMES[next_color % len(COLOR_NAMES)]
                    next_color += 1
                    crab = CrabEntity(
                        pid=info.pid,
                        proc_type=info.type,
                        color=color,
                        scr_w=w,
                        scr_h=h,
                    )
                    crab.set_active(is_active, info.cpu_pct, info.cwd, info.branch)
                    crabs[info.pid] = crab

            for pid in list(crabs):
                if pid not in current:
                    del crabs[pid]

        active_count = 0
        idle_count = 0
        for crab in crabs.values():
            crab.update()
            crab.update_bounds(w, h)
            if crab.active:
                active_count += 1
            else:
                idle_count += 1

        stdscr.erase()
        _draw_header(stdscr, len(crabs), active_count, idle_count)

        if not crabs:
            _draw_waiting(stdscr)
        else:
            for crab in crabs.values():
                _draw_crab(stdscr, crab)

        stdscr.refresh()


if __name__ == "__main__":
    curses.wrapper(_main)
