async ({ binding, hash }) => {
    // WAA proof 与页面输入框状态强绑定：先写 binding → 确认页面认了 → 再签。
    // 机制来源：Catiecli Playground Executor 2.4.9 / gemini2api 执行器 v2.0.0。
    // 上一版（只把 hash 传给快照函数、完全不碰输入框）在 AI Studio 现行构建上会拿到
    // 403 [7] The caller does not have permission，或者 snapshot 直接静默挂死。
    const dms = window.default_MakerSuite;
    const aistudio = window.__AISTUDIO__ || (window.__AISTUDIO__ = {});
    const service = aistudio.service;
    const snapKey = aistudio.snapKey;
    if (!dms || !service || !snapKey || typeof dms[snapKey] !== 'function') {
        throw new Error('service_unavailable');
    }

    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const isShown = (el) => {
        try {
            return el.checkVisibility({ visibilityProperty: true });
        } catch (e) {
            return true;
        }
    };
    const promptBox = () => {
        const boxes = Array.from(document.querySelectorAll('ms-prompt-box textarea')).filter(isShown);
        if (boxes.length) return boxes[0];
        // 构建差异兜底：有些版本没有 ms-prompt-box 包裹层，退到页面上第一个可见 textarea
        const any = Array.from(document.querySelectorAll('textarea')).filter(isShown);
        return any[0] || null;
    };
    // 写值必须走原生 setter：直接赋值 .value 时 Angular 受控组件收不到变更
    const fillPrompt = (text) => {
        const box = promptBox();
        if (!box) return null;
        const desc = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
        desc.set.call(box, text);
        box.dispatchEvent(new window.InputEvent('input', {
            bubbles: true,
            inputType: 'insertText',
            data: text,
        }));
        box.dispatchEvent(new window.Event('change', { bubbles: true }));
        return box.value;
    };
    // 焦点扰动：长时间后台运行后 Angular 的合成事件不再触发变更检测，页面会「卡住」，
    // 用户手动点一下输入框才恢复。这里主动复现那一下交互。
    const nudgeFocus = (box) => {
        try {
            const el = box || promptBox();
            if (!el) return;
            el.focus();
            el.dispatchEvent(new window.Event('focusin', { bubbles: true }));
            el.blur();
            el.dispatchEvent(new window.Event('focusout', { bubbles: true }));
        } catch (e) {}
    };
    const syncPrompt = async (text) => {
        if (!text) return 'skipped';
        if (!promptBox()) return 'no_box';
        // 8 秒预算：页面偶尔会把输入框值改回去（框架锁值），每两次重试加一次焦点扰动
        for (let i = 0; i < 54; i++) {
            if (fillPrompt(text) === text) {
                nudgeFocus();
                return 'synced';
            }
            if (i % 2 === 0) nudgeFocus();
            await sleep(150);
        }
        return 'sync_failed';
    };

    // snapshot 可能因页面 zone 冻结而永不回调——用硬超时兜底（20s → 30s → 30s）
    const PROOF_TIMEOUTS = [20000, 30000, 30000];
    const runSnapshot = async (timeoutMs) => {
        const timer = new Promise((_, reject) =>
            setTimeout(() => reject(new Error('proof_timeout')), timeoutMs)
        );
        return await Promise.race([
            Promise.resolve(dms[snapKey](service, hash)),
            timer,
        ]);
    };

    const job = async () => {
        const box = promptBox();
        const boxState = box ? await syncPrompt(binding) : 'no_box';
        let lastErr = null;
        for (let round = 0; round < PROOF_TIMEOUTS.length; round++) {
            try {
                const proof = await runSnapshot(PROOF_TIMEOUTS[round]);
                if (proof && typeof proof === 'string') {
                    aistudio.snapshot = proof;
                    return { snapshot: proof, box: boxState };
                }
                lastErr = new Error('empty_snapshot');
            } catch (err) {
                lastErr = err;
            }
            // 每轮失败先扰动焦点、再补一次 binding，然后重签
            nudgeFocus(box);
            await syncPrompt(binding);
            await sleep(300);
        }
        throw new Error('snapshot_failed: ' + ((lastErr && lastErr.message) || lastErr));
    };

    // 与页面自己发请求时一样的串行语义：签名服务是单例上的有状态对象，别交错调用
    const prev = aistudio.snapQueue || Promise.resolve();
    const current = prev.catch(() => {}).then(job);
    aistudio.snapQueue = current;
    return await current;
}
