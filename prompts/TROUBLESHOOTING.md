# Dealerly — troubleshooting

## Blank gray window: “Claude” desktop app (Windows)

The **Anthropic Claude** desktop client is a separate app (Electron / WebView2). A **empty dark window** usually means the embedded browser failed to load.

1. Quit Claude completely (system tray) → reopen.
2. **Update** Claude to the latest build.
3. Install or repair **Microsoft Edge WebView2 Runtime** from Microsoft.
4. Temporarily disable **VPN** or allow Claude through **Windows Firewall**.
5. **Reinstall** Claude; in app settings, try turning off **GPU / hardware acceleration** if available.
6. Use **https://claude.ai** in Chrome/Edge or your IDE’s built-in AI while debugging.

This is **unrelated** to `python -m dealerly.cli`. Dealerly talks to APIs via `dealerly/.env`, not the Claude desktop UI.

---

## Dealerly: offer AI unavailable

- **Claude path:** `CLAUDE_API_KEY` must be set (starts with `sk-ant-` for API keys).
- **OpenAI path:** `OPENAI_API_KEY` + default `OPENAI_BASE_URL`, or **local** Ollama: `OPENAI_BASE_URL=http://127.0.0.1:11434/v1` and `OPENAI_MODEL=...` (key optional on localhost).
- **Disable AI offers:** `DEALERLY_AI_BACKEND=none` or turn off offer generation in setup.
- See `README.md` → *Efficiency & local AI*.

---

## Dealerly: ANPR / Plate Recognizer

- Token: `PLATE_RECOGNIZER_TOKEN` in `dealerly/.env`.
- **Quota:** tune `DEALERLY_ANPR_MAX_IMAGES` and `DEALERLY_ANPR_MIN_PROFIT_GBP` (see `README.md`).

---

## Obsidian vault not updating

- Check pipeline log for `[Obsidian] Vault: ...` — path must exist.
- Override: `DEALERLY_OBSIDIAN_VAULT` in `dealerly/.env`.
- Vault entry doc: `Dealerly_Vault/_Dealerly_Brain.md` (local machine only).
