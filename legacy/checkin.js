const axios = require('axios');
const fs = require('fs');
const path = require('path');

// 模拟 Python 脚本中的 Cloudflare 检测逻辑 (占位符)
function detectCloudflareBlock(status, text) {
    if (!text) return false;
    const cfKeywords = ['cloudflare', 'checking your browser', 'attention required'];
    const isBlocked = cfKeywords.some(k => text.toLowerCase().includes(k)) && [403, 503].includes(status);
    return isBlocked;
}

async function runCheckins() {
    const configPath = path.join(__dirname, 'visit_config.json');
    let tasks;

    try {
        const rawData = fs.readFileSync(configPath, 'utf-8');
        tasks = JSON.parse(rawData);
    } catch (error) {
        console.error(`❌ 读取配置文件失败: ${error.message}`);
        return;
    }

    if (!Array.isArray(tasks)) {
        console.error('❌ 配置文件格式错误，应为纯数组格式');
        return;
    }

    console.log(`🚀 开始执行 ${tasks.length} 个账号的签到任务...\n`);

    for (let i = 0; i < tasks.length; i++) {
        const task = tasks[i];
        const taskName = task.name || `Task_${i + 1}`;
        const baseUrl = task.url.replace(/\/$/, ''); // 去除末尾斜杠
        const cookie = task.cookie;
        const proxyUrl = task.proxy || null;
        
        // 使用实例对象来保存运行时的状态（如自动获取的 userId）
        let currentUserId = task.userId;

        console.log(`[${i + 1}/${tasks.length}] 正在处理: ${taskName}`);

        if (!cookie) {
            console.log(`⚠️ [${taskName}] 跳过: 缺少 Cookie\n`);
            continue;
        }

        // 创建 Axios 实例
        const instance = axios.create({
            baseURL: baseUrl,
            timeout: 15000,
            proxy: proxyUrl ? new URL(proxyUrl) : false,
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Cookie': cookie
            }
        });

        // 1. 确保拥有 User ID (自动获取逻辑)
        if (!currentUserId) {
            console.log(`🔍 [${taskName}] 未配置 userId，正在尝试自动获取...`);
            try {
                const selfResp = await instance.get('/api/user/self');
                if (selfResp.data && selfResp.data.success && selfResp.data.data && selfResp.data.data.id) {
                    currentUserId = selfResp.data.data.id;
                    console.log(`✅ [${taskName}] 自动获取 userId 成功: ${currentUserId}`);
                    // 更新请求头
                    instance.defaults.headers.common['New-Api-User'] = String(currentUserId);
                } else {
                    console.log(`❌ [${taskName}] 自动获取 userId 失败: ${selfResp.data?.message || '未知错误'}`);
                    continue;
                }
            } catch (err) {
                console.log(`❌ [${taskName}] 获取用户信息网络错误: ${err.message}`);
                continue;
            }
        } else {
            // 如果配置了 userId，直接设置到 Header
            instance.defaults.headers.common['New-Api-User'] = String(currentUserId);
        }

        // 2. 执行签到
        let checkinSuccess = false;
        let checkinMsg = '';

        try {
            const checkinResp = await instance.post('/api/user/checkin');
            const data = checkinResp.data;

            if (data && data.success) {
                checkinSuccess = true;
                checkinMsg = data.message || '签到成功';
                const quota = data.data?.quota_awarded || 0;
                console.log(`✅ [${taskName}] 签到成功! 获得额度: ${quota}`);
            } else {
                // 处理已签到或失败的情况
                const msg = data?.message || '未知错误';
                if (msg.includes('已签到') || msg.includes('already')) {
                    checkinSuccess = true;
                    checkinMsg = msg;
                    console.log(`ℹ️ [${taskName}] ${msg}`);
                } else {
                    checkinMsg = msg;
                    console.log(`⚠️ [${taskName}] 签到失败: ${msg}`);
                    
                    // 如果是 401 且没有 userId，尝试重新获取一次（模拟 Python 的 _try_login 逻辑简化版）
                    if (checkinResp.status === 401 && !task.userId) {
                         console.log(`🔄 [${taskName}] 检测到 401，尝试重新获取用户信息...`);
                         // 这里可以扩展重试逻辑，为了简洁暂略
                    }
                }
            }

        } catch (error) {
            if (error.response) {
                const status = error.response.status;
                const text = error.response.data;
                
                if (detectCloudflareBlock(status, JSON.stringify(text))) {
                    console.log(`🛡️ [${taskName}] 检测到 Cloudflare 拦截! Node.js 无法自动绕过，请检查 Cookie 是否包含 cf_clearance`);
                } else {
                    console.log(`❌ [${taskName}] 请求错误: HTTP ${status}`);
                }
                checkinMsg = `HTTP ${status}`;
            } else {
                console.log(`❌ [${taskName}] 网络异常: ${error.message}`);
                checkinMsg = error.message;
            }
        }

        console.log(''); // 换行
    }

    console.log("🏁 所有任务执行完毕。");
}

runCheckins();