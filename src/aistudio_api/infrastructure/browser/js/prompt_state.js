() => {
    // 面板/引导日志用的输入框与鉴权状态快照：把「为什么签不动」摆到明面上。
    // 对应 gemini2api 执行器面板的「输入框 / 页面可见 / 页面焦点 / binding 状态」四行。
    try {
        const isShown = (el) => {
            try {
                return el.checkVisibility({ visibilityProperty: true });
            } catch (e) {
                return true;
            }
        };
        const boxes = Array.from(document.querySelectorAll('ms-prompt-box textarea')).filter(isShown);
        const box =
            boxes[0] || Array.from(document.querySelectorAll('textarea')).filter(isShown)[0] || null;
        const aistudio = window.__AISTUDIO__ || {};
        const headers = aistudio.rpcHeaders || {};
        return {
            has_box: !!box,
            boxes: boxes.length,
            value_len: box ? String(box.value || '').length : 0,
            focused: !!box && document.activeElement === box,
            visible: typeof document.visibilityState === 'string' ? document.visibilityState : '',
            has_focus: typeof document.hasFocus === 'function' ? document.hasFocus() : null,
            service: !!aistudio.service,
            capture_via: String(aistudio.captureVia || ''),
            snap_key: String(aistudio.snapKey || ''),
            header_names: Object.keys(headers),
            has_api_key: !!(headers['x-goog-api-key'] || headers['x-aistudio-visit-id']),
        };
    } catch (err) {
        return { error: String(err) };
    }
}
