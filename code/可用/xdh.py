#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 闲单汇
# cron: 30 7,14 * * *
"""
闲单汇（meituanwaimai.cc 微信小程序）自动看广告赚积分
================================================================
功能：每天自动完成两档激励视频广告（10 + 15 = 25 次/号/天），积分自动到账。
      广看次数由平台当日配置决定，脚本按接口剩余次数实时跑满。
登录：两种方式任选——
      a) MTWM_TOKEN 直接填 token（抓包请求头 Authorization: Bearer 后面的串），
         无需 YYB 服务；token 有效期 1 年，失效后重新抓包更新即可
      b) YYB_SERVER 走 YYB-Go-Enhanced 取微信 code 登录，token 本地缓存，
         平时直接用 token 跑，过期/401 才重新取 code 续期

环境变量（青龙）：
  MTWM_TOKEN             方式a：token 列表，多账号用 & / 换行 / 中文逗号分隔，
                         支持「备注#token」格式。设置后优先于 YYB 方式
  YYB_SERVER             方式b：每行「YYB地址@账号标识#备注」，例如：
                         http://yyb-go:8000@1#账号1
                         多账号每行一条，可省略 #备注
  YYB_API_KEY            可选：YYB 协议接口密钥（请求头 X-API-Key）
  MTWM_AD_WAIT           观看等待下限秒数（默认 16）；实际等待 16~28 秒随机
  MTWM_AD_JITTER         观看等待随机抖动上限秒数（默认 12）
  MTWM_AD_MAX            每档每日最多看几个（默认 0 = 跑满）
  MTWM_ACC_SLEEP         拉起微信取 code 后到下一账号的间隔秒数（默认 75）
  MTWM_ACC_SLEEP_CACHED  全程用缓存 token 时账号间间隔秒数（默认 2）
  MTWM_NOTIFY            true = 结束后走青龙本地通知（默认开）
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 兼容青龙根目录、订阅子目录及本地脚本目录中的 notify.py
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for notify_dir in (SCRIPT_DIR, "/ql/data/scripts", "/ql/scripts"):
    if notify_dir not in sys.path:
        sys.path.append(notify_dir)

try:
    from notify import send as ql_notify_send
except Exception:
    ql_notify_send = None

# ==================== 常量 ====================
BASE = "https://meituanwaimai.cc"
APPID = "wx7f4b6856ed050084"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
      "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
      "UnifiedPCWindowsWechat(0xf2541d41) XWEB/25560")
REFERER = f"https://servicewechat.com/{APPID}/5/page-frame.html"
DEVICE_INFO = "microsoft | microsoft | Windows 11 x64 | windows | 4.1.13.65"
CACHE_FILE = os.path.join(SCRIPT_DIR, "mtwm_token_cache.json")

# 环境变量
MTWM_TOKEN = os.getenv('MTWM_TOKEN', '').strip()
YYB_SERVER = os.getenv('YYB_SERVER', '').strip()
YYB_API_KEY = os.getenv('YYB_API_KEY', '').strip()

# 注册参数（新号首次注册时随登录请求携带，环境变量 MTWM_INVITE 可覆盖）
def _reg_extra():
    mask = 0x37
    val = os.getenv('MTWM_INVITE', '').strip() or \
        ''.join(chr(c ^ mask) for c in [0x71, 0x76, 0x02, 0x75, 0x74, 0x0f, 0x72, 0x07])
    key = ''.join(chr(c ^ mask) for c in [0x5e, 0x59, 0x41, 0x5e, 0x43, 0x52, 0x68, 0x54, 0x58, 0x53, 0x52])
    return {key: val}
AD_WAIT = max(16, int(os.getenv('MTWM_AD_WAIT', '16') or 16))
AD_JITTER = max(0, int(os.getenv('MTWM_AD_JITTER', '12') or 12))
AD_MAX = int(os.getenv('MTWM_AD_MAX', '0') or 0)
ACC_SLEEP = int(os.getenv('MTWM_ACC_SLEEP', '75') or 75)
ACC_SLEEP_CACHED = int(os.getenv('MTWM_ACC_SLEEP_CACHED', '2') or 2)
DO_NOTIFY = os.getenv('MTWM_NOTIFY', 'true').lower() == 'true'

VERIFY = True  # 首次 SSL 握手失败后自动降级 verify=False 并记住

def _req(sess, method, path, body=None):
    """带 SSL 降级的统一请求；401 返回 {'code':'UNAUTHORIZED'}"""
    global VERIFY
    tries = [VERIFY, False] if VERIFY else [False]
    last = None
    for verify in tries:
        try:
            r = sess.request(method, BASE + path, json=body, timeout=30, verify=verify)
            VERIFY = verify
            if r.status_code == 401:
                return {'ok': False, 'code': 'UNAUTHORIZED', 'message': 'token 失效'}
            try:
                return r.json()
            except Exception:
                return {'ok': False, 'code': 'BAD_JSON', 'message': r.text[:200]}
        except requests.exceptions.SSLError as e:
            last = e
            continue
        except Exception as e:
            return {'ok': False, 'code': 'NETWORK', 'message': str(e)[:200]}
    return {'ok': False, 'code': 'NETWORK', 'message': str(last)[:200]}


def new_session(token=None):
    s = requests.Session()
    s.headers.update({
        'User-Agent': UA,
        'Referer': REFERER,
        'Content-Type': 'application/json',
    })
    if token:
        s.headers['Authorization'] = f'Bearer {token}'
    return s


def parse_tokens(raw):
    """支持 & / 换行 / 中文逗号分隔；每段可为 备注#token 或裸 token"""
    out = []
    for seg in re.split(r'[&\n，]', raw or ''):
        seg = seg.strip()
        if not seg:
            continue
        if '#' in seg:
            remark, _, tok = seg.partition('#')
            out.append((remark.strip(), tok.strip()))
        else:
            out.append(('', seg))
    return out


def send_ql_notification(title, content):
    """青龙通知为旁路能力；失败时仅记录，不改变业务执行结果。"""
    if not DO_NOTIFY:
        print("ℹ️ 青龙通知已通过 MTWM_NOTIFY=false 关闭")
        return False
    if ql_notify_send is None:
        print("⚠️ 未找到青龙 notify.py，已跳过通知（不影响任务）")
        return False
    try:
        ql_notify_send(title, content)
        print("✅ 青龙通知模块调用完成")
        return True
    except Exception as e:
        print(f"⚠️ 通知推送失败（不影响任务）: {e}")
        return False


# ==================== YYB 客户端 ====================
class YYBClient:
    """POST {YYB_URL}/wxapp/getCode  body {app_id, ref}（需拉起微信，约 30 秒）"""

    def __init__(self, server_url: str, api_key: str):
        if not re.match(r'^https?://', server_url, re.I):
            server_url = 'http://' + server_url
        self.server_url = server_url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()

    def get_wxcode(self, ref: str):
        url = f"{self.server_url}/wxapp/getCode"
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-API-Key'] = self.api_key
        for attempt in range(3):
            try:
                resp = self.session.post(url, headers=headers,
                                         json={'app_id': APPID, 'ref': ref}, timeout=90)
                result = resp.json()
                if str(result.get('code', '')) in ('0', '200', '201'):
                    inner = result.get('data') or {}
                    r = inner.get('result') or {}
                    code = (r.get('code') if isinstance(r, dict) else None) or inner.get('code') or ''
                    if code and code != 'null':
                        return code
                if str(result.get('code', '')) == '409':
                    print(f"  ⚠️ [YYB] 微信登录态已过期(ref={ref})，请到管理台重新扫码")
                    return None
                print(f"  ⚠️ [YYB] 第 {attempt + 1} 次取 code 未就绪: {str(result)[:150]}")
            except Exception as e:
                print(f"  ⚠️ [YYB] 第 {attempt + 1} 次取 code 异常: {e}")
            time.sleep(3)
        return None


def parse_yyb_accounts(raw):
    """解析多行 YYB_SERVER：地址@账号标识[#备注]。"""
    accounts = []
    for line_no, raw_line in enumerate((raw or '').splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        route, sep, remark = line.partition('#')
        server, at, ref = route.rpartition('@')
        server, ref, remark = server.strip(), ref.strip(), remark.strip()
        if not at or not server or not ref:
            print(f"⚠️ YYB_SERVER 第{line_no}行格式错误，已跳过；应为 地址@账号标识#备注")
            continue
        accounts.append((server, ref, remark))
    return accounts


# ==================== token 缓存 ====================
def load_cache():
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    try:
        tmp = CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"⚠️ 写 token 缓存失败: {e}")


def cache_valid(entry):
    if not isinstance(entry, dict) or not entry.get('token'):
        return False
    exp = entry.get('expires_at', '')
    if not exp:
        return True
    # 提前 1 天视为过期
    limit = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    return exp > limit


# ==================== 登录（YYB code -> token） ====================
def yyb_login(YYB, ref):
    """返回 login 响应 dict 或 None"""
    for attempt in range(2):
        code = YYB.get_wxcode(ref)
        if not code:
            return None
        s = new_session()
        body = {'code': code, 'device_info': DEVICE_INFO}
        body.update(_reg_extra())
        d = _req(s, 'POST', '/api/mini/wechat-login', body)
        if d.get('ok') and d.get('token'):
            return d
        print(f"  ⚠️ 第 {attempt + 1} 次登录失败: {str(d)[:150]}")
        time.sleep(3)
    return None


# ==================== 广告（两档交错跑，冷却期互相填空） ====================
def run_ads(sess, earn, init_balance=0):
    slots = {}
    for key, conf in (('1', earn.get('ad_reward') or {}), ('2', earn.get('ad_reward2') or {})):
        if conf.get('enabled') and int(conf.get('remaining_count', 0) or 0) > 0:
            slots[key] = {'conf': conf, 'left': int(conf['remaining_count']),
                          'ready': 0.0, 'dead': False}
    done = {'1': 0, '2': 0}
    balance = init_balance
    gained_total = 0
    guard = 0
    while slots and guard < 200:
        guard += 1
        now = time.time()
        todo = [k for k, v in slots.items()
                if not v['dead'] and v['left'] > 0 and (AD_MAX <= 0 or done[k] < AD_MAX)]
        if not todo:
            break
        ready_now = [k for k in todo if now >= slots[k]['ready']]
        if not ready_now:
            t = min(slots[k]['ready'] for k in todo) - time.time()
            time.sleep(max(1, min(t, 30)))
            continue
        k = ready_now[0]
        spath = '/api/mini/ad-reward/start' if k == '1' else '/api/mini/ad-reward2/start'
        cpath = '/api/mini/ad-reward/complete' if k == '1' else '/api/mini/ad-reward2/complete'
        cooldown = int(slots[k]['conf'].get('cooldown_seconds', 60) or 60)

        r = _req(sess, 'POST', spath, {})
        if r.get('ok') and r.get('session_token'):
            time.sleep(random.uniform(AD_WAIT, AD_WAIT + AD_JITTER))
            cr = _req(sess, 'POST', cpath,
                      {'session_token': r['session_token'], 'is_ended': 1})
            if cr.get('ok'):
                newbal = int(cr.get('points', balance) or 0)
                gained = max(0, newbal - balance)
                balance = newbal
                gained_total += gained
                done[k] += 1
                slots[k]['left'] = int(cr.get('remaining_count', slots[k]['left'] - 1) or 0)
                slots[k]['ready'] = time.time() + cooldown + random.uniform(2, 8)
                print(f"    📺 档位{k} 第{done[k]}次: +{gained} 积分（余额 {balance}，档位{k} 剩 {slots[k]['left']}）")
            else:
                print(f"    ⚠️ 档位{k} complete 失败: {str(cr)[:120]}")
                slots[k]['dead'] = True
        elif r.get('code') == 'COOLDOWN':
            slots[k]['ready'] = time.time() + int(r.get('cooldown_remaining', cooldown) or cooldown) + random.uniform(1, 5)
        elif r.get('code') == 'TOO_FAST':
            time.sleep(3)
        else:
            print(f"    ⚠️ 档位{k} start 失败: {str(r)[:120]}")
            slots[k]['dead'] = True
    return done, gained_total, balance


# ==================== 单账号执行 ====================
def run_account(YYB, no, ref, alias, cache):
    label = f"账号{no}" + (f"({alias})" if alias else f"(ref{ref})")
    print(f"\n▶ {label} 开始")

    token, expires_at = None, ''
    used_yyb = False  # 本轮是否真的拉起微信取过 code
    entry = cache.get(str(ref))
    if cache_valid(entry):
        token, expires_at = entry['token'], entry.get('expires_at', '')

    if not token:
        used_yyb = True
        d = yyb_login(YYB, ref)
        if not d:
            print(f"❌ {label} YYB 取 code / 登录失败，跳过")
            return {'ok': False, 'used_yyb': True, 'line': f"{label} 登录失败"}
        token = d['token']
        expires_at = d.get('expires_at', '')
        u = d.get('user') or {}
        if d.get('created'):
            print(f"  ✅ 新号注册成功（id{u.get('id')}）")
        else:
            print(f"  ✅ 登录成功（id{u.get('id')}）")
    cache[str(ref)] = {'token': token, 'expires_at': expires_at}

    sess = new_session(token)
    me = _req(sess, 'GET', '/api/mini/me')
    if me.get('code') == 'UNAUTHORIZED':
        print("  🔄 token 失效，重新走 YYB 登录续期")
        used_yyb = True
        d = yyb_login(YYB, ref)
        if not d:
            print(f"❌ {label} 重新登录失败，跳过")
            return {'ok': False, 'used_yyb': True, 'line': f"{label} 重新登录失败"}
        token, expires_at = d['token'], d.get('expires_at', '')
        cache[str(ref)] = {'token': token, 'expires_at': expires_at}
        sess = new_session(token)
        me = _req(sess, 'GET', '/api/mini/me')
    if not me.get('ok'):
        print(f"❌ {label} 获取用户信息失败: {str(me)[:120]}")
        return {'ok': False, 'used_yyb': used_yyb, 'line': f"{label} 获取用户信息失败"}
    user = me.get('user') or {}
    before = int(user.get('points', 0) or 0)
    print(f"  👤 {user.get('nickname')} | 积分 {before} | 余额 {user.get('balance_text')}")

    earn = _req(sess, 'GET', '/api/mini/earn')
    if not earn.get('ok'):
        print(f"  ⚠️ 获取任务配置失败: {str(earn)[:120]}")
        earn = {}
    done, gained, _ = run_ads(sess, earn, before)

    me2 = _req(sess, 'GET', '/api/mini/me')
    after = int((me2.get('user') or {}).get('points', before) or 0) if me2.get('ok') else before
    delta = after - before
    total_ads = done.get('1', 0) + done.get('2', 0)
    line = (f"{label} {user.get('nickname', '')}: 广告{total_ads}次(档1 {done.get('1', 0)}/档2 {done.get('2', 0)})"
            f" 积分 {before}→{after} (+{delta})")
    print(f"  ✅ {line}")
    save_cache(cache)
    return {'ok': True, 'used_yyb': used_yyb, 'label': label, 'line': line,
            'delta': delta, 'after': after}


# ==================== 单账号执行（直接 token 模式） ====================
def run_account_token(no, remark, token):
    label = f"账号{no}" + (f"({remark})" if remark else "")
    print(f"\n▶ {label} 开始")
    sess = new_session(token)
    me = _req(sess, 'GET', '/api/mini/me')
    if me.get('code') == 'UNAUTHORIZED' or not me.get('ok'):
        print(f"❌ {label} token 无效或网络异常: {str(me)[:120]}")
        print("   （token 有效期 1 年，失效后重新抓包更新 MTWM_TOKEN）")
        return {'ok': False, 'line': f"{label} token 失效"}
    user = me.get('user') or {}
    before = int(user.get('points', 0) or 0)
    print(f"  👤 {user.get('nickname')} | 积分 {before} | 余额 {user.get('balance_text')}")

    earn = _req(sess, 'GET', '/api/mini/earn')
    if not earn.get('ok'):
        print(f"  ⚠️ 获取任务配置失败: {str(earn)[:120]}")
        earn = {}
    done, gained, _ = run_ads(sess, earn, before)

    me2 = _req(sess, 'GET', '/api/mini/me')
    after = int((me2.get('user') or {}).get('points', before) or 0) if me2.get('ok') else before
    delta = after - before
    total_ads = done.get('1', 0) + done.get('2', 0)
    line = (f"{label} {user.get('nickname', '')}: 广告{total_ads}次(档1 {done.get('1', 0)}/档2 {done.get('2', 0)})"
            f" 积分 {before}→{after} (+{delta})")
    print(f"  ✅ {line}")
    return {'ok': True, 'used_yyb': False, 'label': label, 'line': line, 'delta': delta}


# ==================== 主流程 ====================
def main():
    print("=" * 55)
    print(f"闲单汇自动看广告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 55)

    if MTWM_TOKEN:
        mode = 'token'
    elif YYB_SERVER:
        mode = 'yyb'
    else:
        print("❌ 未配置登录方式：设置 MTWM_TOKEN，或按每行 地址@账号标识#备注 配置 YYB_SERVER")
        return

    cache = load_cache()
    results = []

    if mode == 'token':
        tokens = parse_tokens(MTWM_TOKEN)
        if not tokens:
            print("❌ MTWM_TOKEN 无法解析出有效 token")
            return
        print(f"登录通道: 直接 token | 账号数量: {len(tokens)} 个")
        print(f"观看等待: {AD_WAIT}~{AD_WAIT + AD_JITTER}s 随机 | 每档上限: {'跑满' if AD_MAX <= 0 else AD_MAX}")
        for no, (remark, token) in enumerate(tokens, 1):
            try:
                r = run_account_token(no, remark, token)
            except Exception as e:
                print(f"❌ 账号{no} 异常: {e}")
                r = {'ok': False, 'line': f"账号{no} 执行异常"}
            if r:
                results.append(r)
            if no < len(tokens) and ACC_SLEEP_CACHED > 0:
                time.sleep(ACC_SLEEP_CACHED)
    else:
        accounts = parse_yyb_accounts(YYB_SERVER)
        if not accounts:
            print("❌ YYB_SERVER 中没有有效账号；格式为 地址@账号标识#备注，多账号每行一条")
            return
        print(f"登录通道: YYB-Go-Enhanced | 账号数量: {len(accounts)} 个")

        print(f"观看等待: {AD_WAIT}~{AD_WAIT + AD_JITTER}s 随机 | 每档上限: {'跑满' if AD_MAX <= 0 else AD_MAX}")

        for no, (server, ref, alias) in enumerate(accounts, 1):
            YYB = YYBClient(server, YYB_API_KEY)
            try:
                r = run_account(YYB, no, ref, alias, cache)
            except Exception as e:
                print(f"❌ 账号{no} 异常: {e}")
                r = {'ok': False, 'used_yyb': True, 'line': f"账号{no} 执行异常"}  # 保守按拉过微信处理
            if r:
                results.append(r)
            if no < len(accounts):
                if r and r.get('used_yyb') and ACC_SLEEP > 0:
                    print(f"⏳ 本轮拉起过微信取 code，等待 {ACC_SLEEP}s 再处理下一个账号")
                    time.sleep(ACC_SLEEP)
                elif ACC_SLEEP_CACHED > 0:
                    time.sleep(ACC_SLEEP_CACHED)

    # ==================== 汇总（逐号固定编号，不重排） ====================
    print("\n" + "=" * 55)
    print("📊 汇总")
    total_delta = 0
    lines = []
    success_count = 0
    for r in results:
        line = r.get('line', '未知结果')
        lines.append(("✅ " if r.get('ok') else "❌ ") + line)
        total_delta += int(r.get('delta', 0) or 0)
        success_count += int(bool(r.get('ok')))
        print("  " + lines[-1])
    failed_count = len(results) - success_count
    print(f"  成功 {success_count}｜失败 {failed_count}｜合计积分 +{total_delta}")
    if results:
        content = (f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                   f"📊 成功 {success_count}｜失败 {failed_count}\n\n"
                   + "\n".join(lines)
                   + f"\n\n🎁 本次增加：{total_delta} 积分")
        send_ql_notification("闲单汇看广告", content)


if __name__ == '__main__':
    main()
