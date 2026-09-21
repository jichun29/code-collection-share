#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 兼容 GBK 终端：强制 stdout/stderr 使用 UTF-8（不影响排版与格式）
import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""
益禾堂小程序（企迈 qmai 平台 + 兑吧 duiba 活动）签到动态 code 版

功能：
  1. 本地 code 服务获取微信 code
  2. /account-center/oauth/mini-app-login 使用 code 换 qm-user-token
     （企迈全站 AES-GCM 加密契约，同平台实测脚本验证）
  3. member/redirect 获取兑吧活动落地页地址（取 302 Set-Cookie 会话）
  4. 从活动页解析 signOperatingId（活动 ID 会变，不能写死）
  5. getToken 取混淆 JS，纯 Python 还原 window[k] 得到签到 token
  6. doSign 签到 + signResult 轮询确认
  7. PushPlus / 企业微信推送
  8. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests pycryptodome
  socks5 代理需：
  pip install requests[socks]

⚠️ 签到 token 由兑吧 ctoken 接口下发混淆 JS，本脚本用内置纯 Python
   解码器还原 eval 产物，无需 Node/execjs；signOperatingId 每次运行从
   活动页动态解析，避免活动换 ID 后签到失效。
"""

import base64
import json
import os
import random
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, unquote, urlsplit

import requests

# pycryptodome 原生扩展在部分环境（如 musl aarch64）加载会失败，
# 因此捕获 OSError 并回退到内置纯 Python AES-GCM 实现。
try:
    from Crypto.Cipher import AES
except Exception:
    AES = None


# ====================== 纯 Python AES-GCM 后备实现 ======================
# 仅在 pycryptodome 不可用时使用，接口与 Crypto.Cipher.AES 的 GCM 用法对齐。
AES_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)
AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _aes_xtime(value: int) -> int:
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _aes_mul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _aes_xtime(a)
        b >>= 1
    return result


class PureAES:
    """最小 AES 分组加密实现（支持 128/192/256 位密钥）。"""

    def __init__(self, key: bytes):
        self.key = bytes(key)
        if len(self.key) not in (16, 24, 32):
            raise ValueError("AES key must be 16, 24 or 32 bytes")
        self.rounds = {16: 10, 24: 12, 32: 14}[len(self.key)]
        self.round_keys = self._expand_key()

    def _expand_key(self) -> List[bytes]:
        key = self.key
        nk = len(key) // 4
        words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        for i in range(nk, 4 * (self.rounds + 1)):
            temp = list(words[i - 1])
            if i % nk == 0:
                temp = temp[1:] + temp[:1]
                temp = [AES_SBOX[b] for b in temp]
                temp[0] ^= AES_RCON[i // nk - 1]
            elif nk > 6 and i % nk == 4:
                temp = [AES_SBOX[b] for b in temp]
            words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
        return [
            bytes(byte for word in words[4 * r:4 * r + 4] for byte in word)
            for r in range(self.rounds + 1)
        ]

    def encrypt_block(self, block: bytes) -> bytes:
        state = list(block)
        self._add_round_key(state, 0)
        for round_index in range(1, self.rounds):
            self._sub_bytes(state)
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, round_index)
        self._sub_bytes(state)
        self._shift_rows(state)
        self._add_round_key(state, self.rounds)
        return bytes(state)

    def _add_round_key(self, state, round_index: int) -> None:
        round_key = self.round_keys[round_index]
        for i in range(16):
            state[i] ^= round_key[i]

    @staticmethod
    def _sub_bytes(state) -> None:
        for i in range(16):
            state[i] = AES_SBOX[state[i]]

    @staticmethod
    def _shift_rows(state) -> None:
        # state 按列优先存储：index = row + 4 * column
        for row in range(1, 4):
            values = [state[row + 4 * column] for column in range(4)]
            values = values[row:] + values[:row]
            for column in range(4):
                state[row + 4 * column] = values[column]

    @staticmethod
    def _mix_columns(state) -> None:
        for column in range(4):
            i = 4 * column
            a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
            state[i] = _aes_mul(a0, 2) ^ _aes_mul(a1, 3) ^ a2 ^ a3
            state[i + 1] = a0 ^ _aes_mul(a1, 2) ^ _aes_mul(a2, 3) ^ a3
            state[i + 2] = a0 ^ a1 ^ _aes_mul(a2, 2) ^ _aes_mul(a3, 3)
            state[i + 3] = _aes_mul(a0, 3) ^ a1 ^ a2 ^ _aes_mul(a3, 2)


def _aes_xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def _ghash_mul(x: bytes, y: bytes) -> bytes:
    """GHASH 使用的 GF(2^128) 乘法（大端位序）。"""
    z = 0
    v = int.from_bytes(y, "big")
    x_int = int.from_bytes(x, "big")
    for i in range(128):
        if (x_int >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ (0xE1 << 120)
        else:
            v >>= 1
    return z.to_bytes(16, "big")


def _ghash(hash_key: bytes, data: bytes) -> bytes:
    y = b"\x00" * 16
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        if len(block) < 16:
            block = block + b"\x00" * (16 - len(block))
        y = _ghash_mul(_aes_xor(y, block), hash_key)
    return y


def _gcm_counter_block(j0: bytes, counter: int) -> bytes:
    # GCM 用 inc32(J0) 作为第一个密钥流块
    base = int.from_bytes(j0[12:], "big")
    return j0[:12] + ((base + counter) & 0xFFFFFFFF).to_bytes(4, "big")


def _gcm_crypt(cipher: PureAES, iv: bytes, data: bytes):
    if len(iv) == 12:
        j0 = iv + b"\x00\x00\x00\x01"
    else:
        padding = b"\x00" * ((16 - len(iv) % 16) % 16)
        j0 = _ghash(
            cipher.encrypt_block(b"\x00" * 16),
            iv + padding + b"\x00" * 8 + (len(iv) * 8).to_bytes(8, "big"),
        )

    output = bytearray()
    for index in range(0, len(data), 16):
        keystream = cipher.encrypt_block(_gcm_counter_block(j0, index // 16 + 1))
        output.extend(a ^ b for a, b in zip(data[index:index + 16], keystream))
    return bytes(output), j0


def _gcm_tag(cipher: PureAES, j0: bytes, ciphertext: bytes) -> bytes:
    hash_key = cipher.encrypt_block(b"\x00" * 16)
    payload = ciphertext + b"\x00" * ((16 - len(ciphertext) % 16) % 16)
    payload += (0).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    return _aes_xor(_ghash(hash_key, payload), cipher.encrypt_block(j0))


def pure_gcm_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """返回 ciphertext + 16 字节 tag。"""
    cipher = PureAES(key)
    ciphertext, j0 = _gcm_crypt(cipher, iv, plaintext)
    return ciphertext + _gcm_tag(cipher, j0, ciphertext)


def pure_gcm_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """校验 tag 并返回明文，校验失败抛 ValueError。"""
    if len(data) < 16:
        raise ValueError("ciphertext too short")
    ciphertext, tag = data[:-16], data[-16:]
    cipher = PureAES(key)
    plaintext, j0 = _gcm_crypt(cipher, iv, ciphertext)
    if _gcm_tag(cipher, j0, ciphertext) != tag:
        raise ValueError("MAC check failed")
    return plaintext


# ====================== 兑吧 ctoken 混淆 JS 纯 Python 解码 ======================
# /chw/ctoken/getToken 返回的 token 是一段混淆 JS，浏览器中 eval 后会写入一个
# 固定键（抓包坐实为 '3fd0cbet'）供 /sign/component/doSign 使用。
# 下面直接解释执行该 JS 里的数值表达式，不依赖 Node / execjs。
CTOKEN_ALIAS_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*=\s*String\s*\.\s*fromCharCode")
CTOKEN_KEY_ARRAY_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*=\s*\[([^\]]*)\]")
CTOKEN_HELPER_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\s*=\s*function\s*\([^)]*\)\s*\{\s*return\s*"
    r"arguments\s*\[\s*0\s*\]\s*\^\s*([A-Za-z_$][\w$]*)\s*\[\s*(\d+)\s*\]"
)


def _ctoken_strip_comments(code: str) -> str:
    out, index, length = [], 0, len(code)
    while index < length:
        if code.startswith("/*", index):
            end = code.find("*/", index + 2)
            index = length if end == -1 else end + 2
        elif code.startswith("//", index):
            end = code.find("\n", index)
            index = length if end == -1 else end
        else:
            out.append(code[index])
            index += 1
    return "".join(out)


def _ctoken_normalize_name(name: str) -> str:
    # 压缩器会输出 var__oOmDH=String.fromCharCode，正则捕获到的是 var__oOmDH
    return name[3:] if name.startswith("var") else name


class _CtokenExpressionParser:
    """解释混淆 JS 中出现的整数表达式。"""

    def __init__(self, text: str, helpers: Dict[str, Any]):
        self.text = text
        self.pos = 0
        self.helpers = helpers

    def parse(self) -> int:
        value = self.parse_bit_or()
        self.skip()
        if self.pos != len(self.text):
            raise ValueError(f"trailing input: {self.text[self.pos:self.pos + 40]!r}")
        return value

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def parse_bit_or(self) -> int:
        value = self.parse_xor()
        while True:
            self.skip()
            if self.text.startswith("|", self.pos) and not self.text.startswith("||", self.pos):
                self.pos += 1
                value = int(value) | int(self.parse_xor())
            else:
                return value

    def parse_xor(self) -> int:
        value = self.parse_and()
        while True:
            self.skip()
            if self.text.startswith("^", self.pos):
                self.pos += 1
                value = int(value) ^ int(self.parse_and())
            else:
                return value

    def parse_and(self) -> int:
        value = self.parse_shift()
        while True:
            self.skip()
            if self.text.startswith("&", self.pos) and not self.text.startswith("&&", self.pos):
                self.pos += 1
                value = int(value) & int(self.parse_shift())
            else:
                return value

    def parse_shift(self) -> int:
        value = self.parse_additive()
        while True:
            self.skip()
            if self.text.startswith(">>", self.pos):
                self.pos += 2
                value = int(value) >> int(self.parse_additive())
            elif self.text.startswith("<<", self.pos):
                self.pos += 2
                value = int(value) << int(self.parse_additive())
            else:
                return value

    def parse_additive(self) -> int:
        value = self.parse_multiplicative()
        while True:
            self.skip()
            if self.text.startswith("+", self.pos):
                self.pos += 1
                value = value + self.parse_multiplicative()
            elif self.text.startswith("-", self.pos):
                self.pos += 1
                value = value - self.parse_multiplicative()
            else:
                return value

    def parse_multiplicative(self) -> int:
        value = self.parse_unary()
        while True:
            self.skip()
            if self.text.startswith("*", self.pos):
                self.pos += 1
                value = value * self.parse_unary()
            elif self.text.startswith("/", self.pos):
                self.pos += 1
                divisor = self.parse_unary()
                value = int(value / divisor)
            else:
                return value

    def parse_unary(self) -> int:
        self.skip()
        if self.text.startswith("~", self.pos):
            self.pos += 1
            return ~int(self.parse_unary())
        if self.text.startswith("-", self.pos):
            self.pos += 1
            return -int(self.parse_unary())
        if self.text.startswith("+", self.pos):
            self.pos += 1
            return int(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> int:
        self.skip()
        if self.text.startswith("(", self.pos):
            self.pos += 1
            value = self.parse_bit_or()
            self.skip()
            if not self.text.startswith(")", self.pos):
                raise ValueError("missing )")
            self.pos += 1
            return value

        name = self.read_name()
        if name:
            self.skip()
            if name == "Math":
                if not self.text.startswith(".", self.pos):
                    raise ValueError("expected . after Math")
                self.pos += 1
                attribute = self.read_name()
                if attribute != "abs":
                    raise ValueError(f"unsupported Math.{attribute}")
                return abs(int(self.parse_parenthesized()))
            if name in self.helpers and self.text.startswith("(", self.pos):
                return self.helpers[name](self.parse_parenthesized())
            raise ValueError(f"unknown identifier {name}")

        return self.read_number()

    def parse_parenthesized(self) -> int:
        self.skip()
        if not self.text.startswith("(", self.pos):
            raise ValueError("expected (")
        self.pos += 1
        value = self.parse_bit_or()
        self.skip()
        if not self.text.startswith(")", self.pos):
            raise ValueError("missing )")
        self.pos += 1
        return value

    def read_name(self) -> str:
        self.skip()
        match = re.match(r"[A-Za-z_$][\w$]*", self.text[self.pos:])
        if not match:
            return ""
        self.pos += match.end()
        return match.group(0)

    def read_number(self) -> int:
        self.skip()
        match = re.match(r"0[xX][0-9a-fA-F]+|0[0-7]+|\d+", self.text[self.pos:])
        if not match:
            raise ValueError(f"unexpected char: {self.text[self.pos:self.pos + 30]!r}")
        token = match.group(0)
        self.pos += match.end()
        if token.lower().startswith("0x"):
            return int(token, 16)
        if len(token) > 1 and token[0] == "0":
            return int(token, 8)
        return int(token)


def _ctoken_split_top_level(text: str, separator: str) -> List[str]:
    parts, buffer, depth = [], [], 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _ctoken_match_paren(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced parentheses")


def decode_ctoken_script(raw_js: str) -> str:
    """还原混淆 JS 交给 eval 的字符串（内含 window['key']=value 赋值）。"""
    code = _ctoken_strip_comments(re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw_js))

    alias = ""
    for match in CTOKEN_ALIAS_RE.finditer(code):
        alias = _ctoken_normalize_name(match.group(1))
    if not alias:
        raise ValueError("fromCharCode alias not found")

    keys: Dict[str, List[int]] = {}
    for match in CTOKEN_KEY_ARRAY_RE.finditer(code):
        values = [value.strip() for value in match.group(2).split(",") if value.strip()]
        try:
            keys[_ctoken_normalize_name(match.group(1))] = [int(value, 0) for value in values]
        except ValueError:
            continue

    helpers: Dict[str, Any] = {}
    for name, array_name, index in CTOKEN_HELPER_RE.findall(code):
        name = _ctoken_normalize_name(name)
        array_name = _ctoken_normalize_name(array_name)
        if array_name in keys and int(index) < len(keys[array_name]):
            key = keys[array_name][int(index)]
            helpers[name] = (lambda key_value: (lambda value: int(value) ^ key_value))(key)

    eval_index = code.find("eval(")
    if eval_index == -1:
        raise ValueError("eval( not found")
    open_paren = eval_index + len("eval")
    payload = code[open_paren + 1:_ctoken_match_paren(code, open_paren)]

    chunks: List[str] = []
    for piece in _ctoken_split_top_level(payload, "+"):
        piece = piece.strip()
        call = re.search(r"([A-Za-z_$][\w$]*)\s*\(", piece)
        if not call or call.group(1) != alias:
            continue
        inner = piece[call.end():]
        arguments = inner[:_ctoken_match_paren("(" + inner, 0) - 1]
        for argument in _ctoken_split_top_level(arguments, ","):
            argument = argument.strip()
            if argument:
                chunks.append(chr(int(_CtokenExpressionParser(argument, helpers).parse()) & 0xFFFF))

    return "".join(chunks)


def extract_ctoken_value(eval_script: str) -> str:
    """从 eval 产物中取出前端固定使用的那个 window 键值。

    键名形如 '3fd0cbet'（字母数字混合，不一定是十六进制），因此这里用
    宽松字符集匹配；优先取页面声明的键，否则退回初始项。
    """
    assignments = re.findall(
        r"window\[['\"]([0-9A-Za-z]{4,16})['\"]\]\s*=\s*['\"]([^'\"]*)['\"]",
        eval_script,
    )
    if not assignments:
        raise ValueError("no window assignment found")
    for key, value in assignments:
        if key.lower() == SIGN_TOKEN_KEY.lower():
            return value
    raise ValueError(f"window key {SIGN_TOKEN_KEY} not found in ctoken payload")



APP_NAME = "益禾堂小程序"
APPID = "wx4080846d0cec2fd5"

SERVERS = [
    "10.30.9.183:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

QMAI_BASE_URL = "https://webapi.qmai.cn/web"
QMAI_LOGIN_URL = f"{QMAI_BASE_URL}/account-center/oauth/mini-app-login"
QMAI_REDIRECT_URL = f"{QMAI_BASE_URL}/catering/crm/member/redirect"

ACTIVITY_PAGE_URL = "https://86019.activity-12.m.duiba.com.cn/chw/visual-editor/skins?id=203576"
ACTIVITY_TOKEN_URL = "https://86019-activity.dexfu.cn/chw/ctoken/getToken"
ACTIVITY_SIGN_URL = "https://86019-activity.dexfu.cn/sign/component/doSign"
ACTIVITY_SIGN_PAGE_URL = "https://86019-activity.dexfu.cn/sign/component/page"
ACTIVITY_SIGN_INDEX_URL = "https://86019-activity.dexfu.cn/sign/component/index"
ACTIVITY_SIGN_RESULT_URL = "https://86019-activity.dexfu.cn/sign/component/signResult"
ACTIVITY_ORIGIN = "https://86019-activity.dexfu.cn"

# 活动 ID 会随活动变更，仅当页面解析失败时用作兜底
SIGN_OPERATING_ID_FALLBACK = "340158783207556"
# 兑吧页面脚本中使用的 window 键名（抓包坐实）
SIGN_TOKEN_KEY = "3fd0cbet"
STORE_ID = "203009"

# —— 企迈全站 AES-GCM 加密固定参数（源自解包 requestEncryptSdk，同平台脚本验证）——
KEY_RAW = "mN6KpXq8Sv2WxYz9LdFcRgHjMnBvCtDxZaS3QwE5rT0yU7I4O1A"
KEY_VERSION = "1.0.0"
META_HEADER = "QM-Encrypt-Meta"
CACHE_DIR = os.environ.get("CODE_CACHE_DIR", os.path.join(os.path.expanduser("~"), "Documents", "写代码"))

os.makedirs(CACHE_DIR, exist_ok=True)

COOKIE_FILE = os.path.join(CACHE_DIR, "yhtcookie.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf254173b) XWEB/19027"
)
# 签到页请求 UA（源脚本 doSign 使用）
SIGN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781 NetType/WIFI MiniProgramEnv/Windows "
    "WindowsWechat/WMPF XWEB/50249"
)


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


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🧋 益禾堂签到动态 code 版                     ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
    print("└" + "─" * 50 + "┘")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


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
        auth = f"{quote(username)}:{quote(password)}@"

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



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        print("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        print("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        print("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
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


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str | None = None) -> Dict[str, str]:
    """企迈平台固定头（参考源脚本 redirect 请求头与同平台契约）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "v=1.0",
        "Content-Type": "application/json",
        "xweb_xhr": "1",
        "qm-from-type": "catering",
        "qm-from": "wechat",
        "scene": "1101",
        "store-id": STORE_ID,
        "multi-store-id": "",
        "accept-language": "zh-CN",
        "sec-fetch-site": "cross-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Referer": f"https://servicewechat.com/{APPID}/517/page-frame.html",
    }
    if token:
        headers["qm-user-token"] = token
    return headers


# ========== 业务辅助函数（照源脚本） ==========
def b64relax(value: str) -> bytes:
    """宽松 base64 解码（自动补齐 padding）。"""
    return base64.b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def derive_key(raw: str) -> bytes:
    """解包 M()：宽松 base64 解码，非 32 字节则取前 32 补零。"""
    try:
        b = b64relax(raw)
    except Exception:
        b = raw.encode("utf-8")
    if len(b) == 32:
        return b
    out = bytearray(32)
    out[: min(len(b), 32)] = b[:32]
    return bytes(out)


def gcm_encrypt(plaintext: str, iv: bytes) -> str:
    """AES-256-GCM：返回 base64(ciphertext + 16字节tag)。"""
    raw = plaintext.encode("utf-8")
    if AES is not None:
        cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
        enc, tag = cipher.encrypt_and_digest(raw)
        return base64.b64encode(enc + tag).decode("utf-8")
    return base64.b64encode(pure_gcm_encrypt(KEY, iv, raw)).decode("utf-8")


def gcm_decrypt(payload_b64: str, iv: bytes) -> str:
    buf = base64.b64decode(payload_b64)
    if AES is not None:
        cipher = AES.new(KEY, AES.MODE_GCM, nonce=iv)
        return cipher.decrypt_and_verify(buf[:-16], buf[-16:]).decode("utf-8")
    return pure_gcm_decrypt(KEY, iv, buf).decode("utf-8")


KEY = derive_key(KEY_RAW)


def qmai_request(
    method: str,
    url: str,
    body: Dict[str, Any],
    token: str = "",
    extra_headers: Dict[str, str] | None = None,
    proxies: Dict[str, str] | None = None,
    server: str = "",
) -> Dict[str, Any]:
    """企迈加密请求：AES-GCM 请求体 + QM-Encrypt-Meta 头，响应加密时自动解密。"""
    # AES 不可用时自动使用内置纯 Python AES-GCM（已对拍 pycryptodome）

    payload_obj = dict(body or {})
    if not payload_obj.get("appid"):
        payload_obj["appid"] = APPID
    iv = os.urandom(12)
    ts = int(time.time() * 1000)
    meta = base64.b64encode(
        json.dumps({
            "version": KEY_VERSION,
            "timestamp": ts,
            "iv": base64.b64encode(iv).decode("utf-8"),
        }).encode("utf-8")
    ).decode("utf-8")

    headers = common_headers(token)
    headers[META_HEADER] = meta
    if extra_headers:
        headers.update(extra_headers)

    response = request_with_proxy(
        method,
        url,
        headers=headers,
        json={"payload": gcm_encrypt(json.dumps(payload_obj), iv)},
        proxies=proxies,
        server=server,
    )
    try:
        data = response.json()
    except Exception:
        return {"status": False, "code": -1, "message": f"JSON解析失败: {response.text[:300]}"}

    if isinstance(data, dict) and isinstance(data.get("payload"), str):
        rmeta = response.headers.get(META_HEADER) or response.headers.get(META_HEADER.lower())
        if not rmeta:
            return {"status": False, "code": -1, "message": "响应加密但缺少 QM-Encrypt-Meta"}
        try:
            meta_obj = json.loads(b64relax(rmeta).decode("utf-8"))
            return json.loads(gcm_decrypt(data["payload"], b64relax(meta_obj["iv"])))
        except Exception as exc:
            return {"status": False, "code": -1, "message": f"响应解密失败: {exc}"}

    return data


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ])

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("token"),
                user.get("accessToken"),
                user.get("access_token"),
                user.get("jwt"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """code 换 qm-user-token（照抓包 HAR 坐实的明文接口）

    HAR 实测：POST /web/account-center/oauth/mini-app-login
      body {"code":<wx.login code>,"eVersion":"1.0","appid":<APPID>}
      -> {"code":0,"data":{"token":"...","user":{...}}}
    这一步只需明文 JSON（无需 AES-GCM 加密），本地 code 服务完全可用。
    """
    try:
        print("🔐 [登录] 使用 code 换 qm-user-token（mini-app-login）")
        headers = dict(common_headers())
        headers.update({
            "Qm-From-Type": "catering",
            "Qm-From": "wechat",
            "store-id": STORE_ID,
            "Accept": "v=1.0",
        })
        response = request_with_proxy(
            "POST",
            QMAI_LOGIN_URL,
            headers=headers,
            json={"code": code, "eVersion": "1.0", "appid": APPID},
            proxies=proxies,
            server=server,
        )
        try:
            data = response.json()
        except Exception:
            return None, {"raw": response.text[:300]}
        if int(data.get("code") or 0) != 0:
            print(f"❌ [登录] 接口返回失败: {json_preview(data)}")
            return None, data

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str | None, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "status": False,
            "code": -1,
            "message": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(
    server: str,
    url: str,
    token: str | None,
    proxies: Dict[str, str] | None,
    payload: Dict[str, Any],
    extra_headers: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """业务 POST：企迈接口走 AES-GCM 加密请求。"""
    return qmai_request(
        "POST",
        url,
        payload,
        token=token or "",
        extra_headers=extra_headers,
        proxies=proxies,
        server=server,
    )


# ====================== Token缓存管理 ======================
def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        print(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print("✅ [缓存] Token保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def get_cached_token(server: str) -> str | None:
    cache = load_token_cache()
    data = cache.get(server)
    if data and data.get("token") and data.get("expireTime"):
        try:
            expire = datetime.fromisoformat(data["expireTime"]).timestamp() * 1000
            if time.time() * 1000 < expire - 3600 * 1000:
                print(f"✅ [缓存] 使用 {server} token")
                return data["token"]
        except Exception as exc:
            print(f"⚠️ [缓存] 过期时间解析异常: {exc}")
    return None


def set_cached_token(server: str, token: str, expire_time: str) -> None:
    cache = load_token_cache()
    cache[server] = {"token": token, "expireTime": expire_time, "updateTime": datetime.now().isoformat()}
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（member/redirect 接口验证），失效自动 code 刷新"""
    cache_token = get_cached_token(server)
    if cache_token:
        print("🔍 [缓存] 验证 token")
        try:
            redirect_resp = api_post(server, QMAI_REDIRECT_URL, cache_token, proxies, {"redirectUrl": ACTIVITY_PAGE_URL})
            if redirect_resp.get("status") is True and redirect_resp.get("data"):
                print("✅ [缓存] token 有效")
                return cache_token, None
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, raw_login = login_by_code(server, code, proxies)
    if not token:
        return None, raw_login

    expire_time = None
    if raw_login and isinstance(raw_login, dict):
        inner = raw_login.get("data")
        if isinstance(inner, dict):
            expire_time = inner.get("expireTime") or inner.get("expire_time")
            expires_in = inner.get("expiresIn")
            if not expire_time and isinstance(expires_in, (int, float)) and expires_in > 0:
                expire_time = datetime.fromtimestamp(time.time() + expires_in).isoformat()
    if not expire_time:
        expire_time = datetime.fromtimestamp(time.time() + 24 * 3600).isoformat()
    elif not isinstance(expire_time, str):
        expire_time = datetime.fromtimestamp(expire_time / 1000).isoformat()
    set_cached_token(server, token, expire_time)
    return token, raw_login


def fetch_activity_cookie(server: str, activity_url: str, proxies: Dict[str, str] | None) -> str:
    """访问活动落地页，取 302 Set-Cookie 中 wdata4/w_ts/_ac/wdata3/dcustom 组成会话。"""
    try:
        response = request_with_proxy(
            "GET",
            activity_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            proxies=proxies,
            server=server,
            allow_redirects=False,
        )
        set_cookies: List[str] = []
        raw = getattr(response, "raw", None)
        header_obj = getattr(raw, "headers", None)
        if header_obj is not None:
            try:
                set_cookies = header_obj.getlist("Set-Cookie")
            except Exception:
                set_cookies = []
        if not set_cookies:
            merged = response.headers.get("Set-Cookie", "")
            if merged:
                set_cookies = [merged]

        joined = "".join(set_cookies)
        parts = re.findall(r"(?:wdata4|w_ts|_ac|wdata3|dcustom)=[^;]*;", joined)
        if not parts:
            print(f"⚠️ [活动] 未提取到活动 Cookie: {json_preview(set_cookies, 300)}")
            return ""
        if len(parts) < 5:
            print(f"⚠️ [活动] 活动 Cookie 不完整（{len(parts)}/5），继续尝试")
        print("✅ [活动] 获取活动 token（Cookie）成功")
        return "".join(parts)
    except Exception as exc:
        print(f"❌ [活动] 获取活动 Cookie 异常: {exc}")
        return ""


def _activity_id_candidates(activity_url: str) -> List[str]:
    """列出可能内嵌 signOperatingId 的页面（活动换 ID 时无需改脚本）。"""
    candidates: List[str] = []

    # autologin 链接里的 redirect 参数指向活动页，例如
    #   /chw/visual-editor/skins?id=203576
    match = re.search(r"[?&]redirect=([^&]+)", activity_url or "")
    if match:
        target = unquote(match.group(1))
        candidates.append(target)
        split = urlsplit(target)
        if split.path:
            suffix = f"{split.path}?{split.query}" if split.query else split.path
            candidates.append(f"{ACTIVITY_ORIGIN}{suffix}")

    candidates.append(ACTIVITY_PAGE_URL)
    candidates.append(f"{ACTIVITY_SIGN_PAGE_URL}?preview=false")

    ordered: List[str] = []
    for url in candidates:
        if url and url not in ordered:
            ordered.append(url)
    return ordered


def resolve_sign_operating_id(
    server: str,
    session_cookie: str,
    activity_url: str,
    proxies: Dict[str, str] | None,
) -> str:
    """从活动页解析当次活动的 signOperatingId。"""
    for page_url in _activity_id_candidates(activity_url):
        try:
            response = request_with_proxy(
                "GET",
                page_url,
                headers={
                    "User-Agent": SIGN_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Cookie": session_cookie,
                },
                proxies=proxies,
                server=server,
            )
            matches = re.findall(r"signOperatingId[=\"':\s]+?(\d{6,})", response.text or "")
            if matches:
                print(f"🎯 [签到] 活动 ID: {matches[0]}")
                return matches[0]
        except Exception as exc:
            print(f"⚠️ [签到] 解析活动 ID 异常: {str(exc)[:120]}")

    print(f"⚠️ [签到] 未能解析活动 ID，回退默认值 {SIGN_OPERATING_ID_FALLBACK}")
    return SIGN_OPERATING_ID_FALLBACK


def get_activity_key(
    server: str,
    session_cookie: str,
    sign_operating_id: str,
    proxies: Dict[str, str] | None,
) -> str:
    """getToken：取混淆 JS，用内置纯 Python 解码器还原出签到 token。

    逆向结论（ProxyPin 抓包 + Node 对照验证）：
      · 签到页内联脚本定义 window.getDuibaToken，它会 POST /chw/ctoken/getToken
      · 响应里的 token 字段是混淆 JS，eval 后写入 window['3fd0cbet']
      · 前端把这个值作为 token 字段随 doSign 一起提交
      · 解码全程可在 Python 内完成，无需 Node / execjs
    """
    ts = int(time.time() * 1000)
    try:
        response = request_with_proxy(
            "POST",
            ACTIVITY_TOKEN_URL,
            headers={
                "User-Agent": SIGN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": ACTIVITY_ORIGIN,
                "Referer": f"{ACTIVITY_SIGN_PAGE_URL}?signOperatingId={sign_operating_id}",
                "Cookie": session_cookie,
            },
            data={"timestamp": ts},
            proxies=proxies,
            server=server,
        )
        result = response.json()
        try:
            result = response.json()
        except Exception:
            print(f"❌ [签到] getToken 返回非 JSON（会话可能已过期）: {response.text[:150]}")
            return ""
        if not result.get("success"):
            print(f"❌ [签到] getToken 失败: {json_preview(result, 300)}")
            return ""
        raw_js = str(result.get("token") or "")
        if not raw_js:
            print(f"❌ [签到] getToken 未返回 token 字段: {json_preview(result, 200)}")
            return ""
    except Exception as exc:
        print(f"❌ [签到] getToken 异常: {exc}")
        return ""

    try:
        eval_script = decode_ctoken_script(raw_js)
        key = extract_ctoken_value(eval_script)
    except Exception as exc:
        print(f"❌ [签到] 无法从 getToken 响应解析 token: {exc}")
        return ""

    print("✅ [签到] 获取签到 token 成功")
    return key


def poll_sign_result(
    server: str,
    session_cookie: str,
    order_num: str,
    proxies: Dict[str, str] | None,
    retry: int = 5,
) -> int | None:
    """轮询 signResult 拿实际发放积分（doSign 只返回状态码）。"""
    for _ in range(retry):
        try:
            response = request_with_proxy(
                "GET",
                f"{ACTIVITY_SIGN_RESULT_URL}?orderNum={order_num}&_={int(time.time() * 1000)}",
                headers={
                    "User-Agent": SIGN_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Origin": ACTIVITY_ORIGIN,
                    "Referer": ACTIVITY_SIGN_PAGE_URL,
                    "Cookie": session_cookie,
                },
                proxies=proxies,
                server=server,
            )
            payload = response.json()
            data = payload.get("data") or {}
            sign_result = data.get("signResult")

            # 1 = 处理中，继续轮询；2 = 已出结果，credits 为发放积分
            if sign_result == 1:
                sleep(1.5)
                continue
            if sign_result == 2:
                return int(to_float(data.get("credits")))
            return None
        except Exception as exc:
            print(f"⚠️ [签到] 查询签到结果异常: {str(exc)[:120]}")
            sleep(1.5)

    return None


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    token, raw_login = login_with_cache(server, proxies)
    if not token:
        result["error"] = f"登录失败: {json_preview(raw_login)}"
        return result

    result["token"] = mask(token)

    try:
        # 1. member/redirect 获取活动落地页地址
        redirect_resp = api_post(server, QMAI_REDIRECT_URL, token, proxies, {"redirectUrl": ACTIVITY_PAGE_URL})
        if not (redirect_resp.get("status") is True and redirect_resp.get("data")):
            result["error"] = f"获取活动地址失败: {json_preview(redirect_resp, 300)}"
            print(f"❌ [活动] {result['error']}")
            return result
        activity_url = str(redirect_resp["data"])
        print(f"🎯 [活动] 活动地址: {activity_url}")

        sleep(random.uniform(1.0, 2.0))

        # 2. 访问活动页取会话 Cookie
        session_cookie = fetch_activity_cookie(server, activity_url, proxies)
        if not session_cookie:
            result["error"] = "获取活动会话 Cookie 失败"
            print(f"❌ [活动] {result['error']}")
            return result

        sleep(random.uniform(1.0, 2.0))

        # 3. 从活动页解析当次活动的 signOperatingId
        sign_operating_id = resolve_sign_operating_id(server, session_cookie, activity_url, proxies)

        sleep(random.uniform(1.0, 2.0))

        # 4. getToken 取混淆 JS，用内置解码器还原签到 token
        key = get_activity_key(server, session_cookie, sign_operating_id, proxies)
        if not key:
            result["error"] = "getToken 失败：无法还原兑吧下发的签到 token"
            print(f"❌ [签到] {result['error']}")
            return result

        sleep(random.uniform(1.0, 2.0))

        # 5. doSign 签到
        sign_resp = request_with_proxy(
            "POST",
            f"{ACTIVITY_SIGN_URL}?_={int(time.time() * 1000)}",
            headers={
                "User-Agent": SIGN_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": ACTIVITY_ORIGIN,
                "Referer": f"{ACTIVITY_SIGN_PAGE_URL}?signOperatingId={sign_operating_id}",
                "accept-language": "zh-CN,zh;q=0.9",
                "Cookie": session_cookie,
            },
            data={
                "signOperatingId": sign_operating_id,
                "token": key,
            },
            proxies=proxies,
            server=server,
        )
        try:
            sign_json = sign_resp.json()
        except Exception:
            sign_json = {"success": False, "data": sign_resp.text[:300]}

        if sign_json.get("success") is True:
            sign_data = sign_json.get("data") or {}
            order_num = sign_data.get("orderNum") if isinstance(sign_data, dict) else None

            # doSign 返回的 signResult 是状态码（100=待轮询），实际积分要从 signResult 接口取
            credits = None
            if order_num:
                credits = poll_sign_result(server, session_cookie, str(order_num), proxies)

            if credits is not None:
                result["signMsg"] = f"签到成功，获得{credits}积分"
            elif isinstance(sign_data, dict) and sign_data.get("errorMsg"):
                result["signMsg"] = f"签到成功: {sign_data['errorMsg']}"
            else:
                result["signMsg"] = "签到成功"
            print(f"✅ [签到] {result['signMsg']}")
        else:
            preview = json_preview(sign_json, 300)
            if re.search(r"已签|已经签|签到过|重复|已完成", preview):
                result["signMsg"] = "今日已签到"
                print(f"✅ [签到] {result['signMsg']}")
            else:
                result["signMsg"] = f"签到失败: {preview}"
                print(f"❌ [签到] {result['signMsg']}")

        result["success"] = "失败" not in str(result["signMsg"])
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🧋 益禾堂任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


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
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 益禾堂任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🧋 益禾堂任务完成", build_notify(results))


if __name__ == "__main__":
    main()
