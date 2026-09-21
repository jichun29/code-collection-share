#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 酒仙
# cron: 40 8,20 * * *

"""
酒仙小程序动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. jscode2session 使用 code 换 token（酒仙登录态）
  3. 每日签到领金币
  4. 浏览 / 分享任务完成并领奖
  5. 每日抽奖
  6. 查询金币余额，输出账号排行榜与兑换进度预估
  7. PushPlus 推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  YYB_SERVER        YYB Go 服务地址，格式：地址@微信账号标识，多账号换行分隔
  PLUSPLUS_TOKEN    PushPlus token，可选
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  JX_TARGET_GOLD    兑换目标金币数，默认 5000
  JX_DAILY_GOLD     预估日收益金币，默认 120
  JX_AREA_ID        区域 id，默认 698
  JX_LONGI          经度，默认广州
  JX_LATI           纬度，默认广州
  JX_LOTTERY_ID     每日抽奖活动 id

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import math
import os
import sys
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as url_encode

import requests

# Windows 控制台默认 GBK 无法编码 emoji/特殊字符，强制 stdout/stderr 为 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


APP_NAME = "酒仙小程序"
APPID = "wx244a18142bb0c78a"

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
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

# ==================== 酒仙业务配置 ====================
JX_APP_KEY = os.getenv("jx_app_key", "feff3071-7bff-4fda-b535-c9ebdf245f53")
JX_APP_VERSION = os.getenv("jx_app_version", "9.2.21")
JX_API_VERSION = os.getenv("jx_api_version", "1.0")
JX_APP_CHANNEL = os.getenv("jx_app_channel", "xiaochengxu")
JX_DEVICE_TYPE = os.getenv("jx_device_type", "XIAOCHENGXU")

JX_USER_BASE = "https://newappuser.jiuxian.com"
JX_HOME_BASE = "https://newapphome.jiuxian.com"
JX_PRODUCT_BASE = "https://newappproduct.jiuxian.com"
JX_SHOP_BASE = "https://shop.jiuxian.com"
JX_LOTTERY_BASE = "https://h5market2.jiuxian.com"
JX_LOTTERY_ID = os.getenv("jx_lottery_id", "65ab9a4c353447ea92fa593ecb61172d")

JX_WEBVIEW_UA = os.getenv(
    "jx_webview_ua",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows",
)

TARGET_MOUTAI = int(os.getenv("jx_target_gold", "5000"))
DAILY_EARNINGS = int(os.getenv("jx_daily_gold", "120"))

DEFAULT_LONGI = os.getenv("jx_longi", "113.26435852050781")
DEFAULT_LATI = os.getenv("jx_lati", "23.129079818725586")
DEFAULT_AREA_ID = os.getenv("jx_area_id", "698")

EQUIPMENT_TYPE_TEMPLATE = (
    '{"CPUType":"Intel(R) Core(TM) i7-8086K CPU @ 4.00GHz","benchmarkLevel":-1,'
    '"brand":"microsoft","memorySize":32682.14453125,"model":"microsoft",'
    '"platform":"windows","system":"Windows 11 x64","statusBarHeight":20,'
    '"SDKVersion":"3.17.0","PCKernelVersion":"2.5.5"}'
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c1a) XWEB/25297"
)


# ==================== 通用工具函数 ====================
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def mask_mobile(mobile: str) -> str:
    if len(mobile) >= 7:
        return f"{mobile[:3]}****{mobile[-4:]}"
    return mobile or "未知"


def mask_openid(openid: str) -> str:
    if len(openid) > 10:
        return f"{openid[:6]}****{openid[-4:]}"
    return openid or "未知"


def mask_userid(user_id: str) -> str:
    user_id = str(user_id or "")
    if len(user_id) > 6:
        return f"{user_id[:3]}****{user_id[-3:]}"
    return user_id or "未知"


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


# ==================== 品赞代理 ====================
def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
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

    # 品赞账号密码格式：IP:PORT ACCOUNT PASSWORD
    if " " in text:
        parts = text.split()
        if len(parts) == 3:
            ip_port = parts[0]
            account = parts[1]
            password = parts[2]
            if ":" in ip_port:
                host, _, port = ip_port.partition(":")
                return {
                    "host": host,
                    "port": int(port),
                    "username": account,
                    "password": password,
                }

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


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{url_encode(username)}:{url_encode(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)


# ==================== PushPlus 推送 ====================
def send_pushplus(title: str, content: str) -> None:
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
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
        print("✅ [PushPlus] 推送成功")
    except Exception as exc:
        print(f"❌ [PushPlus] 推送失败: {exc}")


# ==================== 本地 code 服务 ====================
def parse_yyb_entry(raw: str) -> Tuple[str, str]:
    """解析 YYB_SERVER 条目：地址@微信账号标识 → (server, ref)"""
    raw = raw.strip()
    if "@" not in raw:
        print(f"❌ YYB_SERVER 格式应为 地址@微信账号标识，当前值：{raw}")
        return "", ""
    server, ref = raw.split("@", 1)
    server = server.strip().lstrip("http://").lstrip("https://").rstrip("/")
    ref = ref.strip()
    if not server or not ref:
        print(f"❌ YYB_SERVER 缺少地址或微信账号标识，当前值：{raw}")
        return "", ""
    return server, ref


def get_code(entry: str) -> str | None:
    """通过 YYB Go 取码服务获取微信小程序 login code"""
    server, ref = parse_yyb_entry(entry)
    if not server or not ref:
        return None

    url = f"http://{server}/wxapp/getCode"
    print(f"🔐 [授权] 请求 YYB Go 取码: {url}")

    try:
        response = direct_session().post(
            url,
            json={"ref": ref, "app_id": APPID},
            timeout=20,
        )
        data = response.json()

        code = ((data.get("data") or {}).get("result") or {}).get("code")
        if data.get("code") != 0 or not code:
            print(f"❌ [授权] 取码失败: {json_preview(data)}")
            return None

        print("✅ [授权] 取码成功")
        return code
    except Exception as exc:
        print(f"❌ [授权] 取码异常: {exc}")
        return None


# ==================== 酒仙请求封装 ====================
def jx_common_headers(token: str | None = None, extra: Dict[str, str] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "xweb_xhr": "1",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/153/page-frame.html",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
    if extra:
        headers.update(extra)
    return headers


def jx_build_common_params(token: str = "") -> Dict[str, str]:
    return {
        "appKey": JX_APP_KEY,
        "appVersion": JX_APP_VERSION,
        "apiVersion": JX_API_VERSION,
        "areaId": DEFAULT_AREA_ID,
        "channelCode": "0, 1",
        "appChannel": JX_APP_CHANNEL,
        "cpsId": "",
        "deviceType": JX_DEVICE_TYPE,
        "pushToken": "",
        "supportWebp": "2",
        "token": token,
        "longi": DEFAULT_LONGI,
        "lati": DEFAULT_LATI,
        "equipmentType": EQUIPMENT_TYPE_TEMPLATE,
        "screenReslolution": "414x780",
        "sysVersion": "Windows 11 x64",
    }


def jx_get(server: str, url: str, token: str, proxies: Dict[str, str] | None,
           params: Dict[str, str] | None = None, extra_headers: Dict[str, str] | None = None) -> Dict[str, Any]:
    p = jx_build_common_params(token)
    if params:
        p.update(params)
    try:
        resp = request_with_proxy(
            "GET", url,
            headers=jx_common_headers(token, extra_headers),
            params=p,
            proxies=proxies,
            server=server,
        )
        return resp.json()
    except Exception as exc:
        print(f"   ⚠️ 请求异常 {url.rsplit('/', 1)[-1]}: {str(exc)[:60]}")
        return {}


# ==================== 酒仙登录态 ====================
def jx_fetch_login_state(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[bool, str, str]:
    params = jx_build_common_params("")
    params["jscode"] = code
    params["encryptedData"] = ""
    params["iv"] = ""

    try:
        resp = request_with_proxy(
            "GET",
            f"{JX_USER_BASE}/xiaochengxu/jscode2session.htm",
            headers=jx_common_headers(extra={"secure": "false"}),
            params=params,
            proxies=proxies,
            server=server,
            timeout=15,
        )
        try:
            login_data = resp.json()
        except Exception:
            print("⚠️ [登录] jscode2session 响应非JSON")
            return False, "", ""

        if login_data.get("success") != "1":
            err_code = login_data.get("errCode", "")
            err_msg = login_data.get("errMsg", "")
            print(f"❌ [登录] jscode2session失败: errCode={err_code} errMsg={err_msg}")
            return False, "", ""

        result = login_data.get("result") or {}
        token = result.get("token", "")
        user_id = str(result.get("userId", ""))
        if token and user_id:
            return True, user_id, token
        print("⚠️ [登录] 未获取到完整 token/userId")
        return False, "", ""
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return False, "", ""


# ==================== 酒仙业务任务 ====================
def jx_validate_token(server: str, token: str, proxies: Dict[str, str] | None) -> bool:
    print(f"🔑 [校验] 校验 token ({token[:8]}...)")
    data = jx_get(server, f"{JX_USER_BASE}/user/myWinebibber.htm", token, proxies)
    if str(data.get("success")) == "1":
        result = data.get("result") or {}
        mobile = (result.get("userAddressInfo") or {}).get("mobile")
        if not mobile:
            mobile = (result.get("bibberInfo") or {}).get("userName")
        if mobile:
            print(f"✅ [校验] 校验成功：用户 [{mask_mobile(str(mobile))}]")
            return True
        print("✅ [校验] 校验成功（未取到手机号）")
        return True
    # 方案B
    try:
        resp = request_with_proxy(
            "POST",
            f"{JX_USER_BASE}/user/getModuleData.htm",
            headers=jx_common_headers(token, {
                "Content-Type": "application/x-www-form-urlencoded",
                "secure": "false",
            }),
            data=jx_build_common_params(token),
            proxies=proxies,
            server=server,
            timeout=15,
        )
        if str(resp.json().get("success")) == "1":
            print("✅ [校验] 方案B校验成功（token 有效）")
            return True
    except Exception:
        pass
    print("❌ [校验] token 校验失败")
    return False


def jx_query_balance(server: str, token: str, proxies: Dict[str, str] | None, prefix: str = "") -> int:
    data = jx_get(server, f"{JX_USER_BASE}/user/myWinebibber.htm", token, proxies)
    if str(data.get("success")) == "1":
        bibber = (data.get("result") or {}).get("bibberInfo") or {}
        if not isinstance(bibber, dict):
            bibber = {}
        gold = to_int(bibber.get("goldMoney", 0))
        print(f"💰 [余额] {prefix}余额: {gold} 金币")
        return gold
    return 0


def jx_do_sign_in(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    data = jx_get(server, f"{JX_USER_BASE}/memberChannel/userSign.htm", token, proxies)
    if str(data.get("success")) == "1":
        gold = (data.get("result") or {}).get("receivedGoldNums", 0)
        print(f"🎉 [签到] 签到成功: +{gold} 金币")
    else:
        print(f"❌ [签到] 签到失败: {data.get('errMsg', '未知错误')}")


def jx_mark_task_complete(server: str, token: str, proxies: Dict[str, str] | None,
                          task_id: Any, task_token: str) -> bool:
    try:
        resp = request_with_proxy(
            "POST",
            f"{JX_SHOP_BASE}/show/wap/addJinBi.htm",
            data={"taskId": task_id, "taskToken": task_token},
            headers={
                "Host": "shop.jiuxian.com",
                "Accept": "*/*",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": JX_SHOP_BASE,
                "Referer": JX_SHOP_BASE,
                "User-Agent": JX_WEBVIEW_UA,
            },
            cookies={"token": token},
            proxies=proxies,
            server=server,
            timeout=15,
        )
        if resp.json().get("code") == 1:
            return True
    except Exception:
        pass
    print("   - ⚠️ 任务标记失败")
    return False


def jx_claim_task_reward(server: str, token: str, proxies: Dict[str, str] | None,
                         task_id: Any, task_token: str) -> None:
    data = jx_get(
        server, f"{JX_USER_BASE}/memberChannel/receiveRewards.htm", token, proxies,
        params={"taskId": task_id, "taskToken": task_token},
    )
    if str(data.get("success")) == "1":
        gold = (data.get("result") or {}).get("goldNum", 0)
        print(f"   - 🎉 获得奖励: +{gold} 金币")
    else:
        print(f"   - ❌ 领取失败: {data.get('errMsg', '未知错误')}")


def jx_do_browse_task(server: str, token: str, proxies: Dict[str, str] | None,
                      task: Dict[str, Any], task_token: str) -> None:
    try:
        url = task.get("url")
        countdown = int(task.get("countDown", 15) or 15)
        if not url:
            print("   - ⚠️ 任务缺少 url，跳过")
            return
        host = url.split("//", 1)[-1].split("/", 1)[0]
        print(f"   - 浏览页面 (等待 {countdown}s)...")
        request_with_proxy(
            "GET", url,
            headers={
                "Host": host,
                "User-Agent": JX_WEBVIEW_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
            },
            cookies={"token": token},
            proxies=proxies,
            server=server,
            timeout=15,
        )
        sleep(countdown)
        if jx_mark_task_complete(server, token, proxies, task.get("id"), task_token):
            sleep(1)
            jx_claim_task_reward(server, token, proxies, task.get("id"), task_token)
    except Exception as exc:
        print(f"   - ❌ 浏览失败: {str(exc)[:60]}")


def jx_do_share_task(server: str, token: str, proxies: Dict[str, str] | None,
                     task: Dict[str, Any], task_token: str) -> None:
    print("   - 模拟分享...")
    if jx_mark_task_complete(server, token, proxies, task.get("id"), task_token):
        sleep(1)
        jx_claim_task_reward(server, token, proxies, task.get("id"), task_token)


def jx_do_daily_tasks(server: str, token: str, proxies: Dict[str, str] | None) -> None:
    print("--- 🌟 执行日常任务 ---")
    jx_query_balance(server, token, proxies, prefix="初始")

    info_url = f"{JX_USER_BASE}/memberChannel/memberInfo.htm"
    data = jx_get(server, info_url, token, proxies)
    if str(data.get("success")) != "1":
        print(f"⚠️ [任务] 无法获取任务列表: {data.get('errMsg', '未知错误')}")
        return

    result = data.get("result") or {}
    if not isinstance(result, dict):
        return

    if not result.get("isSignTody"):
        print("📌 [签到] 执行每日签到...")
        jx_do_sign_in(server, token, proxies)
        sleep(random.randint(2, 4))
    else:
        print("👍 [签到] 今日已签到")

    data = jx_get(server, info_url, token, proxies)
    result = data.get("result") or {}
    task_info = result.get("taskChannel") or {}
    if not isinstance(task_info, dict):
        task_info = {}

    task_token = task_info.get("taskToken")
    task_list = [t for t in (task_info.get("taskList") or []) if t.get("state") in (0, 1)]

    if not task_list or not task_token:
        print("📦 [任务] 暂无可用任务")
        return

    print(f"📋 [任务] 发现 {len(task_list)} 个待办任务")
    for task in task_list:
        task_name = task.get("taskName", "未知任务")
        state = task.get("state")
        print(f"▶️ [任务] 处理: {task_name}")
        if state == 0:
            if task.get("taskType") == 1:
                jx_do_browse_task(server, token, proxies, task, task_token)
            elif task.get("taskType") == 2:
                jx_do_share_task(server, token, proxies, task, task_token)
            else:
                print("   - ⏭️ 未知任务类型，跳过")
        elif state == 1:
            print("   - 补领奖励...")
            jx_claim_task_reward(server, token, proxies, task.get("id"), task_token)
        sleep(random.randint(2, 4))


def jx_do_daily_lottery(server: str, token: str, proxies: Dict[str, str] | None) -> str:
    print("--- 🎰 酒仙每日抽奖 ---")
    try:
        body = {
            "id": JX_LOTTERY_ID,
            "isOrNotAlert": "false",
            "orderSn": "",
            "advId": "",
            "time": str(int(time.time() * 1000)),
        }
        headers = {
            "Host": "h5market2.jiuxian.com",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": JX_LOTTERY_BASE,
            "Referer": f"{JX_LOTTERY_BASE}/draw.htm?id={JX_LOTTERY_ID}&token={token}",
            "User-Agent": JX_WEBVIEW_UA,
        }
        resp = request_with_proxy(
            "POST", f"{JX_LOTTERY_BASE}/drawObject",
            data=body, headers=headers, cookies={"token": token},
            proxies=proxies, server=server, timeout=15,
        )
        data = resp.json()
        luck = data.get("luck")
        if not isinstance(luck, dict):
            print("🎯 [抽奖] 今日抽奖机会已用完")
            return "今日抽奖机会已用完"
        luck_name = luck.get("luckname") or "未中奖"
        user_coins = data.get("userCoins")
        msg = f"抽奖结果: {luck_name}" + (f" | 剩余金币: {user_coins}" if user_coins is not None else "")
        print(f"🎁 [抽奖] {msg}")
        return msg
    except Exception as exc:
        print(f"❌ [抽奖] 抽奖失败: {str(exc)[:60]}")
        return f"抽奖失败: {str(exc)[:60]}"


# ==================== 排行榜排版 ====================
def _disp_width(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def _pad(s: str, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - _disp_width(s))


# ==================== 日志排版 ====================
def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🍷 酒仙小程序动态 code 版                    ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
    print("└" + "─" * 50 + "┘")


# ==================== 单账号执行 ====================
def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "taskMsg": "-",
        "lotteryMsg": "-",
        "balance": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用品赞代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    code = get_code(server)
    if not code:
        result["error"] = "获取 code 失败"
        return result

    ok, user_id, token = jx_fetch_login_state(server, code, proxies)
    if not ok:
        result["error"] = "换取登录态失败，请检查微信是否在线/已授权酒仙"
        return result

    result["token"] = mask(token)
    print(f"✅ [登录] 登录成功：userId: {mask_userid(user_id)}")

    try:
        if not jx_validate_token(server, token, proxies):
            result["error"] = "token 校验失败"
            return result

        sleep(random.randint(1, 3))
        jx_do_daily_tasks(server, token, proxies)
        result["signMsg"] = "日常任务已执行"

        sleep(random.randint(1, 3))
        result["lotteryMsg"] = jx_do_daily_lottery(server, token, proxies)

        balance = jx_query_balance(server, token, proxies, prefix="最终")
        result["balance"] = str(balance)

        result["success"] = True
        return result
    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


# ==================== 排行榜汇总 ====================
def build_summary(results: List[Dict[str, Any]]) -> str:
    rows = [r for r in results if r["success"] and r["balance"] != "-"]
    if not rows:
        return "⚠️ 未能获取有效数据"

    lines = []
    lines.append(f"🏆 账号积分排行榜 (目标: {TARGET_MOUTAI} | 日收: {DAILY_EARNINGS}) 🏆")
    lines.append(f"{_pad('账号', 14)} | {_pad('总金币', 10)} | {_pad('缺口金币', 12)} | {'预计天数'}")

    rows.sort(key=lambda x: to_int(x["balance"]), reverse=True)
    total_gold = 0
    for idx, r in enumerate(rows, 1):
        balance = to_int(r["balance"])
        total_gold += balance
        diff = TARGET_MOUTAI - balance
        if diff > 0:
            status_msg = f"还差 {diff}"
            days_msg = f"约 {math.ceil(diff / DAILY_EARNINGS)} 天"
        else:
            status_msg = "🎉 可兑换"
            days_msg = "0 天"
        lines.append(
            f"{_pad(mask_openid(r['server']), 14)} | {_pad(balance, 10)} | "
            f"{_pad(status_msg, 12)} | {days_msg}"
        )

    lines.append(f"💰 账号金币合计: {total_gold} 金币")
    return "\n".join(lines)


# ==================== 推送内容 ====================
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍷 酒仙小程序 {len(SERVERS)} 账号任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
🌐 代理：{res["proxyStatus"]}
📡 出口IP：{res["proxyIp"]}
🔐 Token：{res["token"]}
📝 签到：{res["signMsg"]}
📦 任务：{res["taskMsg"]}
🎰 抽奖：{res["lotteryMsg"]}
💰 余额：{res["balance"]} 金币
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    summary = build_summary(results)
    content += "\n" + summary + "\n"
    return content


# ==================== 主流程 ====================
def main() -> None:
    log_title()

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "taskMsg": "-",
                "lotteryMsg": "-",
                "balance": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🍷 酒仙任务执行完成                          ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    print()
    print(build_summary(results))

    send_pushplus("🍷 酒仙小程序任务完成", build_notify(results))


if __name__ == "__main__":
    main()


