#!/usr/bin/env python3

"""PS4 Remote PKG Installer client. Keep the PS4 app in focus during install."""
""" Do you want to complain? twili8t [[at]] proton {.} me """

import argparse
import concurrent.futures
import functools
import http.server
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

PS4_PORT = 12800
DEFAULT_LOCAL_PORT = 8080
DEFAULT_TIMEOUT = 15
CONFIG_PATH = os.path.expanduser("~/.config/ps4_rpi_client.json")

SUB_TYPES = {"Game": 6, "AC": 7, "Patch": 8, "License": 9}
_TASK_OPS = {"p": "pause", "r": "resume", "s": "start", "x": "stop", "u": "unregister"}
CONTENT_TYPES = {0x1A: "GD(game/patch)", 0x1B: "AC/theme", 0x1C: "AL(ac-nodata)", 0x1E: "DP(delta)"}
PKG_MAGIC = b"\x7fCNT"
PKG_HDR_SIZE = 0x2000
PATCH_FLAGS = (0x00100000, 0x40000000, 0x41000000, 0x60000000)

TITLE_RE = re.compile(r"^[A-Z]{4}\d{5}$")          # e.g. CUSA02299
_HEXNUM_RE = re.compile(r":\s*0x([0-9A-Fa-f]+)")

log = deque(maxlen=50)


def ts():
    return time.strftime("%H:%M:%S")


def say(msg):
    line = f"[{ts()}] {msg}"
    log.append(line)
    print(line)


class PS4Error(Exception):
    def __init__(self, msg, code=None, http_status=None):
        super().__init__(msg)
        self.code = code
        self.http_status = http_status

    def __str__(self):
        s = super().__str__()
        if self.code is not None:
            s += f" (PS4 error_code 0x{self.code & 0xFFFFFFFF:08X})"
        return s


NET_HINT = ("PS4 unreachable. Is the Remote PKG Installer app OPEN and IN FOCUS on the PS4? "
            "The PS4 suspends its network when the app is in background. Also check IP/port.")


def parse_ps4_json(text):
    """Server sends bare 0x ints, not valid JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = _HEXNUM_RE.sub(lambda m: ": %d" % int(m.group(1), 16), text)
        return json.loads(fixed)  # raises with context if still bad


def check_url(u):
    if not isinstance(u, str) or not u.strip():
        raise PS4Error("empty URL")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise PS4Error(f"URL must start with http:// or https:// : {u!r}")
    return u.strip()


def check_title_id(t):
    t = (t or "").strip().upper()
    if len(t) != 9 or not TITLE_RE.match(t):
        raise PS4Error(f"bad title_id {t!r}, expected like CUSA02299")
    return t


def check_content_id(c):
    c = (c or "").strip()
    if len(c) != 36:
        raise PS4Error(f"bad content_id length {len(c)}, expected 36: {c!r}")
    p1, p2 = c.find("-"), c.find("-", c.find("-") + 1)
    if p1 < 0 or p2 != 19 or "_" not in c[p1:p2] or len(c.rsplit("-", 1)[-1]) != 16:
        raise PS4Error(f"malformed content_id: {c!r}")
    return c


def check_task_id(t):
    try:
        t = int(t)
    except (TypeError, ValueError):
        raise PS4Error(f"task_id must be an integer, got {t!r}")
    if t < 0:
        raise PS4Error(f"task_id must be >= 0, got {t}")
    return t


def api_post(ps4_ip, endpoint, payload, timeout=DEFAULT_TIMEOUT):
    url = f"http://{ps4_ip}:{PS4_PORT}/api/{endpoint}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        status = e.code
        if not body:
            raise PS4Error(f"PS4 HTTP {status} with empty body on /{endpoint}", http_status=status)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise PS4Error(f"{NET_HINT} ({e})")
    try:
        resp = parse_ps4_json(body)
    except json.JSONDecodeError:
        raise PS4Error(f"PS4 returned non-JSON on /{endpoint} (HTTP {status}): {body[:200]!r}",
                       http_status=status)
    if not isinstance(resp, dict) or resp.get("status") != "success":
        err = resp.get("error", "?") if isinstance(resp, dict) else "?"
        code = resp.get("error_code")
        raise PS4Error(f"PS4 refused /{endpoint}: {err}", code=code, http_status=status)
    return resp


def api_install_direct(ps4_ip, urls, timeout=60):
    urls = [check_url(u) for u in urls]
    if not urls:
        raise PS4Error("no packages given")
    return api_post(ps4_ip, "install", {"type": "direct", "packages": urls}, timeout=timeout)


def api_install_ref(ps4_ip, url, timeout=60):
    return api_post(ps4_ip, "install", {"type": "ref_pkg_url", "url": check_url(url)}, timeout=timeout)


def api_is_exists(ps4_ip, title_id, timeout=DEFAULT_TIMEOUT):
    r = api_post(ps4_ip, "is_exists", {"title_id": check_title_id(title_id)}, timeout=timeout)
    ex = r.get("exists")
    exists = (str(ex).lower() == "true") if not isinstance(ex, bool) else ex
    return exists, r.get("size")


def api_uninstall(ps4_ip, kind, ident, timeout=60):
    ep = {"game": "uninstall_game", "patch": "uninstall_patch",
          "ac": "uninstall_ac", "theme": "uninstall_theme"}[kind]
    key = "title_id" if kind in ("game", "patch") else "content_id"
    ident = check_title_id(ident) if key == "title_id" else check_content_id(ident)
    return api_post(ps4_ip, ep, {key: ident}, timeout=timeout)


def api_task(ps4_ip, action, task_id, timeout=DEFAULT_TIMEOUT):
    ep = {"start": "start_task", "stop": "stop_task", "pause": "pause_task",
          "resume": "resume_task", "unregister": "unregister_task"}[action]
    return api_post(ps4_ip, ep, {"task_id": check_task_id(task_id)}, timeout=timeout)


def api_progress(ps4_ip, task_id, timeout=DEFAULT_TIMEOUT):
    return api_post(ps4_ip, "get_task_progress", {"task_id": check_task_id(task_id)}, timeout=timeout)


def api_find_task(ps4_ip, content_id, sub_type, timeout=DEFAULT_TIMEOUT):
    try:
        sub_type = int(sub_type)
    except (TypeError, ValueError):
        if sub_type not in SUB_TYPES:
            raise PS4Error(f"unknown sub_type {sub_type!r}, pick from {list(SUB_TYPES)}")
        sub_type = SUB_TYPES[sub_type]
    return api_post(ps4_ip, "find_task",
                    {"content_id": check_content_id(content_id), "sub_type": int(sub_type)},
                    timeout=timeout)["task_id"]


def fmt_bytes(n):
    if n is None:
        return "?"
    n = int(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f}{u}" if u != "B" else f"{n}B"
        n /= 1024.0


def fmt_secs(s):
    s = int(s or 0)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def bar(frac, w=24):
    frac = max(0.0, min(1.0, frac or 0.0))
    n = int(frac * w)
    return "[" + "#" * n + "-" * (w - n) + f"] {frac * 100:5.1f}%"


def progress_line(p):
    lt, tt = int(p.get("length_total", 0) or 0), int(p.get("transferred_total", 0) or 0)
    l, t = int(p.get("length", 0) or 0), int(p.get("transferred", 0) or 0)
    base = bar(tt / lt) + f" {fmt_bytes(tt)}/{fmt_bytes(lt)}" if lt else f"{fmt_bytes(tt)} transferred"
    extra = f" piece {p.get('num_index', '?')}/{p.get('num_total', '?')}"
    if p.get("preparing_percent", -1) not in (None, -1) and int(p.get("preparing_percent") or 0) >= 0:
        extra += f" prep {p['preparing_percent']}%"
    if int(p.get("local_copy_percent", 0) or 0) > 0:
        extra += f" copy {p['local_copy_percent']}%"
    if int(p.get("rest_sec_total", 0) or 0) > 0:
        extra += f" ETA {fmt_secs(p['rest_sec_total'])}"
    if int(p.get("error_result", 0) or 0):
        extra += f" ERROR 0x{int(p['error_result']) & 0xFFFFFFFF:08X}"
    if l and l != lt:
        extra += f" (cur {fmt_bytes(t)}/{fmt_bytes(l)})"
    return base + extra


def inspect_pkg(path):
    with open(path, "rb") as f:
        hdr = f.read(PKG_HDR_SIZE)
    if len(hdr) < 0x450 or hdr[:4] != PKG_MAGIC:
        raise PS4Error(f"not a PS4 PKG (bad magic): {os.path.basename(path)}")
    content_id = hdr[0x40:0x40 + 0x25].split(b"\x00")[0].decode("ascii", "replace")
    (ctype,) = struct.unpack(">I", hdr[0x74:0x78])
    (flags,) = struct.unpack(">I", hdr[0x78:0x7C])
    (size,) = struct.unpack(">Q", hdr[0x430:0x438])
    return {"content_id": content_id, "content_type": CONTENT_TYPES.get(ctype, f"0x{ctype:02X}"),
            "is_patch": any(flags & f for f in PATCH_FLAGS),
            "size": size or os.path.getsize(path)}


def scan_pkgs(directory):
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        raise PS4Error(f"cannot list {directory}: {e}")
    for n in names:
        if n.lower().endswith(".pkg"):
            p = os.path.join(directory, n)
            if os.path.isfile(p):
                try:
                    info = inspect_pkg(p)
                except PS4Error as e:
                    info = {"content_id": f"<unreadable: {e}>", "content_type": "?",
                            "is_patch": False, "size": os.path.getsize(p)}
                info.update(name=n, path=p)
                out.append(info)
    return out


def split_siblings(first_path):
    d, base = os.path.dirname(first_path), os.path.basename(first_path)
    m = re.match(r"^(.*)_0\.pkg$", base, re.IGNORECASE)
    if not m:
        return [first_path]
    stem, parts, i = m.group(1), [], 0
    while True:
        p = os.path.join(d, f"{stem}_{i}.pkg")
        if not os.path.isfile(p):
            break
        parts.append(p)
        i += 1
        if i > 64:
            break
    return parts or [first_path]


def parse_range(header, size):
    m = re.match(r"bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m:
        return None
    a, b = m.groups()
    if a == "" and b == "":
        return None
    try:
        if a == "":
            start, end = max(0, size - int(b)), size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return start, end


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "PS4RPI/1.0"

    def log_message(self, *a):
        log.append(f"[{ts()}] http: {' '.join(str(x) for x in a)}")

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        if not os.path.isfile(path):
            self.send_error(404)
            return None
        size = os.path.getsize(path)
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(403)
            return None
        ctype = self.guess_type(path)
        rng = parse_range(self.headers.get("Range"), size)
        if rng:
            start, end = rng
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            f.seek(start)
            self._range_left = end - start + 1
            return f
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self._range_left = None
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_range_left", None)
        try:
            if left is None:
                return super().copyfile(source, outputfile)
            while left > 0:
                chunk = source.read(min(65536, left))
                if not chunk:
                    break
                outputfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            source.close()


class PkgServer:
    def __init__(self):
        self.httpd = self.thread = None
        self.port = None
        self.directory = None

    @property
    def running(self):
        return self.httpd is not None

    def start(self, directory, port, bind="0.0.0.0"):
        if self.running:
            raise PS4Error("file server already running")
        if not os.path.isdir(directory):
            raise PS4Error(f"serve dir missing: {directory}")
        handler = functools.partial(RangeHandler, directory=directory)
        srv = http.server.ThreadingHTTPServer((bind, port), handler)
        srv.daemon_threads = True
        self.port, self.directory = srv.server_address[1], directory
        self.thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                                       daemon=True)
        self.httpd = srv
        self.thread.start()
        try:
            try:
                first = next(n for n in sorted(os.listdir(directory))
                             if os.path.isfile(os.path.join(directory, n)))
            except StopIteration:
                first = None
            if first is None:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5)
            else:
                rq = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/{urllib.parse.quote(first)}",
                    headers={"Range": "bytes=0-0"})
                with urllib.request.urlopen(rq, timeout=5) as r:
                    if r.status != 206:
                        raise PS4Error(f"range probe got HTTP {r.status}, need 206")
        except urllib.error.HTTPError as e:
            self.stop()
            raise PS4Error(f"server self-check failed (HTTP {e.code})")
        except Exception as e:
            if isinstance(e, PS4Error):
                self.stop()
                raise
            self.stop()
            raise PS4Error(f"server self-check failed: {e}")

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            finally:
                self.httpd = self.thread = None

    def urls_for(self, paths):
        ip = lan_ip()
        return [f"http://{ip}:{self.port}/{urllib.parse.quote(os.path.basename(p))}" for p in paths]


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def verify_urls(urls):
    for u in urls:
        rq = urllib.request.Request(u, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(rq, timeout=10) as r:
            if r.status != 206:
                raise PS4Error(f"{u} answered HTTP {r.status}, need 206 Range")


def _probe_ps4(ip, port=PS4_PORT, timeout=0.6):
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/api/is_exists",
                                     data=b'{"title_id":"CUSA00000"}',
                                     headers={"Content-Type": "application/json", "Connection": "close"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return '"status"' in r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return '"status"' in e.read(2000).decode("utf-8", "replace")
        except Exception:
            return False
    except Exception:
        return False


def discover_ps4s(port=PS4_PORT, timeout=0.6):
    base = lan_ip()
    if base.startswith("127.") or "." not in base:
        raise PS4Error("cannot determine LAN, set PS4 IP manually")
    prefix = base.rsplit(".", 1)[0] + "."
    # wider masks when someone complains
    ips = [f"{prefix}{i}" for i in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, hit in zip(ips, ex.map(lambda ip: _probe_ps4(ip, port, timeout), ips)):
            if hit:
                found.append(ip)
    return found


def do_discover(cfg):
    print(f"scanning {lan_ip().rsplit('.', 1)[0]}.1-254:{PS4_PORT} (PS4 app must be open)...")
    try:
        found = discover_ps4s()
    except KeyboardInterrupt:
        print("\nscan interrupted.")
        return
    if not found:
        say("no PS4 found (is the installer app open and in focus?)")
        return
    for ip in found:
        say(f"found PS4 at {ip}")
    if len(found) == 1 and confirm(f"use {found[0]}?"):
        cfg["ps4_ip"] = found[0]
        save_config(cfg)
    elif len(found) > 1:
        v = prompt("use which (blank=keep current)")
        if v.strip() in found:
            cfg["ps4_ip"] = v.strip()
            save_config(cfg)


def load_config():
    cfg = {"ps4_ip": "", "serve_dir": os.getcwd(), "local_port": DEFAULT_LOCAL_PORT,
           "bind_ip": "0.0.0.0", "timeout": DEFAULT_TIMEOUT}
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        say(f"! could not save config: {e}")


class Quit(Exception):
    pass


def clear():
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    else:
        print("\n" + "=" * 60)


def prompt(msg, default=None):
    tag = f" [{default}]" if default not in (None, "") else ""
    try:
        v = input(f"{msg}{tag}: ").strip()
    except EOFError:
        raise Quit
    return v or (default if default is not None else "")


def ask_int(msg, default=None, lo=None, hi=None):
    while True:
        try:
            n = int(prompt(msg, default))
        except ValueError:
            print("  enter an integer"); continue
        if lo is not None and n < lo:
            print(f"  must be >= {lo}"); continue
        if hi is not None and n > hi:
            print(f"  must be <= {hi}"); continue
        return n


def confirm(msg):
    return prompt(msg + " [y/N]").lower() in ("y", "yes")


def need_ps4_ip(cfg):
    if cfg.get("ps4_ip"):
        return True
    ip = prompt("PS4 IPv4 (blank=scans LAN)")
    if not ip:
        do_discover(cfg)
        return bool(cfg.get("ps4_ip"))
    try:
        socket.inet_aton(ip)
    except OSError:
        say("! invalid IPv4")
        return False
    cfg["ps4_ip"] = ip
    save_config(cfg)
    say(f"PS4 IP remembered: {ip}")
    return True


def note_install(tasks, resp, label):
    tid = int(resp.get("task_id", -2))
    if tid < 0:
        say(f"PS4 reports package already installed (task_id={tid}). title={resp.get('title', '?')}")
        return None
    tasks[tid] = label
    say(f"install started: task_id={tid} title={resp.get('title', '?')!r}  (keep PS4 app in focus!)")
    return tid


def header(cfg, srv, tasks):
    print(" PS4 Remote PKG Installer by Twili8t [github.com/twili8, x.com/twili8t]!")
    print(" PS4 Remote PKG Installer client  (app must stay IN FOCUS on PS4 during install)")
    print(f" PS4: {cfg['ps4_ip'] or '(not set)'}:{PS4_PORT} | "
          f"server: {'ON ' + lan_ip() + ':' + str(srv.port) if srv.running else 'OFF'} "
          f"[{os.path.basename(cfg['serve_dir'])}] | tracked tasks: {len(tasks)}")
    print("-" * 78)


def ensure_server(cfg, srv):
    if srv.running:
        return True
    if not os.path.isdir(cfg["serve_dir"]):
        say(f"! serve dir missing: {cfg['serve_dir']}")
        return False
    try:
        srv.start(cfg["serve_dir"], int(cfg["local_port"]), cfg.get("bind_ip", "0.0.0.0"))
    except (PS4Error, OSError) as e:
        say(f"! cannot start file server on port {cfg['local_port']}: {e}")
        return False
    say(f"serving {cfg['serve_dir']} at http://{lan_ip()}:{srv.port}/")
    return True


def do_install_paths(cfg, srv, tasks, paths):
    for p in paths:
        if not os.path.isfile(p):
            say(f"! missing file: {p}")
            return
    if not need_ps4_ip(cfg):
        return
    if not ensure_server(cfg, srv):
        return
    urls = srv.urls_for(paths)
    try:
        verify_urls(urls)
    except Exception as e:
        say(f"! local server cannot serve: {e}")
        return
    names = ", ".join(os.path.basename(p) for p in paths)
    if not confirm(f"install {len(paths)} piece(s) [{names}]?"):
        return
    try:
        note_install(tasks, api_install_direct(cfg["ps4_ip"], urls, timeout=cfg["timeout"] * 4),
                     names)
    except PS4Error as e:
        say(f"! install failed: {e}")


def watch_task(cfg, tid):
    print(f"watching task {tid} every 2s (Ctrl-C to stop)...")
    try:
        while True:
            try:
                print("  " + progress_line(api_progress(cfg["ps4_ip"], tid, cfg["timeout"])))
            except PS4Error as e:
                print(f"  ! {e}")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("  stopped watching (task keeps running on PS4)")


def menu_pkgs(cfg, srv, tasks):
    try:
        pkgs = scan_pkgs(cfg["serve_dir"])
    except PS4Error as e:
        say(f"! {e}")
        return
    if not pkgs:
        say(f"! no .pkg files in {cfg['serve_dir']} (change dir in menu 1)")
        return
    print(f"\n -- PKGs in {cfg['serve_dir']} --")
    for i, p in enumerate(pkgs, 1):
        flag = " [PATCH]" if p["is_patch"] else ""
        print(f" {i:3d}. {p['name']}  {fmt_bytes(p['size']):>9}  {p['content_type']}{flag}\n"
              f"      {p['content_id']}")
    v = prompt("pick numbers (e.g. 1 or 1,2 or 3-5), 0=cancel", "0")
    idx = set()
    try:
        for part in v.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                idx.update(range(int(a), int(b) + 1))
            elif part:
                idx.add(int(part))
    except ValueError:
        say("! bad selection")
        return
    idx.discard(0)
    if not idx or any(i < 1 or i > len(pkgs) for i in idx):
        return
    paths = [pkgs[i - 1]["path"] for i in sorted(idx)]
    if len(paths) == 1 and re.search(r"_0\.pkg$", paths[0], re.IGNORECASE):
        sibs = split_siblings(paths[0])
        if len(sibs) > 1 and confirm(f"found {len(sibs)} split pieces, include all in order?"):
            paths = sibs
    do_install_paths(cfg, srv, tasks, paths)


def menu_tasks(cfg, tasks):
    if tasks:
        print("\n tracked tasks:")
        for tid, name in tasks.items():
            print(f"  {tid}: {name}")
    v = prompt("task_id (or blank to find by content_id)", "")
    if v == "":
        cid = prompt("content_id (36 chars)")
        print(" sub types: " + ", ".join(f"{k}={v_}" for k, v_ in SUB_TYPES.items()))
        st = prompt("sub_type", "Game")
        try:
            tid = api_find_task(cfg["ps4_ip"], cid, st, cfg["timeout"])
            say(f"found task_id={tid}")
            tasks.setdefault(tid, cid)
        except PS4Error as e:
            say(f"! {e}")
        return
    try:
        tid = check_task_id(v)
    except PS4Error as e:
        say(f"! {e}")
        return
    print(" a=progress once  w=watch  p=pause  r=resume  s=start  x=stop  u=unregister  q=back")
    op = prompt("op", "a").lower()
    if op == "q":
        return
    try:
        if op == "a":
            print("  " + progress_line(api_progress(cfg["ps4_ip"], tid, cfg["timeout"])))
        elif op == "w":
            watch_task(cfg, tid)
        elif op in _TASK_OPS:
            api_task(cfg["ps4_ip"], _TASK_OPS[op], tid, cfg["timeout"])
            say(f"task {tid}: ok")
            if op == "u":
                tasks.pop(tid, None)
        else:
            say("! unknown op")
    except PS4Error as e:
        say(f"! {e}")


def menu_remote_install(cfg, tasks):
    if not need_ps4_ip(cfg):
        return
    print(" d=direct PKG URL(s) — PS4 downloads straight from that server")
    print(" m=manifest .json URL (CDN way)")
    mode = prompt("which (d/m), q=back", "q").lower()
    try:
        if mode == "d":
            urls = [check_url(u.strip()) for u in prompt("PKG URL(s), comma-separated in order").split(",")
                    if u.strip()]
            if not urls:
                say("! no URLs given")
                return
            if not confirm(f"install {len(urls)} remote piece(s)?"):
                return
            note_install(tasks, api_install_direct(cfg["ps4_ip"], urls, timeout=cfg["timeout"] * 4),
                         ", ".join(urls))
        elif mode == "m":
            u = prompt("manifest .json URL")
            note_install(tasks, api_install_ref(cfg["ps4_ip"], u, timeout=cfg["timeout"] * 4), u)
        elif mode != "q":
            say("! pick d, m or q")
    except PS4Error as e:
        say(f"! {e}")


def menu_config(cfg, srv):
    print("\n -- config --")
    print(f" 1. PS4 IP      : {cfg['ps4_ip'] or '(unset)'}")
    print(f" 2. serve dir   : {cfg['serve_dir']}")
    print(f" 3. local port  : {cfg['local_port']}")
    print(f" 4. timeout (s) : {cfg['timeout']}")
    print(f" 5. bind IP     : {cfg.get('bind_ip', '0.0.0.0')}")
    print(f" 6. file server : {'ON :' + str(srv.port) if srv.running else 'OFF'} (toggle)")
    print(" 7. discover PS4 on LAN")
    print(" 0. back")
    c = prompt("choice", "0")
    if c == "1":
        ip = prompt("PS4 IPv4", cfg["ps4_ip"] or "")
        try:
            socket.inet_aton(ip)
            cfg["ps4_ip"] = ip
        except OSError:
            say("! invalid IPv4")
    elif c == "2":
        d = prompt("serve dir", cfg["serve_dir"])
        if os.path.isdir(d):
            cfg["serve_dir"] = os.path.abspath(d)
        else:
            say("! not a directory")
    elif c == "3":
        cfg["local_port"] = ask_int("local port", cfg["local_port"], 1, 65535)
        if srv.running:
            say("restart server for the new port to apply (toggle twice)")
    elif c == "4":
        cfg["timeout"] = ask_int("timeout s", cfg["timeout"], 3, 120)
    elif c == "5":
        cfg["bind_ip"] = prompt("bind IP", cfg.get("bind_ip", "0.0.0.0"))
        if srv.running:
            say("restart server for the new bind to apply (toggle twice)")
    elif c == "6":
        if srv.running:
            srv.stop()
            say("file server stopped")
        else:
            ensure_server(cfg, srv)
    elif c == "7":
        do_discover(cfg)
    save_config(cfg)


def menu_uninstall(cfg):
    if not need_ps4_ip(cfg):
        return
    print("\n kinds: game, patch (by title_id) | ac, theme (by content_id)")
    kind = prompt("kind", "game").lower()
    if kind not in ("game", "patch", "ac", "theme"):
        say("! unknown kind"); return
    ident = prompt("title_id or content_id")
    try:
        ident = check_title_id(ident) if kind in ("game", "patch") else check_content_id(ident)
    except PS4Error as e:
        say(f"! {e}"); return
    if not confirm(f"uninstall {kind} {ident}? (destructive on PS4)"):
        return
    try:
        api_uninstall(cfg["ps4_ip"], kind, ident, cfg["timeout"])
        say("uninstall ok")
    except PS4Error as e:
        say(f"! {e}")


def run_tui(cfg, srv):
    tasks = {}
    while True:
        clear()
        header(cfg, srv, tasks)
        print(" 1. config / file server")
        print(" 2. browse local PKGs + install")
        print(" 3. install from URL (direct PKG or manifest)")
        print(" 4. tasks (progress / pause / resume / ...)")
        print(" 5. check if installed (title_id)")
        print(" 6. uninstall")
        print(" 7. show log")
        print(" 0. quit")
        try:
            c = prompt("choice", "0")
        except Quit:
            c = "0"
        try:
            if c == "1":
                menu_config(cfg, srv)
            elif c == "2":
                menu_pkgs(cfg, srv, tasks)
            elif c == "3":
                menu_remote_install(cfg, tasks)
            elif c == "4":
                if need_ps4_ip(cfg):
                    menu_tasks(cfg, tasks)
            elif c == "5":
                if need_ps4_ip(cfg):
                    t = prompt("title_id (e.g. CUSA02299)")
                    try:
                        ex, size = api_is_exists(cfg["ps4_ip"], t, cfg["timeout"])
                        say(f"{t}: exists={ex}" + (f" size=0x{int(size):X} ({fmt_bytes(int(size))})"
                                                   if size is not None else ""))
                    except PS4Error as e:
                        say(f"! {e}")
            elif c == "6":
                menu_uninstall(cfg)
            elif c == "7":
                print("\n -- log --")
                for line in log:
                    print(" " + line)
            elif c == "0":
                if srv.running and confirm("stop file server and quit?"):
                    srv.stop()
                elif not srv.running:
                    pass
                else:
                    continue
                save_config(cfg)
                print("bye.")
                return
            else:
                say("! unknown choice")
        except Quit:
            save_config(cfg)
            print("\nbye.")
            return
        if c != "0":
            try:
                input("\n[Enter] ")
            except (EOFError, KeyboardInterrupt):
                print()
                continue


def headless(args, cfg, srv):
    if args.ps4_ip:
        cfg["ps4_ip"] = args.ps4_ip
    if args.bind_ip:
        cfg["bind_ip"] = args.bind_ip
    if not cfg["ps4_ip"]:
        sys.exit("error: --ps4-ip required (or set it in the TUI first)")
    if args.install:
        paths = []
        for p in args.install:
            p = os.path.abspath(p)
            if not os.path.isfile(p):
                sys.exit(f"error: missing file {p}")
            paths.append(p)
        if len(paths) == 1 and re.search(r"_0\.pkg$", paths[0], re.IGNORECASE):
            sibs = split_siblings(paths[0])
            if len(sibs) > 1:
                print(f"including {len(sibs)} split pieces in order")
                paths = sibs
        cfg["serve_dir"] = os.path.dirname(paths[0])
        if not ensure_server(cfg, srv):
            sys.exit("error: cannot start file server")
        urls = srv.urls_for(paths)
        try:
            verify_urls(urls)
        except Exception as e:
            sys.exit(f"error: local server cannot serve: {e}")
        r = api_install_direct(cfg["ps4_ip"], urls, timeout=cfg["timeout"] * 4)
        tid = int(r.get("task_id", -2))
        print(json.dumps(r))
        if tid < 0:
            print("already installed.")
        else:
            print(progress_line(api_progress(cfg["ps4_ip"], tid, cfg["timeout"])))
    elif args.url:
        try:
            urls = [check_url(u) for u in args.url]
        except PS4Error as e:
            sys.exit(f"error: {e}")
        print(json.dumps(api_install_direct(cfg["ps4_ip"], urls, timeout=cfg["timeout"] * 4)))
    elif args.ref_url:
        print(json.dumps(api_install_ref(cfg["ps4_ip"], args.ref_url, cfg["timeout"] * 4)))
    srv.stop()


def selftest():
    global CONFIG_PATH
    ok = []
    raw = ('{ "status": "success", "bits": 0x1F, "error": 0, "length": 0x1000,'
           ' "transferred": 0x800, "length_total": 0x2000, "transferred_total": 0x800,'
           ' "num_index": 1, "num_total": 2, "rest_sec": 5, "rest_sec_total": 9,'
           ' "preparing_percent": -1, "local_copy_percent": 0 }')
    p = parse_ps4_json(raw)
    assert p["bits"] == 0x1F and p["length"] == 0x1000 and p["transferred_total"] == 0x800, p
    ok.append("hex-json progress")
    r = parse_ps4_json('{ "status": "success", "exists": "true", "size": 0x1A2B3C }')
    assert r["exists"] == "true" and r["size"] == 0x1A2B3C, r
    ok.append("hex-json is_exists")
    assert check_title_id("cusa02299") == "CUSA02299"
    try:
        check_title_id("nope"); assert False
    except PS4Error:
        ok.append("title_id reject")
    assert check_content_id("UP9000-CUSA02299_00-MARVELSSPIDERMAN") == "UP9000-CUSA02299_00-MARVELSSPIDERMAN"
    try:
        check_content_id("short"); assert False
    except PS4Error:
        ok.append("content_id reject")
    for u in ("ftp://x/y.pkg", "", "  "):
        try:
            check_url(u); assert False, u
        except PS4Error:
            pass
    ok.append("url reject")
    assert parse_range("bytes=10-20", 100) == (10, 20)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=0-999", 100) == (0, 99)
    assert parse_range("bytes=200-300", 100) is None
    assert parse_range("garbage", 100) is None
    ok.append("range parse")
    hdr = bytearray(PKG_HDR_SIZE)
    hdr[:4] = PKG_MAGIC
    struct.pack_into(">I", hdr, 0x74, 0x1A)
    struct.pack_into(">I", hdr, 0x78, 0x60000000)
    struct.pack_into(">Q", hdr, 0x430, 123456)
    cid = b"UP1004-CUSA03041_00-REDEMPTION000002"
    hdr[0x40:0x40 + len(cid)] = cid
    with tempfile.NamedTemporaryFile(suffix=".pkg", delete=False) as f:
        f.write(bytes(hdr))
        tmp = f.name
    try:
        info = inspect_pkg(tmp)
        assert info["content_id"] == cid.decode() and info["is_patch"] and info["size"] == 123456, info
        ok.append("pkg header")
    finally:
        os.unlink(tmp)
    with tempfile.TemporaryDirectory() as d:
        blob = os.urandom(300000)
        open(os.path.join(d, "t.pkg"), "wb").write(blob)
        srv = PkgServer()
        srv.start(d, 0)
        try:
            base = f"http://127.0.0.1:{srv.port}/t.pkg"
            rq = urllib.request.Request(base, headers={"Range": "bytes=10-19"})
            with urllib.request.urlopen(rq, timeout=10) as resp:
                assert resp.status == 206, resp.status
                assert resp.headers.get("Content-Range") == f"bytes 10-19/{len(blob)}"
                assert resp.read() == blob[10:20]
            ok.append("range serve 206")
            rq = urllib.request.Request(base, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(rq, timeout=10) as resp:
                assert resp.status == 206 and resp.read() == blob[0:1]
            ok.append("range serve first-byte")
        finally:
            srv.stop()
    with tempfile.TemporaryDirectory() as d:
        old, CONFIG_PATH = CONFIG_PATH, os.path.join(d, "cfg.json")
        try:
            cfg = load_config()
            cfg["ps4_ip"] = "192.168.1.42"
            save_config(cfg)
            assert load_config()["ps4_ip"] == "192.168.1.42", load_config()
            ok.append("config remembers ps4_ip")
        finally:
            CONFIG_PATH = old
    tasks = {}
    assert note_install(tasks, {"task_id": -1, "title": "X"}, "u") is None and not tasks
    assert note_install(tasks, {"task_id": 9, "title": "Y"}, "u") == 9 and tasks == {9: "u"}
    ok.append("note_install")
    assert _probe_ps4("127.0.0.1", port=9, timeout=0.3) is False
    ok.append("discover probe reject")
    print("SELFTEST PASS: " + ", ".join(ok))


def main():
    ap = argparse.ArgumentParser(description="PS4 Remote PKG Installer client (port 12800)")
    ap.add_argument("--ps4-ip")
    ap.add_argument("--serve-dir")
    ap.add_argument("--local-port", type=int)
    ap.add_argument("--bind-ip", default=None)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--install", nargs="+", metavar="PKG")
    ap.add_argument("--url", nargs="+", metavar="URL")
    ap.add_argument("--ref-url")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if args.discover:
        for ip in discover_ps4s():
            print(ip)
        return
    cfg = load_config()
    for k in ("ps4_ip", "serve_dir", "local_port", "bind_ip", "timeout"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    srv = PkgServer()
    try:
        if args.install or args.url or args.ref_url:
            headless(args, cfg, srv)
        else:
            run_tui(cfg, srv)
    except Quit:
        print("\nbye.")
    except KeyboardInterrupt:
        print("\ninterrupted.")
    finally:
        srv.stop()
        save_config(cfg)


if __name__ == "__main__":
    main()
