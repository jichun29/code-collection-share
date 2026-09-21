// name: 蜜雪冰城
// cron: 40 10,22 * * *
const axios = require("axios");
const rs = require("jsrsasign");

// ====================== 配置项 ======================
// PushPlus 通知Token（在青龙面板环境变量中设置 PLUSPLUS_TOKEN）
const PLUSPLUS_TOKEN = process.env.PLUSPLUS_TOKEN || "";

// 从环境变量 YYB_SERVER 读取内网wxcode服务，多条换行分隔
let SERVERS = [];
const envYybGo = process.env.YYB_SERVER || "";
if (envYybGo) {
    SERVERS = envYybGo
        .split(/\r?\n/)
        .map(item => item.trim())
        .filter(item => item.length > 0);
}
// 无有效地址直接退出并提示
if (SERVERS.length === 0) {
    console.error("未配置环境变量 YYB_SERVER，请设置后重试（格式：地址@微信账号标识，多行换行）");
    process.exit(1);
}
console.log(`✅ 读取到 ${SERVERS.length} 个 YYB Go 账号`);
console.log("----------------------------------------\n");

// 固定配置（无需修改）
const APP_ID = "d82be6bbc1da11eb9dd000163e122ecb";
const MINI_APP_ID = "wx7696c66d2245d107";
const UA = "Mozilla/5.0 (Linux; Android 15; 22061218C Build/AQ3A.250226.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.177 Mobile Safari/537.36 XWEB/1460075 MMWEBSDK/20260202 MMWEBID/6435 MicroMessenger/8.0.71.3080(0x28004761) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 miniProgram/wx7696c66d2245d107";

const privateKeyString = `-----BEGIN PRIVATE KEY-----
<REDACTED>
-----END PRIVATE KEY-----`;

// ====================== 工具函数 ======================
function ts13() {
    return Date.now();
}

// RSA签名
function getSHA256withRSA(content) {
    const key = rs.KEYUTIL.getKey(privateKeyString);
    const sig = new rs.KJUR.crypto.Signature({ alg: "SHA256withRSA" });
    sig.init(key);
    sig.updateString(content);
    return rs.hextob64u(sig.sign());
}

// PushPlus通知函数
async function sendPlusPlusNotification(title, content) {
    if (!PLUSPLUS_TOKEN) return;
    try {
        await axios.post("https://www.pushplus.plus/send", {
            token: PLUSPLUS_TOKEN,
            title: title,
            content: content,
            template: "txt"
        }, { timeout: 5000 });
        console.log("✅ 通知推送成功");
    } catch (e) {
        console.log("❌ 通知推送失败：", e.message);
    }
}

// 查询雪王币
async function getUserPoint(token) {
    try {
        const t = ts13();
        const sign = getSHA256withRSA(`appId=${APP_ID}&t=${t}`);
        const { data } = await axios.get("https://mxsa.mxbc.net/api/v1/customer/info", {
            params: { t, appId: APP_ID, sign },
            headers: {
                "Access-Token": token,
                "version": "2.8.27",
                "User-Agent": UA
            },
            timeout: 8000
        });
        return data.code === 0 ? parseInt(data.data.customerPoint) : 0;
    } catch (e) {
        return 0;
    }
}

// 魔法铺任务
async function doMagicShop(token) {
    try {
        const t = ts13();
        const sign = getSHA256withRSA(`appId=${APP_ID}&t=${t}`);
        await axios.get("https://mxsa.mxbc.net/api/v1/duiba/getLoginUrl", {
            params: { appId: APP_ID, t, sign, dbredirect: "" },
            headers: { "Access-Token": token, "version": "2.8.27", "User-Agent": UA },
            timeout: 10000
        });
        return true;
    } catch (e) {
        return false;
    }
}

// 单个服务器执行逻辑
async function getCode(server) {
    // server 格式: "ip:port@ref" 或 "ip:port"
    const atIndex = server.lastIndexOf("@");
    const addr = atIndex === -1 ? server.trim() : server.slice(0, atIndex).trim();
    const ref = atIndex === -1 ? "" : server.slice(atIndex + 1).trim();
    
    // 获取 app_id（不同脚本的 APPID/MINI_APP_ID）
    const appId = (typeof APPID !== "undefined") ? APPID : (typeof MINI_APP_ID !== "undefined") ? MINI_APP_ID : "";
    
    try {
        const { data } = await axios.post("http://" + addr + "/wxapp/getCode", {
            ref: ref || "owNAX6gQdCIdZKWsm2c6adr7_eZY",
            app_id: appId
        }, { timeout: 20000, proxy: false });
        const code = data?.data?.result?.code;
        if (data?.code !== 0 || !code) {
            console.log("❌ " + addr + " 获取code失败: " + JSON.stringify(data));
            return null;
        }
        console.log("✅ " + addr + " 获取code成功");
        return code;
    } catch (e) {
        console.log("❌ " + addr + " 获取code异常: " + e.message);
        return null;
    }
}
async function runServer(server) {
    let result = {
        server: server,
        success: false,
        before: 0,
        after: 0,
        gain: 0,
        error: ""
    };

    console.log(`\n==============================`);
    console.log(`蜜雪冰城 - ${server} 账号任务`);
    console.log(`==============================`);

    try {
        // 1. 获取登录code
        const code = await getCode(server);
        if (!code) throw new Error("获取code失败");

        // 2. code换session
        const t1 = ts13();
        const session = await axios.post("https://mxsa.mxbc.net/api/v1/app/code2Session", {
            code, miniAppId: MINI_APP_ID, t: t1, appId: APP_ID,
            sign: getSHA256withRSA(`appId=${APP_ID}&code=${code}&miniAppId=${MINI_APP_ID}&t=${t1}`)
        }, { headers: { version: "2.8.27" } });

        const { openid, unionid } = session.data.data;

        // 3. 登录获取token
        const t2 = ts13();
        const loginRes = await axios.post("https://mxsa.mxbc.net/api/v2/app/loginByAuthCode", {
            authCode: code, openId: openid, unionid, third: "wxmini", miniAppId: MINI_APP_ID,
            t: t2, appId: APP_ID,
            sign: getSHA256withRSA(`appId=${APP_ID}&authCode=${code}&miniAppId=${MINI_APP_ID}&openId=${openid}&t=${t2}&third=wxmini&unionid=${unionid}`)
        }, { headers: { version: "2.8.27", "x-ssos-cid": unionid } });

        const token = loginRes.data.data.accessToken;
        const before = await getUserPoint(token);
        console.log(`✅ ${server} 登录成功 | 当前雪王币：${before}`);

        // 4. 执行任务
        console.log(`\n执行任务：访问魔法铺...`);
        await doMagicShop(token);
        await new Promise(r => setTimeout(r, 1500));

        // 5. 结果展示
        const after = await getUserPoint(token);
        const gain = Math.max(0, after - before);

        console.log(`\n======================================`);
        console.log(`💎 ${server} 执行前：${before} 雪王币`);
        console.log(`✅ ${server} 本次获得：${gain} 雪王币`);
        console.log(`💎 ${server} 执行后：${after} 雪王币`);
        console.log(`======================================`);

        result.success = true;
        result.before = before;
        result.after = after;
        result.gain = gain;

    } catch (e) {
        result.error = e.message;
        console.log(`❌ ${server} 执行失败：`, e.message);
    }
    return result;
}

// ====================== 主逻辑 ======================
async function run() {
    const results = [];
    // 顺序执行所有服务器
    for (const server of SERVERS) {
        const res = await runServer(server);
        results.push(res);
        // 账号间间隔2秒，避免请求过快
        await new Promise(r => setTimeout(r, 2000));
    }

    // 汇总结果并推送通知
    let notifyContent = "### 蜜雪冰城多账号任务执行结果\n";
    results.forEach(res => {
        if (res.success) {
            notifyContent += `\n#### ${res.server}
- 执行状态：成功
- 执行前雪王币：${res.before}
- 本次获得：${res.gain}
- 执行后雪王币：${res.after}
`;
        } else {
            notifyContent += `\n#### ${res.server}
- 执行状态：失败
- 失败原因：${res.error}
`;
        }
    });

    await sendPlusPlusNotification("蜜雪冰城多账号任务完成", notifyContent);
}

// 启动
(async () => {
    console.log("🚀 蜜雪冰城 魔法铺多账号任务");
    await run();
    console.log("\n🏁 所有账号任务执行完成！");
})();
