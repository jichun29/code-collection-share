#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: iQOO社区
# cron: 20 8,20 * * *

"""
iQOO社区小程序积分 动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /v3/users/vivo/mini 使用 code 换 token
  3. 获取用户信息
  4. 每日任务（浏览/点赞/分享/评论/发帖删除）
  5. 抽奖
  6. 签到
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 服务地址，格式：地址@微信账号标识，多账号换行分隔
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import hashlib
import hmac
import json
import os
import random
import re
import time
import base64
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests


APP_NAME = "iQOO社区小程序"

MINI_APP_ID = "wxcf4266fbc9463132"
PAGE_VERSION = "256"
API_BASE = "https://bbs-api.iqoo.com/api/"
SIGN_SECRET = "2618194b0ebb620055e19cf9811d3c13"
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iqoocookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf254173b) XWEB/19027"
)

_YYB_SERVER_RAW = os.getenv("YYB_SERVER", "")
SERVERS = [line.strip() for line in _YYB_SERVER_RAW.splitlines() if line.strip()]
if not SERVERS:
    print("❌ 未配置环境变量 YYB_SERVER（格式：地址@微信账号标识，多账号换行分隔）")
    exit(1)
print(f"✅ 读取到 {len(SERVERS)} 个 YYB Go 账号")

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "https://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30
_user_idx = 0


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds):
    time.sleep(seconds)


def log(*args):
    print(f"[{APP_NAME}]", *args)


def mask_name(value):
    text = str(value or "")
    return f"{text[:4]}***{text[-3:]}" if len(text) > 10 else text


def short_token(token=""):
    value = str(token or "").replace("Bearer ", "", 1)
    return f"{value[:6]}***{value[-6:]}" if value else ""


def is_success(result):
    try:
        return int(result.get("Code", -1)) == 0
    except (TypeError, ValueError):
        return False


def is_token_error(message):
    return bool(re.search(r"-4011|401|403|token|登录|授权|请先登录|跳转到登录页|访客没有权限", str(message or ""), re.I))


def json_preview(data, limit=800):
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def read_token_cache():
    try:
        if not os.path.exists(TOKEN_CACHE_FILE):
            return {}
        with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_token_cache(cache):
    try:
        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"写入token缓存失败: {e}")


# ==================== 品赞代理 ====================

def direct_session():
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text):
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info):
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    log(f"[代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {"http": proxy_url, "https": proxy_url}


def validate_proxy(proxies):
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            log(f"[代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        log(f"[代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(server):
    if not PROXY_API:
        log(f"[代理] {server} 未配置 PROXY_API，使用直连")
        return None, ""

    log(f"[代理] {server} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                log(f"[代理] 第 {index} 次代理解析失败")
                continue

            log(f"[代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            log(f"[代理] 第 {index} 次代理不可用")
        except Exception as exc:
            log(f"[代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    log("[代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(method, url, *, proxies=None, server="", **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            log(f"[代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            log("[兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)

# ==================== PushPlus推送 ====================

def send_pushplus(title, content):
    if not PLUSPLUS_TOKEN:
        log("[PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        log("[PushPlus] 推送成功")
    except Exception as exc:
        log(f"[PushPlus] 推送失败: {exc}")


def build_notify(results):
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"iQOO社区任务结果\n\n总结: {success_count} 成功 / {fail_count} 失败\n时间: {now_text()}\n"
    content += "=" * 40 + "\n"

    for idx, res in enumerate(results, 1):
        status_text = "成功" if res["success"] else "失败"

        content += f"\n账号 {idx}\n"
        content += f"来源: {res['server']}\n"
        content += f"代理: {res['proxyStatus']}\n"
        content += f"出口IP: {res['proxyIp']}\n"
        content += f"Token: {res['token']}\n"
        content += f"用户: {res['userName']}\n"
        content += f"酷币: {res['score']}\n"
        content += f"签到: {res['signMsg']}\n"
        content += f"抽奖: {res['drawMsg']}\n"
        content += f"结果: {status_text}\n"

        if not res["success"]:
            content += f"原因: {res['error']}\n"

        content += "=" * 40 + "\n"

    return content


# ==================== 8088 code服务 ====================

def parse_yyb_entry(raw):
    """解析 YYB_SERVER 条目：地址@微信账号标识 → (server, ref)"""
    raw = raw.strip()
    if "@" not in raw:
        log(f"❌ YYB_SERVER 格式应为 地址@微信账号标识，当前值：{raw}")
        return "", ""
    server, ref = raw.split("@", 1)
    server = server.strip().lstrip("http://").lstrip("https://").rstrip("/")
    ref = ref.strip()
    if not server or not ref:
        log(f"❌ YYB_SERVER 缺少地址或微信账号标识，当前值：{raw}")
        return "", ""
    return server, ref


def get_code(entry):
    """通过 YYB Go 取码服务获取微信小程序 login code"""
    server, ref = parse_yyb_entry(entry)
    if not server or not ref:
        return None

    url = f"http://{server}/wxapp/getCode"
    log(f"[授权] 请求 YYB Go 取码: {url}")

    try:
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": MINI_APP_ID},
            timeout=20,
        )
        data = response.json()

        code = ((data.get("data") or {}).get("result") or {}).get("code")
        if data.get("code") != 0 or not code:
            log(f"[授权] 取码失败: {json_preview(data)}")
            return None

        log("[授权] 取码成功")
        return code
    except Exception as exc:
        log(f"[授权] 取码异常: {exc}")
        return None


# ==================== Task ====================

class Task:
    def __init__(self, server, proxies):
        global _user_idx
        _user_idx += 1
        self.index = _user_idx
        self.server = server
        self.proxies = proxies
        self.openid = server
        self.token = ""
        self.refresh_token = ""
        self.user_id = ""
        self.user_info = {}
        self.thread_list = []
        self.visitor = hashlib.md5((self.openid or str(int(time.time()))).encode()).hexdigest()
        self._sign_msg = "-"
        self._draw_msg = "-"
        self._login_error = ""

    def run(self):
        result = {
            "server": self.server,
            "success": False,
            "proxyStatus": "使用专属代理" if self.proxies else "使用直连",
            "proxyIp": "-",
            "token": "-",
            "userName": "-",
            "score": "-",
            "signMsg": "-",
            "drawMsg": "-",
            "error": "",
        }

        try:
            cached = self.get_cached_token()
            if cached and cached.get("accessToken"):
                self.apply_token(cached)
                log(f"账号[{self.index}] 使用缓存token: {short_token(self.token)}")
                if not self.check_token():
                    self.remove_cached_token()
                    log(f"账号[{self.index}] 缓存token失效，重新登录")

            if not self.token:
                self.login_by_wx_code()
                if not self.token:
                    result["error"] = self._login_error or "登录失败"
                    result["signMsg"] = self._sign_msg
                    result["drawMsg"] = self._draw_msg
                    return result

            self.user_info_request()
            result["token"] = short_token(self.token)
            result["userName"] = mask_name(
                self.user_info.get("nickname")
                or self.user_info.get("username")
                or self.user_info.get("userName")
                or self.user_id
            )
            score_val = self.user_info.get("score")
            if score_val is None:
                score_val = self.user_info.get("points")
            if score_val is None:
                score_val = self.user_info.get("coolCoin", "未知")
            result["score"] = score_val

            daily_tasks = self.fetch_daily_tasks()
            if not daily_tasks:
                log(f"账号[{self.index}] 未获取到每日任务，跳过任务执行")
            else:
                need_threads = any(
                    t.get("upper_limit") != "不限次数"
                    and (int(t.get("upper_limit", 0)) - t.get("isFinal", 0)) > 0
                    and t.get("rule") in ("view_thread", "like", "share", "create_post")
                    for t in daily_tasks
                )
                if need_threads:
                    self.get_thread_list()

                for task in daily_tasks:
                    rule = task.get("rule")
                    done = task.get("isFinal", 0)
                    upper = task.get("upper_limit")
                    if upper == "不限次数":
                        continue
                    max_count = int(upper)
                    remaining = max_count - done
                    if remaining <= 0:
                        continue

                    log(f"账号[{self.index}] 任务[{task.get('access')}] 已完成{done}/{max_count}，还需{remaining}次")

                    if rule == "view_thread":
                        for item in self.thread_list:
                            if remaining <= 0:
                                break
                            self.view_post(item.get("threadId"))
                            remaining -= 1
                            sleep(3)
                    elif rule == "like":
                        for item in self.thread_list:
                            if remaining <= 0:
                                break
                            self.like_post(item.get("threadId"), item.get("postId"))
                            remaining -= 1
                            sleep(3)
                    elif rule == "share":
                        for item in self.thread_list:
                            if remaining <= 0:
                                break
                            self.share_post(item.get("threadId"))
                            remaining -= 1
                            sleep(3)
                    elif rule == "create_post":
                        if self.thread_list:
                            self.comment_post(self.thread_list[0].get("threadId"))
                            remaining -= 1
                        else:
                            log(f"账号[{self.index}] 无帖子可评论")
                    elif rule == "create_thread":
                        self.create_and_delete_thread()

            self.get_draw_num()
            self.sign_in()

            result["success"] = True
        except Exception as e:
            result["error"] = str(e)

        result["signMsg"] = self._sign_msg
        result["drawMsg"] = self._draw_msg
        return result
    def get_cached_token(self):
        cache = read_token_cache()
        return cache.get(self.openid) or None

    def save_cached_token(self):
        if not self.token:
            return
        cache = read_token_cache()
        cache[self.openid] = {
            "accessToken": self.token,
            "refreshToken": self.refresh_token,
            "userId": self.user_id,
            "userInfo": self.user_info,
            "visitor": self.visitor,
            "updatedAt": datetime.now().isoformat(),
        }
        write_token_cache(cache)

    def remove_cached_token(self):
        cache = read_token_cache()
        if cache.get(self.openid):
            del cache[self.openid]
            write_token_cache(cache)
        self.token = ""
        self.refresh_token = ""
        self.user_id = ""
        self.user_info = {}

    def apply_token(self, data=None):
        data = data or {}
        self.token = data.get("accessToken") or data.get("token") or ""
        self.refresh_token = data.get("refreshToken") or ""
        user_info = data.get("userInfo") or data.get("user") or {}
        self.user_id = data.get("userId") or data.get("uid") or user_info.get("userId") or user_info.get("uid") or ""
        self.user_info = user_info
        self.visitor = data.get("visitor") or self.visitor

    def query_string(self, data=None):
        data = data or {}
        parts = []
        for key in sorted(data.keys()):
            value = data[key]
            if value is None:
                continue
            parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
        return "&".join(parts)

    def get_sign(self, method, api_path, data=None):
        data = data or {}
        upper_method = method.upper()
        timestamp = int(time.time())
        query = self.query_string(data) if upper_method == "GET" else ""
        body = "" if upper_method == "GET" else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        raw = f"{upper_method}&/api/{api_path}&{query}&{body}&appid=1002&timestamp={timestamp}"
        signature = base64.b64encode(hmac.new(SIGN_SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode()
        return f"IQOO-HMAC-SHA256 appid=1002,timestamp={timestamp},signature={signature}"

    def build_headers(self, method, api_path, data, extra=None):
        extra = extra or {}
        headers = {
            "Authorization": f"Bearer {self.token}" if self.token else "Bearer ",
            "X-Visitor": self.visitor,
            "X-Platform": "mini",
            "SIGN": self.get_sign(method, api_path, data),
            "Content-Nonce": "",
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{MINI_APP_ID}/{PAGE_VERSION}/page-frame.html",
        }
        headers.update(extra)
        return headers

    def request(self, api_path, data=None, options=None):
        data = data or {}
        options = options or {}
        method = (options.get("method") or "POST").upper()
        url = requests.compat.urljoin(API_BASE, api_path)
        headers = self.build_headers(method, api_path, data, options.get("headers") or {})

        if method == "GET":
            response = request_with_proxy(
                "GET", url,
                headers=headers,
                params=data,
                proxies=self.proxies,
                server=self.server,
            )
        else:
            headers["Content-Type"] = "application/json"
            body_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            response = request_with_proxy(
                method, url,
                headers=headers,
                data=body_str.encode("utf-8"),
                proxies=self.proxies,
                server=self.server,
            )

        try:
            result = response.json()
        except Exception:
            raise Exception(f"JSON解析失败: {response.text[:300]}")

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {json_preview(result)}")
        if not is_success(result):
            raise Exception(f"{result.get('Code', '')} {result.get('Message') or json_preview(result)}".strip())
        return result

    def fetch_daily_tasks(self):
        try:
            result = self.request("v5/users/tasks", {}, {"method": "GET"})
            return (result.get("Data") or {}).get("perDayData") or []
        except Exception as e:
            log(f"账号[{self.index}] 获取每日任务失败: {e}")
            return []

    def login_by_wx_code(self):
        try:
            code = get_code(self.server)
            if not code:
                raise Exception("获取code失败")
            sleep(1.5)

            encrypted_data = base64.b64encode(os.urandom(48)).decode()
            iv = base64.b64encode(os.urandom(16)).decode()

            payload = {
                "code": code,
                "encryptedData": encrypted_data,
                "iv": iv,
                "from": 46,
            }
            result = self.request("v3/users/vivo/mini", payload)
            data = result.get("Data") or {}
            token = data.get("accessToken") or data.get("token") or ""
            if not token:
                raise Exception(f"登录响应未返回token: {json_preview(result)}")
            self.token = token
            self.refresh_token = data.get("refreshToken") or ""
            self.user_id = data.get("userId") or data.get("uid") or ""
            self.user_info = data.get("user") or data
            self.save_cached_token()
            name = self.user_info.get("nickname") or self.user_info.get("username") or self.user_id
            log(f"账号[{self.index}] CODE登录成功: {mask_name(name)}")
        except Exception as e:
            self._login_error = str(e)
            log(f"账号[{self.index}] CODE登录失败: {e}")
    def check_token(self):
        try:
            if not self.user_id:
                return False
            self.request("v3/user", {"userId": self.user_id}, {"method": "GET"})
            return True
        except Exception:
            return False

    def user_info_request(self):
        if not self.user_id:
            return
        try:
            result = self.request("v3/user", {"userId": self.user_id}, {"method": "GET"})
            self.user_info = result.get("Data") or self.user_info
            self.save_cached_token()
            name = self.user_info.get("nickname") or self.user_info.get("username") or self.user_info.get("userName") or self.user_id
            score = self.user_info.get("score")
            if score is None:
                score = self.user_info.get("points")
            if score is None:
                score = self.user_info.get("coolCoin", "未知")
            log(f"账号[{self.index}] 用户:{mask_name(name)} 酷币:{score}")
        except Exception as e:
            message = str(e)
            log(f"账号[{self.index}] 获取用户信息失败:{message}")
            if is_token_error(message):
                self.remove_cached_token()

    def get_draw_num(self):
        try:
            result = self.request("v3/today.draw.count", {}, {"method": "GET"})
            if (result.get("Data") or {}).get("count") == 0:
                self.draw()
            else:
                self._draw_msg = "今日已抽奖"
                log(f"账号[{self.index}] 今日已抽奖")
        except Exception as e:
            self._draw_msg = f"查询抽奖失败: {e}"
            log(f"账号[{self.index}] 查询抽奖次数失败:{e}")

    def draw(self):
        try:
            result = self.request("v3/luck.draw", {})
            prize = (result.get("Data") or {}).get("prize_name") or "奖励"
            self._draw_msg = f"获得{prize}"
            log(f"账号[{self.index}] 抽奖成功 获得{prize}")
        except Exception as e:
            self._draw_msg = f"抽奖失败: {e}"
            log(f"账号[{self.index}] 抽奖失败:{e}")

    def sign_in(self):
        try:
            result = self.request("v3/sign", {"from": "group"})
            data = result.get("Data") or {}
            self._sign_msg = f"已签到{data.get('serialDays')}天 获得积分{data.get('score')} 当前积分{data.get('scoreCount')}"
            log(f"账号[{self.index}] 当前已签到{data.get('serialDays')}天 获得积分{data.get('score')} 当前积分{data.get('scoreCount')}")
        except Exception as e:
            message = str(e)
            if re.search(r"已签|重复|今日|13006", message):
                self._sign_msg = "今日已签到"
                log(f"账号[{self.index}] 今日已签到")
                return
            self._sign_msg = f"签到失败: {message}"
            log(f"账号[{self.index}] 签到失败:{message}")
            if is_token_error(message):
                self.remove_cached_token()
    def like_post(self, thread_id, post_id):
        try:
            self.request("v3/posts.update", {"id": thread_id, "postId": post_id, "data": {"attributes": {"isLiked": True}}})
            log(f"账号[{self.index}] 帖子点赞成功")
        except Exception as e:
            log(f"账号[{self.index}] 帖子点赞失败:{e}")
        try:
            self.request("v3/posts.update", {"id": thread_id, "postId": post_id, "data": {"attributes": {"isLiked": False}}})
        except Exception:
            pass

    def share_post(self, thread_id):
        try:
            self.request("v3/thread.share", {"threadId": thread_id})
            log(f"账号[{self.index}] 帖子分享成功")
        except Exception as e:
            log(f"账号[{self.index}] 帖子分享失败:{e}")

    def view_post(self, thread_id):
        try:
            self.request("v3/view.count", {"threadId": thread_id, "type": 0}, {"method": "GET"})
            log(f"账号[{self.index}] 帖子浏览成功")
        except Exception as e:
            log(f"账号[{self.index}] 帖子浏览失败:{e}")

    def comment_post(self, thread_id):
        try:
            self.request("v3/posts.create", {"id": thread_id, "type": 0, "content": "666", "source": "", "attachments": []})
            log(f"账号[{self.index}] 帖子评论成功")
        except Exception as e:
            log(f"账号[{self.index}] 帖子评论失败:{e}")

    def create_and_delete_thread(self):
        try:
            result = self.request("v3/thread.create", {
                "categoryId": 16,
                "content": {
                    "text": "<p>设计内核传递IQO0\"不止竞技\"的理念，将自然美</p><p>融入数码产品。既有电竞产品标志性光效元素，可</p><p>兼具清新简约气路，无论是沉浸式游戏、日常通勤</p><p>聆听音乐，都能适配多元场景，实现性能美学与实</p><p>用体验的平衡</p>"
                },
                "position": {},
                "price": 0,
                "freeWords": 0,
                "attachmentPrice": 0,
                "draft": 0,
                "anonymous": 0,
                "topicId": "",
                "source": "",
                "videoId": "",
                "type": 7,
                "attachments": [],
                "isAigc": False
            })
            thread_id = (result.get("Data") or {}).get("threadId")
            if not thread_id:
                log(f"账号[{self.index}] 发帖成功但未获取到threadId")
                return
            log(f"账号[{self.index}] 发帖成功, threadId: {thread_id}")
            sleep(2)
            self.request("v3/thread.delete", {"threadId": thread_id, "message": ""})
            log(f"账号[{self.index}] 帖子已删除")
        except Exception as e:
            log(f"账号[{self.index}] 发帖/删除失败: {e}")

    def get_thread_list(self):
        try:
            result = self.request("v3/thread.list", {
                "scope": 5,
                "page": 1,
                "perPage": 20,
                "filter[sort]": 4,
                "filter[essence]": 1,
                "sequence": 0,
            }, {"method": "GET"})
            self.thread_list = (result.get("Data") or {}).get("pageData") or []
            log(f"账号[{self.index}] 获取到{len(self.thread_list)}条帖子")
        except Exception as e:
            log(f"账号[{self.index}] 获取帖子列表失败:{e}")
            self.thread_list = []

# ==================== 主入口 ====================

def main():
    results = []

    for server in SERVERS:
        log(f"\n{'=' * 50}")
        log(f"  开始处理: {server}")
        log(f"{'=' * 50}")

        proxies, proxy_ip = get_valid_proxy(server)
        sleep(PROXY_FETCH_INTERVAL)

        task = Task(server, proxies)
        result = task.run()
        result["proxyIp"] = proxy_ip or "-"
        results.append(result)

        sleep(5)

    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count
    log(f"\n{'=' * 50}")
    log(f"iQOO社区任务执行完成")
    log(f"成功: {success_count}")
    log(f"失败: {fail_count}")
    log(f"结束: {now_text()}")

    send_pushplus("iQOO社区任务完成", build_notify(results))


if __name__ == "__main__":
    main()