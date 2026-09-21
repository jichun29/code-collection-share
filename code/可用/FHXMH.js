// name: 飞鹤星妈会
// cron: 8 15,3 * * *
const axios = require("axios");
// ====================== YYB Go 账号（环境变量 YYB_SERVER = 地址@微信账号标识，多行） ======================
const SERVERS = (process.env.YYB_SERVER || "")
    .split(/\r?\n/)
    .map(s => s.trim())
    .filter(Boolean);
if (!SERVERS.length) {
    console.error("未配置环境变量 YYB_SERVER，请设置后重试（格式：地址@微信账号标识，多行换行）");
    process.exit(1);
}
function parseYybGoEntry(rawValue) {
    const value = String(rawValue || "").trim();
    if (!value) return { server: "", ref: "" };
    const atIndex = value.indexOf("@");
    if (atIndex === -1) {
        console.log("YYB_SERVER 格式应为 地址@微信账号标识，当前值: " + value);
        return { server: "", ref: "" };
    }
    let server = value.slice(0, atIndex).trim();
    const ref = value.slice(atIndex + 1).trim();
    if (server.startsWith("http://")) server = server.slice(7);
    else if (server.startsWith("https://")) server = server.slice(8);
    server = server.replace(/\/+$/, "");
    if (!server || !ref) return { server: "", ref: "" };
    return { server, ref };
}
async function getCode(server) {
    const { server: parsedServer, ref } = parseYybGoEntry(server);
    if (!parsedServer || !ref) return null;
    const url = "http://" + parsedServer + "/wxapp/getCode";
    try {
        const { data } = await axios.post(url, { ref, app_id: 'wxc83b55d61c7fc51d' }, { timeout: 20000, proxy: false });
        const code = data && data.data && data.data.result && data.data.result.code;
        if (!data || data.code !== 0 || !code) {
            console.log(parsedServer + " 获取code失败: " + JSON.stringify(data));
            return null;
        }
        console.log(parsedServer + " 获取code成功");
        return code;
    } catch (e) {
        console.log(parsedServer + " 获取code异常: " + e.message);
        return null;
    }
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
let userIdx = 1;

const APP = { name: "飞鹤星妈会", appid: "wxc83b55d61c7fc51d" };

const USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) MicroMessenger/3.9.12 MiniProgramEnv/Windows WindowsWechat/WMPF";

function short(value, max = 220) {
    if (value === undefined || value === null) return "";
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > max ? `${text.slice(0, max)}...` : text;
}

function getByPath(obj, path) {
    return String(path)
        .split(".")
        .reduce((cur, key) => (cur && cur[key] !== undefined ? cur[key] : undefined), obj);
}

function findFirst(obj, predicate, depth = 0) {
    if (!obj || depth > 8) return null;
    if (Array.isArray(obj)) {
        for (const item of obj) {
            const found = findFirst(item, predicate, depth + 1);
            if (found) return found;
        }
        return null;
    }
    if (typeof obj === "object") {
        if (predicate(obj)) return obj;
        for (const value of Object.values(obj)) {
            const found = findFirst(value, predicate, depth + 1);
            if (found) return found;
        }
    }
    return null;
}

async function request(options) {
    const res = await axios.request({
        timeout: 20000,
        validateStatus: () => true,
        ...options,
        headers: {
            "User-Agent": USER_AGENT,
            Accept: "application/json, text/plain, */*",
            ...(options.headers || {}),
        },
    });
    return { status: res.status, headers: res.headers || {}, data: res.data };
}

async function getWxCode(server) {
        return await getCode(server);
    }


class FeiheMom {
    constructor(openid) {
        this.server = openid;
        const _yyb = parseYybGoEntry(this.server);
        this.ref = _yyb.ref;
        this.openid = _yyb.ref;
        this.openid = openid;
        this.base = "https://momclub.feihe.com/capis";
        this.token = "";
    }

    async api({ method = "GET", path, data, allowFail = false }) {
        const opts = {
            method,
            url: `${this.base}${path}`,
            headers: {
                Authorization: this.token,
                locale: "zh_CN",
                "content-type": "application/json",
            },
        };
        if (method === "GET") opts.params = data || {};
        else opts.data = data === undefined ? {} : data;
        const res = await request(opts);
        const ok = res.status === 200 && ["00000", "000000", "A00002"].includes(String(res.data?.code));
        if (!ok && !allowFail) throw new Error(`HTTP ${res.status}: ${short(res.data)}`);
        return res.data;
    }

    async login() {
        const code = await getWxCode(this.server);
        const res = await request({
            method: "POST",
            url: `${this.base}/social/ma`,
            headers: { "content-type": "application/json", locale: "zh_CN" },
            data: code,
            transformRequest: [(data) => data],
        });
        const token = res.data?.data?.tokenInfo?.accessToken || res.data?.data?.accessToken || "";
        if (res.status !== 200 || !token) throw new Error(`登录失败 HTTP ${res.status}: ${short(res.data)}`);
        this.token = token;
        return `token=${token.slice(0, 8)}***`;
    }

    async query() {
        const member = await this.api({ path: "/c/user/memberInfo", allowFail: true });
        const user = await this.api({ path: "/p/user/userInfo", allowFail: true });
        const data = member?.data || user?.data || {};
        const score = data.score || data.points || data.integral || data.availableScore || data.totalScore;
        const name = data.nickName || data.nickname || data.memberName || data.mobile || data.phone || "";
        return `用户=${name || "未知"} 积分=${score ?? "未知"} member=${short(member?.data || member, 120)}`;
    }

    async sign() {
        const todo = await this.api({
            path: "/c/activity/todo/list",
            data: { mockTime: Date.now() },
            allowFail: true,
        });
        const checkTodo =
            getByPath(todo, "data.checkInTodo") ||
            findFirst(todo?.data, (item) => item && (item.checkInExtra || /签到|打卡|check/i.test(`${item.taskName || item.name || item.title || ""}`)));
        const activityId = checkTodo?.id || checkTodo?.activityId || checkTodo?.taskId;
        if (!activityId) return `未找到签到任务: ${short(todo)}`;
        const todaySigned =
            checkTodo?.todaySigned ||
            checkTodo?.signed ||
            checkTodo?.finish ||
            checkTodo?.completed ||
            checkTodo?.status === 1 ||
            checkTodo?.state === 1;
        if (todaySigned) return `今日已签到 activityId=${activityId}`;
        const sign = await this.api({
            method: "POST",
            path: "/c/activity/todo/checkIn",
            data: { activityId, mockTime: Date.now() },
            allowFail: true,
        });
        return `签到接口返回: ${short(sign)}`;
    }
}

async function runAccount(openid, index) {
    console.log(`\n========== ${APP.name} 账号[${index}] ${openid} ==========`);
    const runner = new FeiheMom(openid);
    try {
        console.log(`登录：${await runner.login()}`);
        console.log(`查询：${await runner.query()}`);
        console.log(`签到：${await runner.sign()}`);
    } catch (e) {
        console.log(`执行失败：${e.message || e}`);
    }
}

(async () => {
        if (!SERVERS.length) {
        console.log(`未配置 ${"YYB_SERVER"}`);
        return;
    }
    console.log(`共找到${SERVERS.length}个账号`);
    for (let i = 0; i < SERVERS.length; i++) {
        await runAccount(SERVERS[i], i + 1);
        await sleep(800);
    }
})().catch((e) => {
    console.log(`脚本异常：${e.stack || e.message || e}`);
});
