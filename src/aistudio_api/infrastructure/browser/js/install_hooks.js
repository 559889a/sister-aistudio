((() => {
    window.__AISTUDIO__ = window.__AISTUDIO__ || {};
    // 只在首装时初始化：document-start 注入后会轮询重试，重复初始化会把刚捕获的模板清掉
    if (window.__AISTUDIO__.interceptBootstrap === undefined) {
        window.__AISTUDIO__.interceptBootstrap = true;
    }
    if (window.__AISTUDIO__.capturedTemplate === undefined) {
        window.__AISTUDIO__.capturedTemplate = null;
    }

    // ==================== 阶段 0：与「发消息」无关的两处采集 ====================
    // 这两段来自 gemini2api 执行器 v2.0.0：页面一加载就能把 WAA 服务与公共请求头
    // 拿在手里，**不需要用户在页面上发任何一条消息**。
    //   - 服务：Angular DI 造出来的 BotGuardService 单例，从 inject(TB) 与
    //     TB.prototype.initialize / getMetadata 接住 this；
    //   - 头：页面自己的 `/$rpc/` 请求带的那套（x-goog-api-key / x-aistudio-*），
    //     合并保留，缺 x-goog-ext-* 也没关系（读类 RPC 同样带会话级头）。

    const denyHeader = (name) => /^(content-length|host|connection)$/i.test(String(name));

    const recordHeaders = (headers) => {
        try {
            const out = {};
            if (headers && typeof headers.forEach === 'function' && !Array.isArray(headers)) {
                headers.forEach((value, name) => {
                    if (!denyHeader(name)) out[String(name).toLowerCase()] = String(value);
                });
            } else if (Array.isArray(headers)) {
                for (const pair of headers) {
                    if (pair && pair.length >= 2 && !denyHeader(pair[0])) {
                        out[String(pair[0]).toLowerCase()] = String(pair[1]);
                    }
                }
            } else if (headers && typeof headers === 'object') {
                for (const name of Object.keys(headers)) {
                    if (!denyHeader(name)) out[String(name).toLowerCase()] = String(headers[name]);
                }
            }
            if (Object.keys(out).length === 0) return;
            // 合并而不是覆盖：启动 RPC 的公共头不能被后来的请求冲掉
            window.__AISTUDIO__.rpcHeaders = Object.assign({}, window.__AISTUDIO__.rpcHeaders || {}, out);
        } catch (e) {}
    };

    const isRpcUrl = (url) => String(url || '').includes('/$rpc/');

    const installServiceTap = () => {
        const dms = window.default_MakerSuite;
        // 只依赖 `dms.n`（页面自己的 inject）。**别再要求 `dms.TB`**：
        // 2026-10-20 的构建里 `default_MakerSuite.TB` 已经不存在了，
        // 旧探针因此一次都没装上，binding 引导每次启动都白等 35 秒才回退 page_send。
        if (!dms || typeof dms.n !== 'function') return false;
        if (window.__AISTUDIO__.serviceTapInstalled) return true;
        window.__AISTUDIO__.serviceTapInstalled = true;

        const capture = (instance, via) => {
            const state = window.__AISTUDIO__;
            if (!instance || state.service) return;
            state.service = instance;
            state.captureVia = via;
        };

        // 新版构建认形状：BotGuardService 的签名器挂在 `service.A.snapshot` 上
        // （旧构建的 `dms.TB` 只是那个类的导出名，名字随构建变、形状不变）。
        // 2026-10-20 补双方法锚：_.YB 原型上的 initialize/getMetadata 未被压缩
        // （incident-static-evidence-2026-10-20.md B-1），压缩属性 A 换名时靠它兜住。
        const looksLikeService = (obj) => {
            try {
                if (!obj || typeof obj !== 'object') return false;
                if (
                    typeof obj.initialize === 'function' &&
                    typeof obj.getMetadata === 'function'
                ) {
                    return true;
                }
                return !!(
                    obj.A &&
                    typeof obj.A === 'object' &&
                    typeof obj.A.snapshot === 'function'
                );
            } catch (e) {
                return false;
            }
        };

        try {
            const inject = dms.n;
            if (!inject.__aistudio_tap) {
                const wrapped = function (token) {
                    const out = inject.apply(this, arguments);
                    try {
                        if (dms.TB && token === dms.TB) {
                            capture(out, 'inject(TB)');
                        } else if (looksLikeService(out)) {
                            capture(out, 'inject(shape)');
                        }
                    } catch (e) {}
                    return out;
                };
                wrapped.__aistudio_tap = true;
                dms.n = wrapped;
            }
        } catch (e) {}

        // 旧构建兼容：TB.prototype.initialize / getMetadata 上接 this
        try {
            const proto = (dms.TB && dms.TB.prototype) || {};
            for (const method of ['initialize', 'getMetadata']) {
                const orig = proto[method];
                if (typeof orig !== 'function' || orig.__aistudio_tap) continue;
                const wrapped = function () {
                    capture(this, 'TB.' + method);
                    return orig.apply(this, arguments);
                };
                wrapped.__aistudio_tap = true;
                proto[method] = wrapped;
            }
        } catch (e) {}
        return true;
    };

    // 每进一次页面（含 install 重试）都尝试装一次探针；已在则不重复
    installServiceTap();

    const dms = window.default_MakerSuite;
    if (!dms) return 'no_default_MakerSuite';

    const isSnapCandidate = (fn) => {
        if (typeof fn !== 'function') return false;
        try {
            const s = fn.toString();
            return (s.includes('.snapshot(') || s.includes('snapshot({') || s.includes('.snapshot)')) &&
                   (s.includes('content') || s.includes('e9b') || s.includes('Bcc') || s.includes('yield') || s.includes('Promise'));
        } catch (e) {
            return false;
        }
    };

    // Auto-detect snapshot function via multi-variant feature matching
    let snapKey = window.__AISTUDIO__.snapKey || null;
    if (!snapKey) {
        for (const k of Object.keys(dms)) {
            try {
                if (isSnapCandidate(dms[k])) {
                    snapKey = k;
                    break;
                }
            } catch(e) {}
        }
    }
    if (!snapKey) return 'no_snapshot_fn';

    // Hook snapshot function to capture service
    if (!dms[snapKey].__api_hooked) {
        const origSnap = dms[snapKey];
        dms[snapKey] = function(...args) {
            window.__AISTUDIO__.service = args[0];
            const result = origSnap.apply(this, args);
            if (result instanceof Promise) {
                return result.then(s => {
                    window.__AISTUDIO__.snapshot = s;
                    return s;
                });
            }
            window.__AISTUDIO__.snapshot = result;
            return result;
        };
        dms[snapKey].__api_hooked = true;
    }

    // Intercept prompt save requests and one-shot bootstrap GenerateContent in-page
    if (!window.__api_fetch_hooked && typeof window.fetch === 'function') {
        const origFetch = window.fetch;
        window.fetch = function(...args) {
            try {
                const req = args[0];
                const init = args[1] || {};
                const url = typeof req === 'string' ? req : (req && req.url ? req.url : '');
                if (isRpcUrl(url)) {
                    recordHeaders(init.headers || (req && req.headers));
                }
                if (url.includes('SavePrompt') || url.includes('CreatePrompt')) {
                    return (async () => new Response(JSON.stringify([]), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json+protobuf' }
                    }))();
                }
                if (
                    window.__AISTUDIO__ &&
                    window.__AISTUDIO__.interceptBootstrap &&
                    url.includes('GenerateContent') &&
                    !url.includes('Count')
                ) {
                    const rawBody = init.body !== undefined ? init.body : '';
                    const bodyStr = typeof rawBody === 'string' ? rawBody : '';
                    if (bodyStr) {
                        const headersObj = { 'Referer': 'https://aistudio.google.com/' };
                        const rawHeaders = init.headers || (req && req.headers);
                        if (rawHeaders) {
                            if (typeof rawHeaders.forEach === 'function') {
                                rawHeaders.forEach((v, k) => { headersObj[String(k)] = String(v); });
                            } else if (Array.isArray(rawHeaders)) {
                                for (const pair of rawHeaders) {
                                    if (pair && pair.length >= 2) {
                                        headersObj[String(pair[0])] = String(pair[1]);
                                    }
                                }
                            } else if (typeof rawHeaders === 'object') {
                                for (const k of Object.keys(rawHeaders)) {
                                    headersObj[String(k)] = String(rawHeaders[k]);
                                }
                            }
                        }
                        window.__AISTUDIO__.capturedTemplate = {
                            url: url,
                            headers: headersObj,
                            body: bodyStr
                        };
                        window.__AISTUDIO__.interceptBootstrap = false;
                        return Promise.reject(new DOMException('Bootstrap GenerateContent intercepted', 'AbortError'));
                    }
                }
            } catch(e) {}
            return origFetch.apply(this, args);
        };
        window.__api_fetch_hooked = true;
    }

    if (!window.__api_xhr_hooked && typeof window.XMLHttpRequest === 'function') {
        const proto = window.XMLHttpRequest.prototype;
        const origOpen = proto.open;
        const origSetHeader = proto.setRequestHeader;
        const origSend = proto.send;

        proto.open = function(method, url, ...rest) {
            try {
                this.__aistudio_method = String(method || 'GET');
                this.__aistudio_url = String(url || '');
                this.__aistudio_headers = { 'Referer': 'https://aistudio.google.com/' };
            } catch(e) {}
            return origOpen.call(this, method, url, ...rest);
        };

        proto.setRequestHeader = function(name, value) {
            try {
                if (!this.__aistudio_headers) {
                    this.__aistudio_headers = { 'Referer': 'https://aistudio.google.com/' };
                }
                this.__aistudio_headers[String(name)] = String(value);
            } catch(e) {}
            return origSetHeader.call(this, name, value);
        };

        proto.send = function(body) {
            try {
                const url = this.__aistudio_url || '';
                if (isRpcUrl(url)) {
                    recordHeaders(this.__aistudio_headers);
                }
                if (url.includes('SavePrompt') || url.includes('CreatePrompt')) {
                    setTimeout(() => { try { this.abort(); } catch(e) {} }, 0);
                    return;
                }
                // 被动取证：Proxy RPC（Build 应用沙箱的 fetch 桥在主页面发，见
                // 逆向相关内容/ Jn51xb.js REd/SEd）。不拦截不篡改，只记请求模板
                // 与流式响应分帧，供 ProxyStreamedCall 重放逆向用。
                if (
                    url.includes('ProxyStreamedCall') &&
                    window.__AISTUDIO__ &&
                    window.__AISTUDIO__.proxyCapture !== 'done'
                ) {
                    const cap = {
                        url: url,
                        method: this.__aistudio_method || 'POST',
                        headers: Object.assign({}, this.__aistudio_headers || {}),
                        body: typeof body === 'string' ? body : '',
                        responseChunks: []
                    };
                    window.__AISTUDIO__.proxyCapture = cap;
                    let seen = 0;
                    this.addEventListener('readystatechange', () => {
                        try {
                            const rt = this.responseText || '';
                            if (rt.length > seen) {
                                cap.responseChunks.push(rt.slice(seen));
                                seen = rt.length;
                            }
                        } catch(e) {}
                    });
                    this.addEventListener('loadend', () => {
                        try {
                            cap.status = this.status;
                            cap.responseHeaders = this.getAllResponseHeaders();
                            cap.fullResponse = this.responseText || '';
                            cap.done = true;
                            window.__AISTUDIO__.proxyCapture = 'done';
                        } catch(e) {}
                    });
                }
                if (
                    window.__AISTUDIO__ &&
                    window.__AISTUDIO__.interceptBootstrap &&
                    url.includes('GenerateContent') &&
                    !url.includes('Count')
                ) {
                    const bodyStr = typeof body === 'string' ? body : '';
                    if (bodyStr) {
                        window.__AISTUDIO__.capturedTemplate = {
                            url: url,
                            headers: Object.assign({}, this.__aistudio_headers || {}),
                            body: bodyStr
                        };
                        window.__AISTUDIO__.interceptBootstrap = false;
                        setTimeout(() => { try { this.abort(); } catch(e) {} }, 0);
                        return;
                    }
                }
            } catch(e) {}
            return origSend.call(this, body);
        };
        window.__api_xhr_hooked = true;
    }
    window.__AISTUDIO__.hooked = true;
    window.__AISTUDIO__.snapKey = snapKey;
    return 'hooked:' + snapKey;
})())
