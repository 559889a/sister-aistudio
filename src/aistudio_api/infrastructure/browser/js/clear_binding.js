() => {
    // 请求结束把 binding 清掉：留在输入框里的话，页面刷新/切页会把它当成用户的草稿。
    // 只清我们自己写进去的值；写值同样走原生 setter + input/change 事件。
    try {
        const isShown = (el) => {
            try {
                return el.checkVisibility({ visibilityProperty: true });
            } catch (e) {
                return true;
            }
        };
        const boxes = Array.from(document.querySelectorAll('ms-prompt-box textarea'));
        const box =
            boxes.filter(isShown)[0] ||
            boxes[0] ||
            Array.from(document.querySelectorAll('textarea')).filter(isShown)[0] ||
            null;
        if (!box) return 'no_box';
        if (!box.value) return 'already_empty';
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
