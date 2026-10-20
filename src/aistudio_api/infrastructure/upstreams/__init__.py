"""非浏览器上游通道（Vertex Express / Console batchGraphql）。

选择逻辑在 api/app.py 的 lifespan：``AISTUDIO_UPSTREAM`` 决定用哪条通道构造
runtime_state.client（aistudio / vertex_express / console_graphql）。新通道实现
AIStudioClient 的调用面（generate_content / stream_generate_content / warmup /
clear_templates，``_session=None`` 使浏览器重启分支成为 no-op）。
"""
