({ bindingId }) => {
    // 请求结束把 binding 清掉：留在输入框里的话，页面刷新/切页会把它当成用户的草稿。
    // 只清我们自己写进去的值；写值同样走原生 setter + input/change 事件。
    try {
        const state = window.__AISTUDIO__;
        if (!state) return 'not_owned';
        const job = state.bindingJobs && state.bindingJobs[bindingId];
        if (job) job.cancelled = true;
        const owned = state.binding;
        if (!owned || owned.id !== bindingId) return 'not_owned';
        delete state.binding;
        const box = owned.box;
        if (!box || !box.isConnected) return 'no_box';
        if (!box.value) return 'already_empty';
        if (box.value !== owned.text) return 'not_owned';
        const desc = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
        desc.set.call(box, '');
        box.dispatchEvent(new window.InputEvent('input', {
            bubbles: true,
            inputType: 'deleteContentBackward',
            data: null,
        }));
        box.dispatchEvent(new window.Event('change', { bubbles: true }));
        return 'cleared';
    } catch (err) {
        return 'error:' + String(err);
    }
}
