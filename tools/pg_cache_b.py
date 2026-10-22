"""PG 通道隐式缓存 AB 黑箱采样 —— 脚本 B（热前缀臂）。

与脚本 A 同一份引擎，唯一区别是本脚本默认跑 **B 臂**：全部轮次复用同一份长前缀，
只换末尾片段与问题；预热一发写入缓存后，采样 10 发看 TTFT / 总耗时 / cached_tokens
是否真的比 A 臂（每轮冷前缀）好。

  .venv/Scripts/python.exe tools/pg_cache_b.py               # 只跑 B 臂 10 发（预热+10）
  .venv/Scripts/python.exe tools/pg_cache_b.py --interleave  # A+B 交错 20 发（推荐，同 A）
产物：%TEMP%\\pg-cache-ab\\report_b.jsonl（或 report_ab.jsonl）

设计与口径见 tools/pg_cache_a.py 顶部说明。
"""

from __future__ import annotations

import sys

from pg_cache_a import main_b

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main_b()
