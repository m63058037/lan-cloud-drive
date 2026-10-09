"""局域网云盘 — 单文件 FastAPI 服务。"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import http.server
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import socketserver
import ssl
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

TEXT_EXT = {
    ".txt", ".md", ".csv", ".json", ".xml", ".log", ".ini", ".cfg", ".yml", ".yaml",
    ".py", ".js", ".ts", ".css", ".html", ".htm", ".bat", ".ps1", ".sh", ".toml",
}
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VID_EXT = {".mp4", ".webm", ".ogg", ".mov"}
AUD_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
PDF_EXT = {".pdf"}
ZIP_EXT = {".zip"}
EDIT_MAX = 2 * 1024 * 1024
SEARCH_CAP = 400
ZIP_FILE_CAP = 8000


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    storage = Path(cfg["storage_dir"])
    storage.mkdir(parents=True, exist_ok=True)
    cfg["_storage"] = storage.resolve()
    return cfg


CFG = load_config()
STORAGE: Path = CFG["_storage"]
CLOUD = STORAGE / ".cloud"
TRASH = CLOUD / "trash"
SHARES_FILE = CLOUD / "shares.json"
FAVS_FILE = CLOUD / "favorites.json"
RECENT_FILE = CLOUD / "recent.json"
AUTO_ZIP_MB = int(CFG.get("auto_zip_mb") or 300)
AUTO_ZIP_BYTES = AUTO_ZIP_MB * 1024 * 1024
for p in (CLOUD, TRASH):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="局域网云盘", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=CFG["secret_key"],
    max_age=int(CFG.get("session_max_age_hours", 72)) * 3600,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

NAME_RE = re.compile(r'^[^<>:"/\\|?*\x00-\x1f]+$')
MAX_NAME_LEN = 200


def logged_in(request: Request) -> bool:
    return bool(request.session.get("ok"))


def require_login(request: Request) -> None:
    if not logged_in(request):
        raise HTTPException(status_code=401, detail="未登录")


def safe_join(rel: str, root: Path | None = None) -> Path:
    root = (root or STORAGE).resolve()
    rel = (rel or "").replace("\\", "/").strip("/")
    if rel in ("", "."):
        return root
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="非法路径")
    target = (root.joinpath(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路径") from None
    return target


def rel_of(path: Path, root: Path | None = None) -> str:
    root = (root or STORAGE).resolve()
    path = path.resolve()
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or len(name) > MAX_NAME_LEN or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法名称")
    return name


def unique_dest(parent: Path, name: str) -> Path:
    dest = parent / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def compress_file(path: Path) -> Path:
    """将单文件压缩为同目录下的 zip（原子写入）；若压缩后反而更大则保留原文件。"""
    if not path.is_file():
        return path
    zip_path = unique_dest(path.parent, path.name + ".zip")
    tmp = path.parent / (zip_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(path, path.name)
        if tmp.stat().st_size < path.stat().st_size:
            tmp.replace(zip_path)
            path.unlink(missing_ok=True)
            return zip_path
        tmp.unlink(missing_ok=True)
    except (OSError, zipfile.BadZipFile):
        tmp.unlink(missing_ok=True)
    return path


def kind_of(path: Path) -> str:
    if path.is_dir():
        return "dir"
    ext = path.suffix.lower()
    if ext in IMG_EXT:
        return "image"
    if ext in VID_EXT:
        return "video"
    if ext in AUD_EXT:
        return "audio"
    if ext in PDF_EXT:
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    if ext in ZIP_EXT:
        return "zip"
    return "file"


def entry_info(path: Path) -> dict:
    st = path.stat()
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": rel_of(path),
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "mtime_ts": int(st.st_mtime),
        "kind": kind_of(path),
    }


def skip_child(path: Path) -> bool:
    return path.name.startswith(".") or path.resolve() == CLOUD.resolve()


def json_load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def json_save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def push_recent(rel: str) -> None:
    if not rel:
        return
    items = json_load(RECENT_FILE, [])
    items = [x for x in items if x != rel]
    items.insert(0, rel)
    json_save(RECENT_FILE, items[:30])


def _ascii_fallback(name: str) -> str:
    out = re.sub(r"[^\x20-\x7e]", "_", name).strip(" .")
    if not out or out.count("_") == len(out):
        return "download"
    return out


def attachment(name: str) -> dict:
    return {
        "Content-Disposition": (
            f'attachment; filename="{_ascii_fallback(name)}"; filename*=UTF-8\'\'{quote(name)}'
        )
    }


def inline_headers(name: str) -> dict:
    return {
        "Content-Disposition": (
            f'inline; filename="{_ascii_fallback(name)}"; filename*=UTF-8\'\'{quote(name)}'
        )
    }


def zip_stream(paths: list[Path], zip_name: str) -> StreamingResponse:
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            if not item.exists():
                continue
            if item.is_file():
                zf.write(item, item.name)
                count += 1
            else:
                base = item.parent
                for root, dirs, files in os.walk(item):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fn in files:
                        if fn.startswith("."):
                            continue
                        fp = Path(root) / fn
                        arc = fp.relative_to(base).as_posix()
                        zf.write(fp, arc)
                        count += 1
                        if count > ZIP_FILE_CAP:
                            raise HTTPException(status_code=400, detail="打包文件过多")
    if count == 0:
        raise HTTPException(status_code=400, detail="没有可打包的文件")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=attachment(zip_name if zip_name.endswith(".zip") else zip_name + ".zip"),
    )


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return ips


# ===== 发送到附近设备（LocalSend 协议）=====
LOCAL_PORT = int(CFG.get("local_port") or 53317)
LOCAL_ALIAS = "局域网云盘"
MCAST_GROUP = "224.0.0.167"
MCAST_PORT = 53317
LOCAL_FINGERPRINT = "cloud-" + uuid.uuid4().hex[:10]

# --- 本机 TLS 客户端证书：真实 LocalSend 的 HTTPS 服务要求客户端证书（任意有效自签名即可）---
CERT_FILE = CLOUD / "localsend_cert.pem"
KEY_FILE = CLOUD / "localsend_key.pem"


def _ensure_local_cert() -> None:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    import datetime as _dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "局域网云盘")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
                                     content_commitment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))


_ensure_local_cert()
TLS_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
TLS_CTX.check_hostname = False
TLS_CTX.verify_mode = ssl.CERT_NONE
TLS_CTX.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
# 真实 LocalSend 在 HTTPS 模式下用客户端证书的 SHA-256 指纹标识设备
CERT_FINGERPRINT = hashlib.sha256(CERT_FILE.read_bytes()).hexdigest().upper()

# --- 本机 LocalSend 监听服务：接收真实设备听到我们通告后回执的 /register，以及 /info 探测 ---
KNOWN_DEVICES: dict[str, dict] = {}
DEVICE_TTL = 30


def _dev_key(d: dict) -> str:
    return d.get("fingerprint") or f"{d.get('ip')}:{d.get('port')}"


# --- 接收真实设备发送的文件（LocalSend v2 服务端）---
_INCOMING = STORAGE / "来自设备"
_INCOMING.mkdir(parents=True, exist_ok=True)
_LS_SESSIONS: dict[str, dict] = {}
_LS_SESSIONS_LOCK = threading.Lock()


def _unique_target(name: str) -> Path:
    name = (name or "file").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if not name or name in (".", ".."):
        name = "file"
    target = _INCOMING / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = _INCOMING / f"{stem} ({n}){suffix}"
        n += 1
    return target


class _LsServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _our_info(self) -> bytes:
        return json.dumps({
            "alias": LOCAL_ALIAS, "version": "2.2", "deviceModel": "CloudDisk",
            "deviceType": "desktop", "fingerprint": LOCAL_FINGERPRINT, "download": True,
        }).encode("utf-8")

    def _send_json(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/localsend/v2/info", "/api/localsend/v1/info", "/api/v1/info"):
            self._send_json(200, self._our_info())
        else:
            self._send_json(404, b'{"error":"not found"}')

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/localsend/v2/register", "/api/localsend/v1/register", "/api/v1/register"):
            try:
                body = json.loads(self._read_body().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, b'{"error":"bad request"}')
                return
            if isinstance(body, dict) and body.get("alias"):
                ip = self.client_address[0]
                port = int(body.get("port") or LOCAL_PORT)
                entry = {
                    "alias": body.get("alias", ip),
                    "ip": ip,
                    "model": body.get("deviceModel", ""),
                    "type": body.get("deviceType", ""),
                    "version": body.get("version", ""),
                    "fingerprint": body.get("fingerprint", ""),
                    "port": port,
                    "protocol": str(body.get("protocol") or "http").lower(),
                    "_seen": time.time(),
                }
                KNOWN_DEVICES[_dev_key(entry)] = entry
                self._send_json(200, self._our_info())
                return
            self._send_json(400, b'{"error":"bad request"}')
            return
        if path == "/api/localsend/v2/prepare-upload":
            self._handle_prepare_upload()
            return
        if path == "/api/localsend/v2/upload":
            self._handle_upload()
            return
        if path == "/api/localsend/v2/cancel":
            try:
                body = json.loads(self._read_body().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
            sid = (body or {}).get("sessionId") if isinstance(body, dict) else None
            if sid:
                with _LS_SESSIONS_LOCK:
                    _LS_SESSIONS.pop(sid, None)
            self._send_json(200, b"{}")
            return
        self._send_json(404, b'{"error":"not found"}')

    def _handle_prepare_upload(self) -> None:
        try:
            body = json.loads(self._read_body().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, b'{"error":"bad request"}')
            return
        if not isinstance(body, dict):
            self._send_json(400, b'{"error":"bad request"}')
            return
        files = body.get("files")
        if not isinstance(files, dict) or not files:
            self._send_json(400, b'{"error":"no files"}')
            return
        session_id = uuid.uuid4().hex
        tokens: dict[str, str] = {}
        with _LS_SESSIONS_LOCK:
            for fid, meta in files.items():
                if not isinstance(meta, dict):
                    continue
                tokens[fid] = uuid.uuid4().hex
            _LS_SESSIONS[session_id] = {
                "sender": self.client_address[0],
                "files": {fid: {"name": (files[fid] or {}).get("fileName", "file"),
                                "token": tokens[fid]} for fid in tokens},
            }
        if not tokens:
            self._send_json(400, b'{"error":"no files"}')
            return
        self._send_json(200, json.dumps({
            "sessionId": session_id,
            "files": tokens,
        }).encode("utf-8"))

    def _handle_upload(self) -> None:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        sid = (q.get("sessionId") or [""])[0]
        fid = (q.get("fileId") or [""])[0]
        token = (q.get("token") or [""])[0]
        with _LS_SESSIONS_LOCK:
            sess = _LS_SESSIONS.get(sid)
            fmeta = (sess or {}).get("files", {}).get(fid) if sess else None
            ok = bool(fmeta) and fmeta["token"] == token
        if not ok:
            self._send_json(403, b'{"error":"invalid token"}')
            return
        try:
            body = self._read_body()
        except Exception:
            body = b""
        target = _unique_target(fmeta["name"])
        try:
            with target.open("wb") as f:
                f.write(body)
        except OSError:
            self._send_json(500, b'{"error":"save failed"}')
            return
        self._send_json(200, b"{}")


def _start_ls_listener() -> int:
    for p in range(LOCAL_PORT, LOCAL_PORT + 50):
        try:
            srv = socketserver.ThreadingTCPServer(("0.0.0.0", p), _LsServer)
        except OSError:
            continue
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return p
    raise RuntimeError("无法监听 LocalSend 端口")


LS_LISTEN_PORT = _start_ls_listener()


def _announce_loop() -> None:
    """周期组播通告本机，触发真实 LocalSend 设备 /register 回执，实现双向发现。"""
    msg = json.dumps({
        "alias": LOCAL_ALIAS, "version": "2.2", "deviceModel": "CloudDisk",
        "deviceType": "desktop", "fingerprint": LOCAL_FINGERPRINT,
        "port": LS_LISTEN_PORT, "protocol": "http", "download": True,
    }).encode("utf-8")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    while True:
        try:
            s.sendto(msg, (MCAST_GROUP, MCAST_PORT))
        except OSError:
            pass
        time.sleep(4)


threading.Thread(target=_announce_loop, daemon=True).start()


def _is_lan_host(host: str) -> bool:
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return False
    nums = ip.split(".")
    if len(nums) != 4:
        return False
    try:
        a, b = int(nums[0]), int(nums[1])
    except ValueError:
        return False
    return a == 10 or a == 127 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def _probe_device(host: str, port: int | None = None, use_https: bool | None = None,
                  timeout: float = 0.6) -> dict | None:
    """探测设备信息。优先使用通告端口，回落标准 53317 与本机配置端口；协议按通告值优先，失败再尝试另一种。"""
    paths = (
        "/api/localsend/v2/info",
        "/api/localsend/v1/info",
        "/api/v1/info",
        "/",
    )
    ports: list[int] = []
    for p in (port, 53317, LOCAL_PORT):
        if p and p not in ports:
            ports.append(p)
    schemes = [True, False] if use_https is None else [use_https, not use_https]
    for p in ports:
        for https in schemes:
            for path in paths:
                try:
                    conn = (
                        http.client.HTTPSConnection(host, p, timeout=timeout, context=TLS_CTX)
                        if https
                        else http.client.HTTPConnection(host, p, timeout=timeout)
                    )
                    try:
                        conn.request("GET", path)
                        resp = conn.getresponse()
                        if resp.status != 200:
                            continue
                        data = json.loads(resp.read().decode("utf-8"))
                    finally:
                        conn.close()
                    if isinstance(data, dict) and data.get("alias"):
                        data["_port"] = p
                        data["_https"] = https
                        return data
                except (OSError, ValueError, json.JSONDecodeError,
                        http.client.HTTPException, http.client.BadStatusLine):
                    continue
    return None


def _mcast_discover(listen_secs: float = 2.5) -> list[dict]:
    """发送 LocalSend 组播通告并监听其他设备的组播消息，收集设备信息。"""
    out: dict[str, dict] = {}

    def _send_announce(sock: socket.socket):
        msg = {
            "alias": LOCAL_ALIAS,
            "version": "2.2",
            "deviceModel": "CloudDisk",
            "deviceType": "desktop",
            "fingerprint": LOCAL_FINGERPRINT,
            "port": LS_LISTEN_PORT,
            "protocol": "http",
            "download": True,
        }
        try:
            sock.sendto(json.dumps(msg).encode("utf-8"), (MCAST_GROUP, MCAST_PORT))
        except OSError:
            pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", MCAST_PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton(MCAST_GROUP) + socket.inet_aton("0.0.0.0"))
        sock.settimeout(listen_secs)
        _send_announce(sock)
        deadline = time.time() + listen_secs
        while time.time() < deadline:
            try:
                raw, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not data.get("alias"):
                continue
            fp = data.get("fingerprint") or ""
            if fp and fp == LOCAL_FINGERPRINT:
                continue
            ip = addr[0]
            if ip.startswith("127."):
                continue
            entry = {
                "alias": data.get("alias", ip),
                "ip": ip,
                "model": data.get("deviceModel") or "",
                "type": data.get("deviceType") or "",
                "version": data.get("version") or "",
                "fingerprint": data.get("fingerprint", ""),
                "port": int(data.get("port") or 53317),
                "protocol": str(data.get("protocol") or "http").lower(),
            }
            out[_dev_key(entry)] = entry
    except OSError:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return list(out.values())


def discover_devices(hosts: list[str] | None = None) -> list[dict]:
    found: dict[str, dict] = {}
    now = time.time()
    # 1) 主动组播通告的设备；通告本身也会触发真实设备 /register 回执
    for d in _mcast_discover():
        d["_seen"] = now
        found.setdefault(_dev_key(d), d)
    # 2) 真实设备听到通告后主动 /register 回执的设备（最可靠，可覆盖同名 mcast 条目）
    for key, d in KNOWN_DEVICES.items():
        if now - d.get("_seen", 0) <= DEVICE_TTL:
            found[key] = {k: v for k, v in d.items() if not k.startswith("_")}
    # 3) 子网全量探测兜底（/info 端点）
    if hosts is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            base = s.getsockname()[0]
        except OSError:
            base = "127.0.0.1"
        finally:
            s.close()
        sub = ".".join(base.split(".")[:3])
        hosts = [f"{sub}.{i}" for i in range(1, 255)]
    ip_map: dict[str, dict] = {}
    for d in found.values():
        ip_map.setdefault(d["ip"], d)
    params = []
    for host in hosts:
        m = ip_map.get(host)
        port = m.get("port") if m else None
        https = None if not m else (m.get("protocol") == "https")
        params.append((host, port, https))
    with ThreadPoolExecutor(max_workers=64) as pool:
        for host, info in zip(hosts, pool.map(lambda p: _probe_device(p[0], p[1], p[2]), params)):
            if not info:
                continue
            entry = {
                "alias": info.get("alias", host),
                "ip": host,
                "model": info.get("deviceModel", ""),
                "type": info.get("deviceType", ""),
                "version": info.get("version", ""),
                "fingerprint": info.get("fingerprint", ""),
                "port": info.get("_port", LOCAL_PORT),
                "protocol": "https" if info.get("_https") else "http",
            }
            found[_dev_key(entry)] = entry
    out = list(found.values())
    out.sort(key=lambda d: d["alias"].lower())
    return out


def send_to_local_device(
    host: str,
    path: Path,
    use_https: bool = True,
    port: int = LOCAL_PORT,
    wait_timeout: int = 90,
) -> None:
    if not _is_lan_host(host):
        raise HTTPException(status_code=400, detail="仅支持局域网设备")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="只能发送文件")
    session_id = uuid.uuid4().hex
    file_id = uuid.uuid4().hex
    fingerprint = uuid.uuid4().hex[:12]
    size = path.stat().st_size
    ctx = TLS_CTX if use_https else None

    def _req(method, url, body=None, headers=None, timeout=wait_timeout):
        conn = (
            http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
            if use_https
            else http.client.HTTPConnection(host, port, timeout=timeout)
        )
        try:
            conn.request(method, url, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw
        finally:
            conn.close()

    def _upload_ok(status: int) -> bool:
        return status in (200, 201, 204)

    def _send_v2() -> None:
        meta = {
            "id": file_id,
            "fileName": path.name,
            "size": size,
            "fileType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "sha256": None,
            "preview": None,
            "metadata": None,
        }
        body = {
            "info": {
                "alias": LOCAL_ALIAS,
                "version": "2.2",
                "deviceModel": "CloudDisk",
                "deviceType": "desktop",
                "fingerprint": CERT_FINGERPRINT if use_https else LOCAL_FINGERPRINT,
                "port": LOCAL_PORT,
                "protocol": "https" if use_https else "http",
                "download": False,
            },
            "files": {file_id: meta},
        }
        status, raw = _req(
            "POST",
            "/api/localsend/v2/prepare-upload",
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if status == 404:
            raise _V1Only()
        if status not in (200, 201):
            raise RuntimeError(f"发送请求被拒绝（HTTP {status}）")
        try:
            decision = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            decision = {}
        token = (decision.get("files") or {}).get(file_id) or ""
        with path.open("rb") as f:
            status, _ = _req(
                "POST",
                f"/api/localsend/v2/upload?sessionId={session_id}&fileId={file_id}&token={token}",
                body=f,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                timeout=600,
            )
        if not _upload_ok(status):
            raise RuntimeError(f"文件上传失败（HTTP {status}）")

    class _V1Only(Exception):
        pass

    try:
        _send_v2()
        return
    except _V1Only:
        pass

    payload = {
        "anonymous": True,
        "sessionId": session_id,
        "files": [
            {
                "id": file_id,
                "fileName": path.name,
                "size": size,
                "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            }
        ],
        "info": {
            "alias": LOCAL_ALIAS,
            "version": "1.0.0",
            "deviceModel": "CloudDisk",
            "deviceType": "desktop",
            "fingerprint": fingerprint,
        },
        "fingerprint": fingerprint,
    }
    status, raw = _req(
        "POST",
        "/api/v1/send-request",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if status not in (200, 201):
        raise RuntimeError(f"发送请求被拒绝（HTTP {status}）")
    try:
        decision = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        decision = {}
    if decision.get("status") == "cancel":
        raise RuntimeError("对方拒绝了传输")
    if decision.get("status") != "ok":
        raise RuntimeError("对方未确认或请求超时")
    with path.open("rb") as f:
        status, _ = _req(
            "POST",
            f"/api/v1/upload/{session_id}/{file_id}",
            body=f,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            timeout=600,
        )
    if not _upload_ok(status):
        raise RuntimeError(f"文件上传失败（HTTP {status}）")


def crumbs_of(rel: str) -> list[dict]:
    crumbs = [{"name": "根目录", "path": ""}]
    if rel.strip("/"):
        acc = []
        for part in rel.replace("\\", "/").strip("/").split("/"):
            acc.append(part)
            crumbs.append({"name": part, "path": "/".join(acc)})
    return crumbs


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"storage_label": str(STORAGE)},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: Annotated[str, Form()] = ""):
    if password == CFG["password"]:
        request.session["ok"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "密码错误"},
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/api/stats")
async def api_stats(request: Request):
    require_login(request)
    du = shutil.disk_usage(STORAGE)
    used = 0
    files = 0
    for root, dirs, fnames in os.walk(STORAGE):
        dirs[:] = [d for d in dirs if d != ".cloud" and not d.startswith(".")]
        for fn in fnames:
            if fn.startswith("."):
                continue
            fp = Path(root) / fn
            try:
                used += fp.stat().st_size
                files += 1
            except OSError:
                continue
    trash_n = sum(1 for _ in TRASH.iterdir()) if TRASH.exists() else 0
    return {
        "used": used,
        "files": files,
        "disk_total": du.total,
        "disk_free": du.free,
        "trash": trash_n,
    }


@app.get("/api/list")
async def api_list(request: Request, path: str = "", sort: str = "name"):
    require_login(request)
    target = safe_join(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="目录不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")
    items = []
    for child in target.iterdir():
        if skip_child(child):
            continue
        try:
            items.append(entry_info(child))
        except OSError:
            continue
    key = {
        "size": lambda x: (not x["is_dir"], x["size"], x["name"].lower()),
        "mtime": lambda x: (not x["is_dir"], -x["mtime_ts"]),
        "name": lambda x: (not x["is_dir"], x["name"].lower()),
    }.get(sort, None)
    items.sort(key=key or (lambda x: (not x["is_dir"], x["name"].lower())))
    rel = rel_of(target)
    return {"path": rel, "crumbs": crumbs_of(rel), "items": items}


@app.get("/api/search")
async def api_search(request: Request, q: str = ""):
    require_login(request)
    q = (q or "").strip().lower()
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="请输入关键词")
    hits = []
    for root, dirs, files in os.walk(STORAGE):
        dirs[:] = [d for d in dirs if d != ".cloud" and not d.startswith(".")]
        rp = Path(root)
        for name in dirs + files:
            if q not in name.lower():
                continue
            p = rp / name
            if skip_child(p):
                continue
            try:
                hits.append(entry_info(p))
            except OSError:
                continue
            if len(hits) >= SEARCH_CAP:
                return {"q": q, "items": hits, "capped": True}
    return {"q": q, "items": hits, "capped": False}


@app.post("/api/mkdir")
async def api_mkdir(request: Request, path: str = Form(""), name: str = Form(...)):
    require_login(request)
    name = validate_name(name)
    parent = safe_join(path)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="父目录不存在")
    dest = parent / name
    if dest.exists():
        raise HTTPException(status_code=409, detail="已存在同名项")
    dest.mkdir()
    return {"ok": True, "item": entry_info(dest)}


@app.post("/api/touch")
async def api_touch(request: Request, path: str = Form(""), name: str = Form(...)):
    require_login(request)
    name = validate_name(name)
    if not Path(name).suffix:
        name = name + ".txt"
        validate_name(name)
    parent = safe_join(path)
    dest = unique_dest(parent, name)
    dest.write_text("", encoding="utf-8")
    return {"ok": True, "item": entry_info(dest)}


@app.post("/api/upload")
async def api_upload(
    request: Request,
    path: str = Form(""),
    files: list[UploadFile] = File(...),
    rel_paths: Annotated[list[str], Form()] = [],
):
    require_login(request)
    parent = safe_join(path)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="目录不存在")
    max_mb = int(CFG.get("max_upload_mb") or 0)
    max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else None
    saved = []
    for i, uf in enumerate(files):
        rel = (rel_paths[i] if i < len(rel_paths) else "") or ""
        rel = rel.replace("\\", "/").strip("/")
        sub = parent
        if rel:
            parts = [p for p in rel.split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                raise HTTPException(status_code=400, detail="非法路径")
            for p in parts[:-1]:
                sub = sub / validate_name(p)
            sub.mkdir(parents=True, exist_ok=True)
            name = validate_name(parts[-1])
        else:
            name = validate_name(Path(uf.filename or "upload.bin").name)
        dest = unique_dest(sub, name)
        written = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail=f"超过 {max_mb}MB 限制")
                    out.write(chunk)
        finally:
            await uf.close()
        item = entry_info(dest)
        if not item["is_dir"] and item["size"] > AUTO_ZIP_BYTES:
            dest = await asyncio.to_thread(compress_file, dest)
            item = entry_info(dest)
        saved.append(item)
    return {"ok": True, "items": saved}


@app.get("/api/local/discover")
async def api_local_discover(request: Request):
    require_login(request)
    devices = await asyncio.to_thread(discover_devices)
    return {"devices": devices}


@app.post("/api/local/send")
async def api_local_send(request: Request, path: str = Form(...), host: str = Form(...),
                         port: int = Form(LOCAL_PORT), use_https: str = Form("")):
    require_login(request)
    target = safe_join(path)
    use_https_flag = use_https.lower() in ("1", "true", "yes", "on")
    try:
        await asyncio.to_thread(send_to_local_device, host.strip(), target, use_https_flag, port)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {"ok": True}


@app.get("/api/download")
async def api_download(request: Request, path: str):
    require_login(request)
    target = safe_join(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="不存在")
    if target.is_dir():
        push_recent(rel_of(target))
        return zip_stream([target], target.name + ".zip")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    push_recent(rel_of(target))
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers=attachment(target.name))


@app.post("/api/zip")
async def api_zip(request: Request, paths: str = Form(...)):
    require_login(request)
    rels = [p for p in paths.split("\n") if p.strip()]
    items = [safe_join(p.strip()) for p in rels]
    if not items:
        raise HTTPException(status_code=400, detail="未选择文件")
    name = items[0].name + ".zip" if len(items) == 1 else "download.zip"
    return zip_stream(items, name)


@app.get("/api/raw")
async def api_raw(request: Request, path: str):
    require_login(request)
    target = safe_join(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    push_recent(rel_of(target))
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers=inline_headers(target.name))


@app.get("/api/text")
async def api_text_get(request: Request, path: str):
    require_login(request)
    target = safe_join(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if target.stat().st_size > EDIT_MAX:
        raise HTTPException(status_code=400, detail="文件过大，无法在线编辑")
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    push_recent(rel_of(target))
    return {"path": rel_of(target), "name": target.name, "text": text}


@app.post("/api/text")
async def api_text_save(request: Request, path: str = Form(...), text: str = Form("")):
    require_login(request)
    target = safe_join(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if len(text.encode("utf-8")) > EDIT_MAX:
        raise HTTPException(status_code=400, detail="内容过大")
    target.write_text(text, encoding="utf-8")
    return {"ok": True, "item": entry_info(target)}


@app.post("/api/rename")
async def api_rename(request: Request, path: str = Form(...), name: str = Form(...)):
    require_login(request)
    name = validate_name(name)
    target = safe_join(path)
    if not target.exists() or target == STORAGE:
        raise HTTPException(status_code=400, detail="无法重命名")
    dest = target.parent / name
    if dest.exists():
        raise HTTPException(status_code=409, detail="已存在同名项")
    target.rename(dest)
    return {"ok": True, "item": entry_info(dest)}


@app.post("/api/copy")
async def api_copy(request: Request, src: str = Form(...), dest: str = Form("")):
    require_login(request)
    source = safe_join(src)
    parent = safe_join(dest)
    if not source.exists() or source == STORAGE:
        raise HTTPException(status_code=400, detail="无法复制")
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")
    out = unique_dest(parent, source.name)
    if source.is_dir():
        shutil.copytree(source, out)
    else:
        shutil.copy2(source, out)
    return {"ok": True, "item": entry_info(out)}


@app.post("/api/move")
async def api_move(request: Request, src: str = Form(...), dest: str = Form("")):
    require_login(request)
    source = safe_join(src)
    parent = safe_join(dest)
    if not source.exists() or source == STORAGE:
        raise HTTPException(status_code=400, detail="无法移动")
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")
    try:
        source.resolve().relative_to(parent.resolve())
        if parent.resolve() != source.parent.resolve():
            # 不能把目录移进自己里面
            if source.is_dir() and (
                parent.resolve() == source.resolve()
                or str(parent.resolve()).startswith(str(source.resolve()) + os.sep)
            ):
                raise HTTPException(status_code=400, detail="不能移动到自身内部")
    except ValueError:
        pass
    out = unique_dest(parent, source.name)
    shutil.move(str(source), str(out))
    return {"ok": True, "item": entry_info(out)}


@app.post("/api/delete")
async def api_delete(request: Request, path: str = Form(...)):
    require_login(request)
    target = safe_join(path)
    if target == STORAGE or CLOUD in target.parents or target == CLOUD:
        raise HTTPException(status_code=400, detail="不能删除")
    if not target.exists():
        raise HTTPException(status_code=404, detail="不存在")
    orig = rel_of(target)
    dest = unique_dest(TRASH, f"{int(time.time())}_{target.name}")
    shutil.move(str(target), str(dest))
    meta = json_load(CLOUD / "trash.json", {})
    meta[dest.name] = {"orig": orig, "name": target.name, "is_dir": dest.is_dir(), "t": int(time.time())}
    json_save(CLOUD / "trash.json", meta)
    return {"ok": True}


@app.get("/api/trash")
async def api_trash_list(request: Request):
    require_login(request)
    meta = json_load(CLOUD / "trash.json", {})
    items = []
    for name, info in list(meta.items()):
        p = TRASH / name
        if not p.exists():
            meta.pop(name, None)
            continue
        try:
            e = entry_info(p)
        except OSError:
            continue
        e["orig"] = info.get("orig", "")
        e["trash_id"] = name
        items.append(e)
    json_save(CLOUD / "trash.json", meta)
    items.sort(key=lambda x: -x["mtime_ts"])
    return {"items": items}


@app.post("/api/trash/restore")
async def api_trash_restore(request: Request, trash_id: str = Form(...)):
    require_login(request)
    meta = json_load(CLOUD / "trash.json", {})
    info = meta.get(trash_id)
    src = TRASH / trash_id
    if not info or not src.exists():
        raise HTTPException(status_code=404, detail="回收站中不存在")
    orig = info.get("orig") or info.get("name")
    dest_parent = safe_join(str(Path(orig).parent).replace("\\", "/")) if orig else STORAGE
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(dest_parent, info.get("name") or src.name)
    shutil.move(str(src), str(dest))
    meta.pop(trash_id, None)
    json_save(CLOUD / "trash.json", meta)
    return {"ok": True, "item": entry_info(dest)}


@app.post("/api/trash/purge")
async def api_trash_purge(request: Request, trash_id: str = Form("")):
    require_login(request)
    meta = json_load(CLOUD / "trash.json", {})
    if trash_id:
        src = TRASH / trash_id
        if src.exists():
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
        meta.pop(trash_id, None)
    else:
        shutil.rmtree(TRASH, ignore_errors=True)
        TRASH.mkdir(parents=True, exist_ok=True)
        meta = {}
    json_save(CLOUD / "trash.json", meta)
    return {"ok": True}


@app.post("/api/unzip")
async def api_unzip(request: Request, path: str = Form(...)):
    require_login(request)
    zpath = safe_join(path)
    if zpath.suffix.lower() != ".zip" or not zpath.is_file():
        raise HTTPException(status_code=400, detail="请选择 zip 文件")
    dest_dir = unique_dest(zpath.parent, zpath.stem)
    dest_dir.mkdir()
    try:
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    continue
                zf.extract(info, dest_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="损坏的 zip") from None
    return {"ok": True, "item": entry_info(dest_dir)}


@app.get("/api/favorites")
async def api_favs(request: Request):
    require_login(request)
    paths = json_load(FAVS_FILE, [])
    items = []
    alive = []
    for p in paths:
        fp = safe_join(p)
        if fp.exists() and not skip_child(fp):
            try:
                items.append(entry_info(fp))
                alive.append(p)
            except OSError:
                continue
    if alive != paths:
        json_save(FAVS_FILE, alive)
    return {"items": items}


@app.post("/api/favorites")
async def api_fav_toggle(request: Request, path: str = Form(...)):
    require_login(request)
    target = safe_join(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="不存在")
    rel = rel_of(target)
    items = json_load(FAVS_FILE, [])
    if rel in items:
        items = [x for x in items if x != rel]
        starred = False
    else:
        items.insert(0, rel)
        starred = True
    json_save(FAVS_FILE, items[:200])
    return {"ok": True, "starred": starred}


@app.get("/api/recent")
async def api_recent(request: Request):
    require_login(request)
    paths = json_load(RECENT_FILE, [])
    items = []
    alive = []
    for p in paths:
        fp = safe_join(p)
        if fp.exists() and not skip_child(fp):
            try:
                items.append(entry_info(fp))
                alive.append(p)
            except OSError:
                continue
    if alive != paths:
        json_save(RECENT_FILE, alive)
    return {"items": items}


@app.post("/api/share")
async def api_share(request: Request, path: str = Form(...), days: int = Form(7)):
    require_login(request)
    target = safe_join(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="不存在")
    days = max(1, min(int(days), 30))
    token = secrets.token_urlsafe(12)
    shares = json_load(SHARES_FILE, {})
    shares[token] = {"path": rel_of(target), "exp": int(time.time()) + days * 86400}
    json_save(SHARES_FILE, shares)
    return {"ok": True, "token": token, "url": f"/s/{token}", "days": days}


def share_entry(token: str) -> tuple[dict, Path]:
    shares = json_load(SHARES_FILE, {})
    info = shares.get(token)
    if not info:
        raise HTTPException(status_code=404, detail="分享不存在")
    if int(info.get("exp") or 0) < time.time():
        raise HTTPException(status_code=410, detail="分享已过期")
    target = safe_join(info["path"])
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件已删除")
    return info, target


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str):
    info, target = share_entry(token)
    return templates.TemplateResponse(
        request,
        "share.html",
        {"token": token, "name": target.name, "is_dir": target.is_dir(), "path": info["path"]},
    )


@app.get("/s/{token}/download")
async def share_download(token: str, path: str = ""):
    info, root = share_entry(token)
    if root.is_file():
        media = mimetypes.guess_type(root.name)[0] or "application/octet-stream"
        return FileResponse(root, media_type=media, headers=attachment(root.name))
    target = safe_join(path, root) if path else root
    if target.is_dir():
        return zip_stream([target], target.name + ".zip")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, headers=attachment(target.name))


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and exc.status_code == 401 and not request.url.path.startswith("/api"):
        return RedirectResponse("/login", status_code=302)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def main() -> None:
    import uvicorn

    port = int(CFG["port"])
    host = CFG.get("host", "0.0.0.0")
    print(f"存储目录: {STORAGE}")
    print(f"本机访问: http://127.0.0.1:{port}")
    for ip in lan_ips():
        print(f"局域网:   http://{ip}:{port}")
    print("共享密码: 见 config.json")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
