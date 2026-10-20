"""Shared helpers for selecting and launching the Chromium browser backend.

Provides direct Chromium subprocess launching with stealth and performance flags.
仅支持 Windows 宿主（2026-10-20 起；Termux/Linux/Docker 路径已移除），
Windows 看护用 Job Object（KILL_ON_JOB_CLOSE），非 Windows 宿主只做无害兜底。
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("browser")


def _derive_stable_fingerprint_seed(key: str) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return 10000 + (int(digest[:8], 16) % 90000)


def find_chromium_executable() -> str:
    """Find the best Chromium executable available on the system."""
    # 1. Configured explicit path
    if (
        settings.browser_executable_path
        and Path(settings.browser_executable_path).exists()
    ):
        return settings.browser_executable_path

    project_root = Path(__file__).resolve().parents[4]
    # 2. Local project-scoped CloakBrowser (.cloakbrowser/**/chrome.exe)
    cloak_dir = project_root / ".cloakbrowser"
    if cloak_dir.is_dir():
        for pat in ("**/chrome.exe", "**/chrome"):
            local_matches = sorted(cloak_dir.glob(pat))
            for match in reversed(local_matches):
                if match.is_file() and (
                    os.access(match, os.X_OK) or platform.system() == "Windows"
                ):
                    return str(match)

    # 3. User-level CloakBrowser (~/.cloakbrowser/**/chrome.exe)
    user_cloak_dir = Path.home() / ".cloakbrowser"
    if user_cloak_dir.is_dir():
        for pat in ("**/chrome.exe", "**/chrome"):
            cloak_matches = sorted(user_cloak_dir.glob(pat))
            for match in reversed(cloak_matches):
                if match.is_file() and (
                    os.access(match, os.X_OK) or platform.system() == "Windows"
                ):
                    return str(match)

    # 4. Strictly CloakBrowser only. All fallbacks to Edge/Chrome/Brave/Chromium are removed!
    raise FileNotFoundError(
        "未检测到 CloakBrowser 浏览器。\n"
        "本项目仅支持 CloakBrowser，不支持 Edge 或标准 Chrome。\n"
        "请先安装 CloakBrowser（Windows 运行 scripts\\install-browser.bat），\n"
        "或设置 AISTUDIO_BROWSER_EXECUTABLE 环境变量指向自定义路径。"
    )


def build_chromium_args(
    port: int,
    user_data_dir: str | None = None,
    *,
    headless: bool | None = None,
    stable_fingerprint_key: str | None = None,
    proxy_url: str | None = None,
    timezone: str | None = None,
    locale: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build optimized mobile/stealth CLI arguments for direct Chromium launch."""
    is_headless = settings.browser_headless if headless is None else headless
    args: list[str] = [
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--in-process-gpu",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--renderer-process-limit=1",
        "--no-zygote",
        "--disable-breakpad",
        "--mute-audio",
        "--disable-site-isolation-trials",
        "--js-flags=--max-old-space-size=128 --expose-gc --optimize-for-size",
        "--disk-cache-size=16777216",
        "--media-cache-size=1",
        "--enable-low-end-device-mode",
        "--aggressive-cache-discard",
        "--disable-smooth-scrolling",
        "--disable-threaded-animation",
        "--disable-threaded-scrolling",
        "--disable-backing-store-limit",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-speech-api",
        "--disable-component-update",
        "--disable-default-apps",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=Translate,OptimizationHints,MediaRouter,DialLocalDiscovery,PreloadMediaEngagementData,CertificateTransparencyComponentUpdater,SitePerProcess,AudioServiceOutOfProcess,CalculateNativeWinOcclusion,InterestFeedContentSuggestions,BackForwardCache",
    ]
    if user_data_dir:
        args.append(f"--user-data-dir={user_data_dir}")

    if is_headless:
        args.extend(
            [
                "--headless=new",
                "--hide-scrollbars",
                "--window-size=1280,800",
            ]
        )
    else:
        args.extend(
            [
                "--start-maximized",
                "--ignore-gpu-blocklist",
            ]
        )

    effective_proxy = proxy_url or settings.proxy_url
    if effective_proxy:
        args.append(f"--proxy-server={effective_proxy}")

    fingerprint_seed = (
        _derive_stable_fingerprint_seed(stable_fingerprint_key)
        if stable_fingerprint_key
        else None
    )
    if fingerprint_seed is not None:
        args.append(f"--fingerprint={fingerprint_seed}")
    # 遵循 CloakBrowser 官方反爬指纹规范：统一伪装为受众最广、风控阈值最友好的
    # Windows 桌面指纹池
    args.append("--fingerprint-platform=windows")

    effective_tz = timezone or settings.timezone
    effective_locale = locale or settings.locale
    args.append(f"--fingerprint-timezone={effective_tz}")
    args.append(f"--fingerprint-locale={effective_locale}")

    if extra_args:
        args.extend(extra_args)
    return args


def _get_process_cmdline_windows(pid: int) -> str:
    """Retrieve command line for a Windows process using NtQueryInformationProcess with fallback."""
    if pid <= 1:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR),
            ]

        windll = getattr(ctypes, "windll", None)
        if not windll:
            return ""
        h = windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            return_length = wintypes.ULONG(0)
            status = windll.ntdll.NtQueryInformationProcess(
                h, 60, None, 0, ctypes.byref(return_length)
            )
            if return_length.value == 0:
                return ""
            buf = (ctypes.c_char * return_length.value)()
            status = windll.ntdll.NtQueryInformationProcess(
                h, 60, buf, return_length.value, ctypes.byref(return_length)
            )
            if status != 0:
                return ""
            us = ctypes.cast(buf, ctypes.POINTER(UNICODE_STRING)).contents
            return us.Buffer or ""
        finally:
            windll.kernel32.CloseHandle(h)
    except Exception:
        pass

    # Fallback to PowerShell if ctypes call fails
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            check=False,
            timeout=2.0,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def _get_process_list_windows() -> list[tuple[int, int, str]]:
    """Retrieve list of (pid, ppid, exe_name) using Win32 Toolhelp32 snapshot."""
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        windll = getattr(ctypes, "windll", None)
        if not windll:
            return []
        snap = windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1 or not snap:
            return []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)

        processes: list[tuple[int, int, str]] = []
        try:
            success = windll.kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while success:
                processes.append(
                    (
                        int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        str(entry.szExeFile),
                    )
                )
                success = windll.kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            windll.kernel32.CloseHandle(snap)
        return processes
    except Exception as e:
        log.debug("Toolhelp32 进程枚举异常: %s", e)
        return []


def _get_pid_listening_on_port_win32(port: int) -> int | None:
    """Find the process ID listening on a given TCP port using GetExtendedTcpTable in <1ms."""
    try:
        import ctypes
        from ctypes import wintypes

        AF_INET = 2
        TCP_TABLE_OWNER_PID_ALL = 5

        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("dwState", wintypes.DWORD),
                ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD),
            ]

        windll = getattr(ctypes, "windll", None)
        if not windll:
            return None
        dwSize = wintypes.DWORD(0)
        windll.iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(dwSize), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
        )
        if dwSize.value == 0:
            return None
        buf = (ctypes.c_char * dwSize.value)()
        res = windll.iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(dwSize), True, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
        )
        if res != 0:
            return None
        num_entries = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        p_rows = ctypes.cast(
            ctypes.byref(buf, 4),
            ctypes.POINTER(MIB_TCPROW_OWNER_PID * num_entries),
        ).contents
        for row in p_rows:
            row_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
            # MIB_TCP_STATE_LISTEN = 2
            if row.dwState == 2 and row_port == port:
                return int(row.dwOwningPid)
    except Exception as e:
        log.debug("GetExtendedTcpTable 查询失败: %s", e)
    return None


def _find_pid_listening_on_port_windows(port: int) -> int | None:
    """Find the process ID listening on a given TCP port on Windows."""
    # 1. Native Win32 API (<1ms)
    pid = _get_pid_listening_on_port_win32(port)
    if pid is not None:
        return pid

    # 2. Netstat fallback
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore"
        )
        import re

        pattern = re.compile(
            rf"^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):{port}\s+.*LISTENING\s+(\d+)",
            re.MULTILINE,
        )
        m = pattern.search(out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _is_active_api_server(pid: int) -> bool:
    """Check if a process is a live running AI Studio API server instance."""
    if pid <= 1:
        return False
    if pid in (os.getpid(), os.getppid()):
        return False
    if platform.system() == "Windows":
        cmd = _get_process_cmdline_windows(pid)
        return bool(
            cmd and any(k in cmd for k in ("main.py", "aistudio-api-server", "uvicorn"))
        )
    return False


def _is_port_in_use(port: int) -> bool:
    """Check if a TCP port on localhost is currently listening."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def cleanup_stale_chromium(
    port: int,
    user_data_dir: str | None = None,
    *,
    timeout_s: float = 3.0,
) -> list[int]:
    """Scan and terminate stale or orphaned Chromium / CloakBrowser processes.

    SAFETY GUARANTEE:
      Never terminates browser processes owned by a live active AI Studio API server.
      Only cleans up:
        1. True orphan processes (PPid == 1 or parent dead).
        2. Stale processes matching port/data-dir left behind from past crashes.
    Also cleans up stale lock files in user_data_dir and waits for port release.
    """
    if platform.system() != "Windows":
        return []

    cur_pid = os.getpid()
    parent_pid = os.getppid()
    candidate_pids: set[int] = set()

    proc_list = _get_process_list_windows()
    parent_map = {p[0]: p[1] for p in proc_list}

    # 1. Check if the target port is held by any listening process
    listener_pid = _find_pid_listening_on_port_windows(port)
    if (
        listener_pid
        and listener_pid not in (cur_pid, parent_pid)
        and listener_pid > 1
    ):
        # SAFETY GUARANTEE: Never touch listener if it or its parent is an active API server!
        listener_parent = parent_map.get(listener_pid, 0)
        if not _is_active_api_server(listener_pid) and not (
            listener_parent > 1 and _is_active_api_server(listener_parent)
        ):
            candidate_pids.add(listener_pid)

    # 2. Check running chrome.exe processes matching port or data dir
    for c_pid, c_ppid, exe_name in proc_list:
        if exe_name.lower() != "chrome.exe":
            continue
        if c_pid in (cur_pid, parent_pid) or c_pid <= 1:
            continue
        if c_ppid > 1 and _is_active_api_server(c_ppid):
            continue
        c_cmd = _get_process_cmdline_windows(c_pid)
        if f"--remote-debugging-port={port}" in c_cmd or (
            user_data_dir and user_data_dir in c_cmd
        ):
            candidate_pids.add(c_pid)

    killed_pids: list[int] = []
    if candidate_pids:
        log.info(
            "发现 %d 个遗留或孤儿浏览器进程待清理: %s",
            len(candidate_pids),
            sorted(candidate_pids),
        )
        for pid in sorted(candidate_pids):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
                killed_pids.append(pid)
            except Exception as e:
                log.debug("Windows taskkill 终止 PID %d 失败: %s", pid, e)

    # 3. Clean up stale Chromium lock files in user_data_dir
    if user_data_dir:
        udd_path = Path(user_data_dir)
        if udd_path.is_dir():
            for lock_name in (
                "SingletonLock",
                "SingletonCookie",
                "SingletonSocket",
                "DevToolsActivePort",
            ):
                lock_file = udd_path / lock_name
                if lock_file.is_symlink() or lock_file.exists():
                    try:
                        lock_file.unlink()
                        log.debug("已移除遗留锁文件: %s", lock_file)
                    except Exception as e:
                        log.debug("移除锁文件 %s 失败: %s", lock_file, e)

    # 4. Wait until port is verified free
    if _is_port_in_use(port):
        port_deadline = time.time() + timeout_s
        while time.time() < port_deadline and _is_port_in_use(port):
            time.sleep(0.1)

    return killed_pids


def _create_windows_job_object() -> int | None:
    """Create a Windows Job Object configured to kill all assigned processes on job close."""
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        kernel32 = win_dll("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception as e:
        log.debug("创建 Windows Job Object 失败: %s", e)
        return None


def _assign_process_to_job(job_handle: int | None, proc_handle: int) -> bool:
    """Assign child process to Windows Job Object for lifecycle watchdog protection."""
    if not job_handle or platform.system() != "Windows":
        return False
    try:
        import ctypes

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return False
        kernel32 = win_dll("kernel32", use_last_error=True)
        return bool(kernel32.AssignProcessToJobObject(job_handle, proc_handle))
    except Exception as e:
        log.debug("将进程分配至 Windows Job Object 失败: %s", e)
        return False


_ACTIVE_CHROMIUM_PROCESSES: set[ChromiumProcess] = set()


def _cleanup_all_chromium() -> None:
    """Terminate all active Chromium instances managed by this process."""
    for proc in list(_ACTIVE_CHROMIUM_PROCESSES):
        with contextlib.suppress(Exception):
            proc.terminate()


_cleanup_handlers_installed: bool = False


def install_process_cleanup_handlers() -> None:
    """Register atexit and signal handlers to guarantee browser termination on process exit."""
    global _cleanup_handlers_installed
    if _cleanup_handlers_installed:
        return
    _cleanup_handlers_installed = True

    atexit.register(_cleanup_all_chromium)

    sigbreak = getattr(signal, "SIGBREAK", None)
    signals_to_hook = [signal.SIGINT]
    if sigbreak is not None:
        signals_to_hook.append(sigbreak)
    for sig in signals_to_hook:
        try:
            prev_handler = signal.getsignal(sig)

            def _make_handler(prev: object):
                def _handler(signum: int, frame: object) -> None:
                    _cleanup_all_chromium()
                    if callable(prev) and prev not in (
                        signal.SIG_IGN,
                        signal.SIG_DFL,
                    ):
                        prev(signum, frame)
                    elif signum == signal.SIGINT:
                        raise KeyboardInterrupt
                    else:
                        sys.exit(128 + signum)

                return _handler

            signal.signal(sig, _make_handler(prev_handler))
        except (ValueError, AttributeError, TypeError):
            pass


class ChromiumProcess:
    """Manages lifecycle of a direct Chromium subprocess (Windows Job Object 看护)."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        port: int,
        user_data_dir: str | None = None,
        *,
        job: int | None = None,
    ):
        self.process = process
        self.port = port
        self.user_data_dir = user_data_dir
        self.job = job
        _ACTIVE_CHROMIUM_PROCESSES.add(self)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self, timeout_s: float = 3.0) -> None:
        """Terminate the Chromium subprocess tree safely."""
        _ACTIVE_CHROMIUM_PROCESSES.discard(self)

        # Close Windows Job Object if present (kills job processes on handle close)
        if self.job is not None:
            try:
                import ctypes

                win_dll = getattr(ctypes, "WinDLL", None)
                if win_dll is not None:
                    kernel32 = win_dll("kernel32", use_last_error=True)
                    kernel32.CloseHandle(self.job)
            except Exception:
                pass
            self.job = None

        if self.process.poll() is not None:
            return

        try:
            self.process.terminate()
            self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True,
                    check=False,
                )
            except Exception:
                self.process.kill()
        except Exception as e:
            log.debug("关闭 Chromium 异常: %s", e)
            with contextlib.suppress(Exception):
                self.process.kill()


def launch_chromium_process(
    port: int,
    user_data_dir: str | None = None,
    *,
    headless: bool | None = None,
    extra_args: list[str] | None = None,
) -> ChromiumProcess:
    """Launch Chromium binary directly as a subprocess with watchdog protection."""
    install_process_cleanup_handlers()

    # Clean up any stale or orphaned browser processes on port before launching
    cleanup_stale_chromium(port=port, user_data_dir=user_data_dir)

    executable = find_chromium_executable()
    if user_data_dir:
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    args = build_chromium_args(
        port=port,
        user_data_dir=user_data_dir,
        headless=headless,
        stable_fingerprint_key=user_data_dir,
        timezone=settings.timezone,
        locale=settings.locale,
        extra_args=extra_args,
    )
    cmd = [executable, *args]
    log.info("正在启动 CloakBrowser 进程: %s (调试端口 %d)", executable, port)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    job = None
    if platform.system() == "Windows":
        job = _create_windows_job_object()
        proc_handle = getattr(proc, "_handle", None)
        if job and proc_handle:
            _assign_process_to_job(job, int(proc_handle))
    return ChromiumProcess(proc, port, user_data_dir, job=job)
