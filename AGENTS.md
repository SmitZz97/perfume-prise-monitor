# Codex: three separate responsibilities

This repo is for a Russian-language Telegram perfume shop. Preserve existing Windows/Tkinter workflows, manual price attestations, and user data.

## Agent 1 — retail competitors

- Entry point: `agent_competitors.py`; sources: goldapple.ru, randewoo.ru, letu.ru, rivegauche.ru only.
- Reuse `discover_perfumes.py`, `monitor_core.py`, `hybrid_store.py`, `browser_access.py`; never access Delice Opt or supplier credentials.
- No inferred prices, CAPTCHA/robots bypasses, tester-vs-regular substitution, or silent TTL refreshes.
- Owns the independent `competitors.json` snapshot.

## Agent 2 — supplier

- Entry point: `agent_supplier.py`; source: application.delice-opt.ru only.
- Read current row-level offers from the user's separately authorized local Chromium profile; never reuse remembered prices as current.
- Validate full brand/fragrance, EDP/EDT/Extrait/Parfum, size, kit/tester status and an unambiguous price. Report every variant and failed match.
- Never read competitors' snapshots, send credentials to GitHub, or silently authenticate.
- Owns `supplier.json`.

## Agent 3 — pricing

- Entry point: `agent_pricing.py`; **no network or browser access**.
- Read exact matching outputs from Agents 1 and 2 using `agent_protocol.py`.
- Reject prices without verified identity, missing supplier price, future/naive timestamps, prices >=24 hours old, unavailable or third-party retail offers, and conditional card/loyalty prices.
- Use `pricing_recommendation.calculate`; floor >=15% markup after entered expenses, not 15% margin. Without reliable retailers show only a cost floor, not a market optimum.
- Owns the final `recommendations.xlsx` and its diagnostics.

## Orchestration and protection

- `run_three_agents.py` launches each worker in its own process; a failed step stops the pipeline.
- Never commit `.env`, `supplier_links.json`, `supplier_browser_profile`, `local_data`, users' Excel files, cookies, tokens, or other local state. Follow `.gitignore`.
- Keep the old Windows interface functional; test with `python -m pytest tests -q` where browsers are permitted, and use the no-browser test files where Chromium is restricted.
- Do not claim a live price until it has been confirmed by the running site/profile or an explicit current human attestation.
