#!/usr/bin/env python3
"""今天吃什么？— 多用户共享菜名池服务器（仅用 Python 标准库）

用法:
    python3 server.py [端口]     # 默认端口 8787

用户名/密码保存在 users.json（PBKDF2 加盐哈希），菜名保存在 dishes.json（含添加人与时间）。
登录会话保存在内存中，服务器重启后需要重新登录。
"""

import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "dishes.json")
USER_FILE = os.path.join(BASE_DIR, "users.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

DEFAULT_SETTINGS = {"registrationOpen": True}

COOKIE_NAME = "ew_session"
ITERATIONS = 100_000  # PBKDF2 迭代次数

lock = threading.Lock()
sessions = {}  # token -> username


def load_users():
    try:
        with open(USER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data["users"]
    except (OSError, ValueError):
        pass
    return {}


def save_users(users):
    tmp = USER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_FILE)


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {**DEFAULT_SETTINGS, **data}
    except (OSError, ValueError):
        pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), ITERATIONS
    ).hex()


def load_state():
    """返回 (rev, dishes)，每道菜统一为 {name, by, at}。兼容旧的纯菜名数组。"""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0, []
    dishes = []
    if isinstance(data, dict) and isinstance(data.get("dishes"), list):
        for item in data["dishes"]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                dishes.append({"name": item["name"], "by": item.get("by"), "at": item.get("at")})
            elif isinstance(item, str):
                dishes.append({"name": item, "by": None, "at": None})
        return int(data.get("rev", 0)), dishes
    if isinstance(data, list):
        return 1, [{"name": d, "by": None, "at": None} for d in data if isinstance(d, str)]
    return 0, []


def save_state(rev, dishes):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"rev": rev, "dishes": dishes}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)  # 原子替换，避免写一半被读到


def new_session(username):
    token = secrets.token_urlsafe(32)
    sessions[token] = username
    return token


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type="application/json; charset=utf-8", set_cookie=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, set_cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), set_cookie=set_cookie)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def _session_cookie(self, token, max_age):
        return f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"

    def _current_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        if not morsel:
            return None
        return sessions.get(morsel.value)

    def _serve_file(self, filename):
        try:
            with open(os.path.join(BASE_DIR, filename), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except OSError:
            self._json(404, {"error": filename + " not found"})

    def _serve_index(self):
        self._serve_file("index.html")

    def _current_role(self):
        """返回 (用户名, 是否管理员)。未登录返回 (None, False)。"""
        user = self._current_user()
        if not user:
            return None, False
        with lock:
            info = load_users().get(user) or {}
        return user, info.get("role") == "admin"

    def do_HEAD(self):
        # Cloudflare 等代理/健康检查会发 HEAD 请求，按 GET 的路由返回空响应体
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/admin"):
            self.send_response(200)
        else:
            self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/me":
            user, is_admin = self._current_role()
            with lock:
                settings = load_settings()
                info = (load_users().get(user) or {}) if user else {}
            self._json(200, {"user": user, "role": "admin" if is_admin else "user",
                             "registrationOpen": settings.get("registrationOpen", True),
                             "created": info.get("created")})
        elif path == "/admin":
            _, is_admin = self._current_role()
            if is_admin:
                self._serve_file("admin.html")
            else:  # 非管理员一律回到前台
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
        elif path == "/api/admin/overview":
            _, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            with lock:
                users = load_users()
                rev, dishes = load_state()
                settings = load_settings()
            user_list = [
                {
                    "name": name,
                    "created": info.get("created"),
                    "role": info.get("role", "user"),
                    "dishCount": sum(1 for d in dishes if d["by"] == name),
                }
                for name, info in users.items()
            ]
            user_list.sort(key=lambda u: u["created"] or 0)
            self._json(200, {"users": user_list, "dishes": dishes, "rev": rev,
                             "registrationOpen": settings.get("registrationOpen", True)})
        elif path == "/api/dishes":
            if not self._current_user():
                self._json(401, {"error": "请先登录"})
                return
            with lock:
                rev, dishes = load_state()
            self._json(200, {"rev": rev, "dishes": dishes})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/admin/password":  # 管理员重置他人密码
            caller, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            body = self._read_json()
            if not isinstance(body, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            name = str(body.get("name") or "").strip()
            new_pw = str(body.get("newPassword") or "")
            if name == caller:
                self._json(400, {"error": "请在前台「账户设置」里修改自己的密码"})
                return
            if not 4 <= len(new_pw) <= 64:
                self._json(400, {"error": "新密码需要 4-64 位"})
                return
            with lock:
                users = load_users()
                info = users.get(name)
                if not info:
                    self._json(404, {"error": "用户不存在"})
                    return
                salt = secrets.token_hex(16)
                info["salt"] = salt
                info["hash"] = hash_password(new_pw, salt)
                save_users(users)
            self._json(200, {"ok": True, "name": name})
            return

        if path == "/api/admin/role":  # 设置/取消管理员（管理员）
            caller, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            body = self._read_json()
            if not isinstance(body, dict) or not isinstance(body.get("admin"), bool):
                self._json(400, {"error": "请求格式错误"})
                return
            name = str(body.get("name") or "").strip()
            make_admin = body["admin"]
            if name == caller:
                self._json(400, {"error": "不能修改自己的角色"})
                return
            with lock:
                users = load_users()
                if name not in users:
                    self._json(404, {"error": "用户不存在"})
                    return
                if users[name].get("role") == "admin" and not make_admin:
                    admins = [u for u, info in users.items() if info.get("role") == "admin"]
                    if len(admins) <= 1:
                        self._json(400, {"error": "至少保留一个管理员"})
                        return
                users[name]["role"] = "admin" if make_admin else "user"
                save_users(users)
            self._json(200, {"ok": True, "name": name, "role": users[name]["role"]})
            return

        if path == "/api/admin/registration":  # 开关注册（管理员）
            _, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            body = self._read_json()
            if not isinstance(body, dict) or not isinstance(body.get("open"), bool):
                self._json(400, {"error": "请求格式错误"})
                return
            with lock:
                settings = load_settings()
                settings["registrationOpen"] = body["open"]
                save_settings(settings)
            self._json(200, {"ok": True, "registrationOpen": body["open"]})
            return

        if path in ("/api/register", "/api/login"):
            body = self._read_json()
            if not isinstance(body, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            if not 1 <= len(username) <= 20:
                self._json(400, {"error": "用户名需要 1-20 个字符"})
                return
            if not 4 <= len(password) <= 64:
                self._json(400, {"error": "密码需要 4-64 位"})
                return

            if path == "/api/register":
                with lock:
                    if not load_settings().get("registrationOpen", True):
                        self._json(403, {"error": "注册已关闭，请联系管理员"})
                        return
                    users = load_users()
                    if username in users:
                        self._json(400, {"error": "用户名已被占用"})
                        return
                    salt = secrets.token_hex(16)
                    users[username] = {
                        "salt": salt,
                        "hash": hash_password(password, salt),
                        "created": int(time.time()),
                        "role": "user",
                    }
                    save_users(users)
                    token = new_session(username)
                self._json(200, {"ok": True, "user": username, "role": "user"},
                           set_cookie=self._session_cookie(token, 7 * 24 * 3600))
            else:
                with lock:
                    info = load_users().get(username)
                    valid = False
                    if info:
                        try:
                            valid = secrets.compare_digest(
                                info["hash"], hash_password(password, info["salt"])
                            )
                        except (KeyError, ValueError):
                            valid = False
                    if not valid:
                        self._json(400, {"error": "用户名或密码错误"})
                        return
                    token = new_session(username)
                self._json(200, {"ok": True, "user": username,
                                 "role": info.get("role", "user")},
                           set_cookie=self._session_cookie(token, 7 * 24 * 3600))
            return

        if path == "/api/logout":
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
            except Exception:
                pass
            morsel = cookie.get(COOKIE_NAME)
            if morsel:
                with lock:
                    sessions.pop(morsel.value, None)
            self._json(200, {"ok": True},
                       set_cookie=f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            return

        if path == "/api/password":  # 修改自己的密码（登录用户）
            user = self._current_user()
            if not user:
                self._json(401, {"error": "请先登录"})
                return
            body = self._read_json()
            if not isinstance(body, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            old_pw = str(body.get("oldPassword") or "")
            new_pw = str(body.get("newPassword") or "")
            if not 4 <= len(new_pw) <= 64:
                self._json(400, {"error": "新密码需要 4-64 位"})
                return
            with lock:
                users = load_users()
                info = users.get(user)
                if not info:
                    self._json(404, {"error": "用户不存在"})
                    return
                ok = False
                try:
                    ok = secrets.compare_digest(info["hash"], hash_password(old_pw, info["salt"]))
                except (KeyError, ValueError):
                    ok = False
                if not ok:
                    self._json(400, {"error": "当前密码错误"})
                    return
                salt = secrets.token_hex(16)
                info["salt"] = salt
                info["hash"] = hash_password(new_pw, salt)
                save_users(users)
            self._json(200, {"ok": True})
            return

        if path == "/api/dishes/rename":  # 重命名菜品（本人或管理员）
            user = self._current_user()
            if not user:
                self._json(401, {"error": "请先登录"})
                return
            _, is_admin = self._current_role()
            body = self._read_json()
            if not isinstance(body, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            old_name = str(body.get("from") or "").strip()
            new_name = str(body.get("to") or "").strip()
            if not old_name or not new_name:
                self._json(400, {"error": "菜名不能为空"})
                return
            with lock:
                rev, dishes = load_state()
                target = next((d for d in dishes if d["name"] == old_name), None)
                if not target:
                    self._json(404, {"error": "菜品不存在"})
                    return
                if target["by"] != user and not is_admin:
                    self._json(403, {"error": "只能修改自己添加的菜品"})
                    return
                if new_name != old_name and any(d["name"] == new_name for d in dishes):
                    self._json(400, {"error": "新菜名已存在"})
                    return
                target["name"] = new_name
                rev += 1
                save_state(rev, dishes)
            self._json(200, {"rev": rev, "dishes": dishes})
            return

        if path == "/api/dishes":
            user = self._current_user()
            if not user:
                self._json(401, {"error": "请先登录"})
                return
            body = self._read_json()
            if not isinstance(body, dict):
                self._json(400, {"error": "请求格式错误"})
                return
            names = body.get("names")
            if not isinstance(names, list):
                names = [body.get("name")]
            names = [str(n).strip() for n in names if isinstance(n, str) and n.strip()]
            if not names:
                self._json(400, {"error": "菜名不能为空"})
                return
            now = int(time.time())
            with lock:
                rev, dishes = load_state()
                existing = {d["name"] for d in dishes}
                added = [n for n in names if n not in existing]
                if added:
                    dishes = dishes + [{"name": n, "by": user, "at": now} for n in added]
                    rev += 1
                    save_state(rev, dishes)
            self._json(200, {"rev": rev, "dishes": dishes, "added": added})
            return

        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)

        # ---- 管理员接口 ----
        if path.startswith("/api/admin/users/"):
            _, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            name = path[len("/api/admin/users/"):]
            with lock:
                users = load_users()
                if name not in users:
                    self._json(404, {"error": "用户不存在"})
                    return
                if users[name].get("role") == "admin":
                    self._json(400, {"error": "不能删除管理员账号"})
                    return
                del users[name]
                save_users(users)
                # 该用户添加的菜品一并移除
                rev, dishes = load_state()
                remaining = [d for d in dishes if d["by"] != name]
                if len(remaining) != len(dishes):
                    rev += 1
                    save_state(rev, remaining)
            self._json(200, {"ok": True})
            return

        if path.startswith("/api/admin/dishes/"):
            _, is_admin = self._current_role()
            if not is_admin:
                self._json(403, {"error": "需要管理员权限"})
                return
            name = path[len("/api/admin/dishes/"):]
            with lock:
                rev, dishes = load_state()
                if any(d["name"] == name for d in dishes):
                    dishes = [d for d in dishes if d["name"] != name]
                    rev += 1
                    save_state(rev, dishes)
            self._json(200, {"rev": rev, "dishes": dishes})
            return

        # ---- 普通登录用户接口 ----
        if not self._current_user():
            self._json(401, {"error": "请先登录"})
            return
        if path == "/api/dishes":  # 清空全部
            with lock:
                rev, _ = load_state()
                rev += 1
                save_state(rev, [])
            self._json(200, {"rev": rev, "dishes": []})
        elif path.startswith("/api/dishes/"):  # 删除单个（仅本人或管理员）
            user = self._current_user()
            _, is_admin = self._current_role()
            name = path[len("/api/dishes/"):]
            with lock:
                rev, dishes = load_state()
                target = next((d for d in dishes if d["name"] == name), None)
                if target and target["by"] != user and not is_admin:
                    self._json(403, {"error": "只能删除自己添加的菜品"})
                    return
                if target:
                    dishes = [d for d in dishes if d["name"] != name]
                    rev += 1
                    save_state(rev, dishes)
            self._json(200, {"rev": rev, "dishes": dishes})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 不真正发包，只为拿到出口网卡 IP
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        print(f"端口 {port} 被占用，请换一个端口运行：python3 server.py 8899")
        sys.exit(1)
    print("今天吃什么？服务器已启动")
    print(f"  本机访问:  http://localhost:{port}")
    print(f"  局域网:    http://{lan_ip()}:{port}")
    print(f"  用户数据:  {USER_FILE}")
    print(f"  菜名数据:  {DATA_FILE}")
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
