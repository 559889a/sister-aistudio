"""非浏览器上游通道（Vertex Express / Console batchGraphql）。

api/app.py 的 lifespan 把三通道注册进 UpstreamRegistry；模型前缀选择客户端，
``AISTUDIO_UPSTREAM`` 只覆盖无前缀模型的默认通道。runtime_state.client 始终
保留浏览器客户端供账号管理使用；无浏览器通道 ``_session=None`` 不参与 PG 调度。
"""
