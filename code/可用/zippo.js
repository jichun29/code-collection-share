// YYB-Go-Enhanced 适配说明：配置多行 YYB_SERVER=地址@账号标识；通知使用青龙 sendNotify。
// ===== YYB-Go-Enhanced + QingLong standalone adapter =====
function _yybRoutes() {
    const routes = String(process.env.YYB_SERVER || '').split(/\r?\n/)
        .map(v => v.trim()).filter(Boolean).map((line, index) => {
            const at = line.lastIndexOf('@');
            if (at <= 0 || at >= line.length - 1) {
                throw new Error(`YYB_SERVER 第 ${index + 1} 行格式错误，应为 地址@账号标识`);
            }
            let server = line.slice(0, at).trim().replace(/\/+$/, '');
            if (!/^https?:\/\//i.test(server)) server = `http://${server}`;
            return { server, ref: line.slice(at + 1).trim() };
        });
    if (!routes.length) throw new Error('未配置 YYB_SERVER（每行：地址@账号标识）');
    return routes;
}

function _yybCleanRef(value) {
    return String(value || '').split('#')[0].replace(/^(wx|yyb|wmpf|syzs):/i, '').trim();
}

function _yybRouteFor(identifier) {
    const routes = _yybRoutes();
    const wanted = _yybCleanRef(identifier);
    const exact = routes.find(x => _yybCleanRef(x.ref) === wanted);
    if (exact) return exact;
    if (/^\d+$/.test(wanted) && routes[Number(wanted) - 1]) return routes[Number(wanted) - 1];
    if (routes.length === 1) return routes[0];
    throw new Error(`YYB_SERVER 中找不到账号标识：${wanted || '(空)'}`);
}

async function getSingleCode(appId, identifier) {
    const route = _yybRouteFor(identifier);
    const response = await axios.post(`${route.server}/wxapp/getCode`,
        { ref: route.ref, app_id: appId },
        { timeout: 30000, headers: { 'Content-Type': 'application/json' } });
    const body = response.data;
    if (!body || Number(body.code) !== 0) {
        throw new Error(`/wxapp/getCode 返回失败：${body?.msg || body?.message || JSON.stringify(body)}`);
    }
    const code = body.data?.result?.code;
    if (!code) throw new Error('/wxapp/getCode 未返回 data.result.code');
    return code;
}

async function _resolveYybAccounts(envName = '') {
    const structured = new Set(['qmai', 'quncrm']);
    const configured = structured.has(envName) ? String(process.env[envName] || '').trim() : '';
    if (configured) return configured.split(/[\r\n&]+/).map(v => v.trim()).filter(Boolean);
    return _yybRoutes().map(x => x.ref);
}
global.getSingleCode = getSingleCode;
global.resolveAccounts = _resolveYybAccounts;

async function _sendQingLongNotify(title, content) {
    const candidates = ['./sendNotify', '../sendNotify', '/ql/data/scripts/sendNotify', '/ql/scripts/sendNotify'];
    let lastError = null;
    for (const candidate of candidates) {
        try {
            const mod = require(candidate);
            const send = mod?.sendNotify || mod?.send;
            if (typeof send === 'function') {
                await send(title, content);
                return true;
            }
        } catch (error) { lastError = error; }
    }
    console.log(`青龙通知失败（不影响任务结果）：${lastError?.message || '未找到通知模块'}`);
    return false;
}
const qlNotify = { sendNotify: _sendQingLongNotify, send: _sendQingLongNotify };
// ===== adapter end =====

// name: zippo会员
// cron: 44 8 * * *
/*
------------------------------------------
@Description: zippo会员 - 微信小程序静默登录 + 每日签到/会员任务
------------------------------------------
变量名：zippo
变量值：yyb_go 存活账号的 openid/账号标识，多账号用 & 或换行分隔（可加 #备注）

变量：
  YYB_SERVER     YYB-Go-Enhanced 路由，每行：地址@账号标识
  账号直接来自 YYB_SERVER；无需配置 WX_ID
------------------------------------------
契约（appid wxaa75ffd8c2d75da7，host wx-center.zippo.com.cn）：
  这家没有业务成功码：成功就是 HTTP 2xx（登录/签到都回 **201**）且响应体里没有 code；
  失败才带 code，且**放在 4xx 的 JSON 体里** —— 重复签到 = HTTP 400
    {"code":"already_signed","message":"今日已签到"}，所以不能在非 200 时直接抛。
  登录  POST /api/users/auth  {code, scene:"1001", platform:"wxmp"}
          -> {token(JWT), sid, openId, unionId}；之后 Authorization: Bearer <token>（带空格）
          固定头 x-app-id:zippo / x-platform:wxmp / x-platform-id:<appid> / x-platform-env:release
  资料  GET  /api/users/profile   -> memberLevel/phone(已脱敏)/nickname
  日历  GET  /api/daily-signin/month?month=YYYY-MM  -> days[].isSignIn（month 必须是 YYYY-MM，
          发 YYYY-MM-DD 会回 400 "month must be a Date instance"）(只读，脚本未用)
  签到  POST /api/daily-signin  {}  -> rewards[].count（每日 1 分，连签 7/30 天另有奖励）
  任务  GET  /api/missions -> list[].missions[]；completes/receives 分别是完成/领取次数
  浏览  POST /api/missions/records {code:"pageview",missionId}
  收藏  POST /api/favorites {targetType:"sku",targetId:<任务 link 中 skuId>,favorited:true}
  领奖  POST /api/missions/{id}/rewards {id} -> points/rewardValue
------------------------------------------
*/

class WeChatServer {
    constructor(config) { this.config = config || {}; }
    async getCode(wxid) {
        try {
            const ref = String(wxid).split('#')[0].trim();
            const code = await getSingleCode(this.config.appid, ref);
            return { data: { status: true, code, data: { code } } };
        } catch (e) {
            return { data: { status: false, message: e.message || String(e) } };
        }
    }
}

class Env {
    constructor(name) { this.name = name; this.userList = []; this.userIdx = 1; this.userCount = 0; this.logs = []; const originalLog = console.log; console.log = (...args) => { this.logs.push(args.join(" ")); originalLog.apply(console, args); }; }
    log(...args) { console.log(...args); }
    async wait(minMs, maxMs) { const ms = maxMs ? Math.floor(minMs + Math.random() * (maxMs - minMs)) : minMs; await new Promise(r => setTimeout(r, ms)); }
    async checkEnv(ckName) {
        const list = await global.resolveAccounts(ckName);
        this.userList = list;
        this.userCount = list.length;
        if (!this.userList.length) console.log('未配置可用的 YYB_SERVER 或脚本专用账号变量');
    }
    async done() { try { const notify = qlNotify; await notify.sendNotify(this.name, this.logs.join('\n')); } catch (e) { console.log('通知发送失败', e); } }
}

const $ = new Env("zippo会员");
const axios = Object.assign(async function axios(config = {}) {
    const method = String(config.method || 'GET').toUpperCase();
    let url = String(config.url || '');
    if (config.params && typeof config.params === 'object') {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(config.params)) {
            if (value !== undefined && value !== null) query.append(key, String(value));
        }
        const text = query.toString();
        if (text) url += (url.includes('?') ? '&' : '?') + text;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Number(config.timeout || 30000));
    const headers = { ...(config.headers || {}) };
    let body;
    if (!['GET', 'HEAD'].includes(method) && config.data !== undefined) {
        const contentType = Object.entries(headers).find(([key]) => key.toLowerCase() === 'content-type')?.[1] || '';
        body = typeof config.data === 'string' || Buffer.isBuffer(config.data)
            ? config.data
            : contentType.includes('application/x-www-form-urlencoded')
                ? new URLSearchParams(config.data).toString()
                : JSON.stringify(config.data);
        if (!contentType && typeof config.data === 'object') headers['Content-Type'] = 'application/json';
    }
    try {
        const response = await fetch(url, { method, headers, body, signal: controller.signal, redirect: 'follow' });
        const raw = await response.text();
        let data = raw;
        try { data = raw ? JSON.parse(raw) : ''; } catch {}
        const responseHeaders = Object.fromEntries(response.headers.entries());
        const setCookies = typeof response.headers.getSetCookie === 'function'
            ? response.headers.getSetCookie()
            : (response.headers.get('set-cookie') ? [response.headers.get('set-cookie')] : []);
        if (setCookies.length) responseHeaders['set-cookie'] = setCookies;
        const result = { status: response.status, statusText: response.statusText,
            headers: responseHeaders, data };
        const accepted = typeof config.validateStatus === 'function'
            ? config.validateStatus(response.status)
            : response.status >= 200 && response.status < 300;
        if (!accepted) {
            const error = new Error(`HTTP ${response.status}`);
            error.response = result;
            throw error;
        }
        return result;
    } finally {
        clearTimeout(timer);
    }
}, {
    request(config) { return axios(config); },
    get(url, config = {}) { return axios({ ...config, method: 'GET', url }); },
    post(url, data, config = {}) { return axios({ ...config, method: 'POST', url, data }); },
});
const fs = require("fs");
const path = require("path");

const ckName = "zippo";
const MINI_APP_ID = "wxaa75ffd8c2d75da7";
const BASE = "https://wx-center.zippo.com.cn";

const TOKEN_CACHE_FILE = path.join(__dirname, "zippo_token_cache.json");
const USER_AGENT =
    "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 MicroMessenger/8.0.48.2580(0x28003036) MiniProgramEnv/android";

const EP_LOGIN = "/api/users/auth";
const EP_SIGN = "/api/daily-signin";
const EP_USER = "/api/users/profile";
const EP_MISSIONS = "/api/missions";

const wechat = new WeChatServer({ appid: MINI_APP_ID });

function readCache() {
    try {
        if (!fs.existsSync(TOKEN_CACHE_FILE)) return {};
        return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf8")) || {};
    } catch (e) {
        return {};
    }
}

function writeCache(cache) {
    try {
        fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
    } catch (e) {
        $.log(`写入token缓存失败: ${e.message || e}`);
    }
}

function parseAccount(raw = "") {
    const [id, remark] = String(raw).split("#").map((s) => (s || "").trim());
    return { openid: id, remark: remark || "" };
}

function short(v, n = 200) {
    const t = typeof v === "string" ? v : JSON.stringify(v);
    return !t ? "" : t.length > n ? `${t.slice(0, n)}...` : t;
}

function form(obj) {
    return Object.entries(obj)
        .map(([k, v]) => `${k}=${encodeURIComponent(v === undefined || v === null ? "" : v)}`)
        .join("&");
}

/** 该后端的成功判定 */
const isOk = (res) => !res?.code;
const msgOf = (res) => res?.message || res?.message || res?.msg || short(res);
/** 每天跑一次，「已签到」必须当成成功而不是失败 */
const isAlreadyDone = (t) => /已签|已经签|签到过|重复|已完成|already/i.test(String(t || ""));
const isAuthError = (t) => /登录|token|未授权|未登录|失效|过期|重新|401/i.test(String(t || ""));
/** 账号态：这个微信号还没在该平台注册/绑定 —— 不是脚本缺陷，别打 ❌ */
const isNotRegistered = (t) => /未注册|未绑定|请先注册|请先绑定|not regist/i.test(String(t || ""));

class Task {
    constructor(raw) {
        this.index = $.userIdx++;
        this.account = parseAccount(raw);
        this.token = "";
        this.signedToday = false;
        // 设备号按 openid 稳定派生：同一账号每次跑都一样，避免被当成新设备
        this.deviceId = "d_" + require("crypto").createHash("md5")
            .update(String(this.account.openid || raw)).digest("hex").slice(0, 16);
    }

    log(text) {
        $.log(`账号[${this.index}]${this.account.remark ? `[${this.account.remark}]` : ""} ${text}`);
    }

    async request(apiPath, body = null, withAuth = true, method = "POST", query = null, epHeaders = null) {
        const isForm = false;
        const headers = {
            "Content-Type": isForm ? "application/x-www-form-urlencoded" : "application/json",
            "User-Agent": USER_AGENT,
            Referer: `https://servicewechat.com/${MINI_APP_ID}/0/page-frame.html`,
            Accept: "application/json, text/plain, */*",
            xweb_xhr: "1",
            "x-app-id": "zippo",
            "x-platform": "wxmp",
            "x-platform-id": MINI_APP_ID,
            "x-platform-env": "release",
            ...(epHeaders || {}),
        };
        if (withAuth && this.token) headers["Authorization"] = `Bearer ${this.token}`;
        const payload = body || {};

        const isGet = String(method).toUpperCase() === "GET";
        // query 独立于 body：有些接口是 POST 但参数只在查询串上
        const qs = query ? form(query) : (isGet && Object.keys(payload).length ? form(payload) : "");
        const res = await axios.request({
            method: isGet ? "GET" : "POST",
            url: `${BASE}${apiPath}${qs ? `?${qs}` : ""}`,
            data: isGet ? undefined : (isForm ? form(payload) : payload),
            headers,
            timeout: 20000,
            validateStatus: () => true,
        });
        if (res.status < 200 || res.status >= 300) {
            // 业务结论常常躺在 4xx/5xx 的 JSON 体里（"今日已签到" 见过 400 也见过 500），
            // 有 JSON 体就交给下游按业务码判，别在这一层抛掉
            if (res.data && typeof res.data === "object") return res.data;
            throw new Error(`${apiPath} HTTP ${res.status}: ${short(res.data)}`);
        }
        return res.data;
    }

    /**
     * wcs.getCode 在 status:false 时也会 resolve，必须自己判失败，
     * 否则 wx_server 的取码限流会被误报成目标站登录失败。
     */
    async getCode() {
        const { data } = await wechat.getCode(this.account.openid);
        if (data && data.status === false) {
            throw new Error(`wx_server 取code失败: ${data.message || short(data)}`);
        }
        const code = data?.data?.code || data?.code;
        if (!code || typeof code !== "string") throw new Error(`wx_server 未返回 code: ${short(data)}`);
        return code;
    }

    async login() {
        const code = await this.getCode();
        const res = await this.request(EP_LOGIN, { code, scene: "1001", platform: "wxmp" }, false, "POST", null, null);
        if (!isOk(res)) throw new Error(`登录失败: ${msgOf(res)}`);
        this.token = res.token || "";

        if (!this.token) throw new Error(`登录未返回 token: ${short(res)}`);
        const cache = readCache();
        cache[this.account.openid] = { token: this.token, updatedAt: new Date().toISOString() };
        writeCache(cache);
        this.log("登录成功");
    }

    async ensureLogin() {
        const cached = readCache()[this.account.openid] || {};
        if (!this.token && cached.token) {
            this.token = cached.token;
            if (await this.queryUser(false)) {
                this.log("使用缓存token");
                return;
            }
            this.log("缓存token失效，重新登录");
            this.token = "";
        }
        if (!this.token) await this.login();
    }

    async queryUser(needLog = true) {
        if (!EP_USER) return true;
        const res = await this.request(EP_USER, {}, true, "GET", null, null);
        if (!isOk(res)) {
            if (needLog) this.log(`读取资料失败: ${msgOf(res)}`);
            return false;
        }
        // 有的家没有 data/body 包装，响应体本身就是数据（zippo 的 profile 就是）
        const d = res.data || res.datas || res.body || res || {};
        if (needLog) {
            this.log(`会员: ${d.memberLevel || "-"}${d.phone ? ` ${d.phone}` : ""}`);
        }
        return true;
    }

    async sign(retry = true) {
        const res = await this.request(EP_SIGN, {}, true, "POST", null, null);
        if (isOk(res)) return this.log("✅ 签到成功");
        if (isAlreadyDone(msgOf(res))) return this.log(`✅ 今日已签到（${msgOf(res)}）`);
        if (isNotRegistered(msgOf(res))) {
            return this.log(`⚠️ ${msgOf(res)} —— 该微信号还没在该平台注册会员，先在小程序里注册一次再跑`);
        }
        if (retry && isAuthError(msgOf(res))) {
            this.log("会话失效，重新登录后重试");
            this.token = "";
            await this.login();
            return this.sign(false);
        }
        this.log(`❌ 签到失败: ${msgOf(res)}`);
    }

    async getDailyMissions() {
        const res = await this.request(EP_MISSIONS, {}, true, "GET");
        if (!isOk(res)) throw new Error(`读取会员任务失败: ${msgOf(res)}`);
        const groups = Array.isArray(res?.list) ? res.list : [];
        const daily = groups.find((group) => group?.type === "daily");
        return Array.isArray(daily?.missions) ? daily.missions : [];
    }

    missionPending(mission) {
        return Number(mission?.completes || 0) < Number(mission?.times || 1);
    }

    rewardPending(mission) {
        return Number(mission?.completes || 0) > Number(mission?.receives || 0);
    }

    async claimMissionReward(mission, label, forceAttempt = false) {
        if (!mission?.id) return this.log(`⚠️ ${label}缺少任务 ID，无法领奖`);
        if (!forceAttempt && !this.rewardPending(mission)) {
            const done = Number(mission?.completes || 0);
            const received = Number(mission?.receives || 0);
            return this.log(done > 0 && received >= done ? `✅ ${label}奖励已领取` : `⚠️ ${label}尚未完成，暂不可领取`);
        }
        const res = await this.request(`/api/missions/${mission.id}/rewards`, { id: mission.id });
        if (isOk(res)) {
            const points = res?.points ?? res?.rewardValue ?? mission?.rewardValue;
            return this.log(`✅ ${label}奖励领取成功${points !== undefined ? `（+${points}积分）` : ""}`);
        }
        const message = msgOf(res);
        if (isAlreadyDone(message) || /领取过|已领取|already.*receiv/i.test(message)) {
            return this.log(`✅ ${label}奖励已领取（${message}）`);
        }
        this.log(`⚠️ ${label}奖励领取未成功: ${message}`);
    }

    async completePageview(mission) {
        if (!this.missionPending(mission)) return this.claimMissionReward(mission, "浏览上新");
        const res = await this.request("/api/missions/records", { code: mission.code || "pageview", missionId: mission.id });
        if (!isOk(res)) {
            const message = msgOf(res);
            if (!isAlreadyDone(message)) return this.log(`❌ 浏览上新失败: ${message}`);
        }
        this.log("✅ 浏览上新已完成");
        const current = (await this.getDailyMissions()).find((item) => item?.code === "pageview") || mission;
        await this.claimMissionReward(current, "浏览上新");
    }

    async completeGoodsFavorite(mission) {
        if (!this.missionPending(mission)) return this.claimMissionReward(mission, "收藏商品");
        const link = String(mission?.link || "");
        const skuId = link.match(/[?&]skuId=([^&]+)/i)?.[1];
        if (!skuId) return this.log("❌ 收藏商品失败: 任务链接未提供 skuId");
        const res = await this.request("/api/favorites", { targetType: "sku", targetId: decodeURIComponent(skuId), favorited: true });
        if (!isOk(res)) {
            const message = msgOf(res);
            if (!isAlreadyDone(message)) return this.log(`❌ 收藏商品失败: ${message}`);
        }
        this.log("✅ 收藏商品已完成");
        const current = (await this.getDailyMissions()).find((item) => item?.code === "goodsfav") || mission;
        await this.claimMissionReward(current, "收藏商品");
    }

    async runMissions() {
        let missions = await this.getDailyMissions();
        const pageview = missions.find((item) => item?.code === "pageview");
        const goodsfav = missions.find((item) => item?.code === "goodsfav");
        if (pageview) await this.completePageview(pageview);
        else this.log("⚠️ 未找到浏览上新任务");
        if (goodsfav) await this.completeGoodsFavorite(goodsfav);
        else this.log("⚠️ 未找到收藏商品任务");

        missions = await this.getDailyMissions();
        const invite = missions.find((item) => item?.code === "invitemember");
        if (invite) {
            const alreadyReceived = Number(invite?.receives || 0) >= Number(invite?.completes || 0) && Number(invite?.receives || 0) > 0;
            if (alreadyReceived) this.log("✅ 邀请好友奖励今日已领取");
            else await this.claimMissionReward(invite, "邀请好友", true);
        } else {
            this.log("⚠️ 未找到邀请好友任务");
        }
    }

    async run() {
        if (!this.account.openid) {
            this.log("跳过：变量值里没有 openid");
            return;
        }
        try {
            await this.ensureLogin();
            await this.queryUser();
            await this.sign();
            await this.runMissions();
        } catch (e) {
            this.log(`执行失败: ${e.message || e}`);
        }
    }
}

!(async () => {
    await $.checkEnv(ckName);
    if (!$.userCount) {
        $.log(`未找到变量 ${ckName}`);
        return;
    }
    for (let i = 0; i < $.userList.length; i++) {
        await new Task($.userList[i]).run();
        if (i < $.userList.length - 1) await $.wait(1500, 3000);
    }
})()
    .catch((e) => $.log(e.message || e))
    .finally(() => $.done());
