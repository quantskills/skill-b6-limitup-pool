# Portable Loader Prompt / 便携加载提示

This file is the portable entrypoint for Hermes, OpenClaw, and any agent runtime that does not
natively discover `SKILL.md` folders. Native runtimes (Claude Code, Codex, Cursor) should read
[SKILL.md](../SKILL.md) directly; the Cursor rule is [cursor-rule.mdc](cursor-rule.mdc) and the
Codex interface is [openai.yaml](openai.yaml).

本文件是 Hermes / OpenClaw 等不原生识别 `SKILL.md` 目录的运行时的便携入口。
把下面这段提示粘贴进运行时的系统提示或工具描述，并把占位路径替换成仓库根目录。

```text
You have access to a local skill named skill-b6-limitup-pool at:
<SKILL_B6_LIMITUP_POOL_ROOT>

Use it when the user wants to maintain or query the post-market A-share limit-up pool: first boards, streak counts, blow-up counts, re-seal times, theme groups, special patterns, and sentiment metrics such as promotion and blow-up rates.

1. Read <SKILL_B6_LIMITUP_POOL_ROOT>/SKILL.md first; it is the canonical declaration.
2. Read <SKILL_B6_LIMITUP_POOL_ROOT>/开发产物/SKILL.md for the agent-facing interface (inputs, outputs, run()/validate_input()).
3. Read the references before touching data logic:
   - <SKILL_B6_LIMITUP_POOL_ROOT>/开发产物/references/api_guide.md
4. Run entrypoints from <SKILL_B6_LIMITUP_POOL_ROOT>:
   python 开发产物/scripts/build.py --mode daily
   python 开发产物/scripts/build.py --mode backfill --start 20250101 --end 20250630
   python 开发产物/scripts/render_html.py
5. Limit-up thresholds follow exchange rules by board (10% main board / 20% ChiNext & STAR / 5% ST / 30% BSE); never use one flat threshold
6. Blow-up and re-seal marks derived from daily bars are proxies; label them as daily-proxy and never present them as minute-level facts
7. Parquet output is keyed by `trade_date`/`build_id`/`target_id`/`result_type`; after re-running a trade date, check for leftover stale rows of that day (known issue, see repository issue #2)
8. Credentials come only from PANDA_USERNAME / PANDA_PASSWORD or ~/.pandadata/pandadata.env; never hard-code or print them.
9. This is a QuantSkills Community Project for research and education only: no investment advice, no return promises, no claim of official endorsement.
```
