(args) => {
    return new Promise((resolve) => {
        const controller = new AbortController();

        const headers = Object.assign({}, args.headers || {});

        window.fetch(args.url, {
            method: 'POST',
            headers: headers,
            body: args.gzip ? Uint8Array.from(atob(args.body), c => c.charCodeAt(0)) : args.body,
            credentials: 'include',
            signal: controller.signal,
        }).then(async (response) => {
            try {
                const text = await response.text();
                resolve({status: response.status, body: text});
            } catch (e) {
                resolve({status: response.status, body: ''});
            }
        }).catch((err) => {
            const aborted = err && err.name === 'AbortError';
            resolve({status: 0, body: aborted ? 'aborted' : 'network error'});
        });
    });
}
