# k-hermes changelist (vs upstream Hermes / NousResearch hermes-agent)

This fork tracks changes that diverge from upstream Hermes Agent.
Upstream PRs are intentionally not opened for these items.

## Product identity (locked)

- **Package-owned persona**: k-hermes identity is `돌쇠 (Dolshoi)`, defined in `hermes_cli/product_identity.py`.
- **Not user-overridable**: with `PRODUCT_IDENTITY_LOCKED=True`, runtime ignores `$HERMES_HOME/SOUL.md` for system-prompt identity and rewrites that file to the package constant on `ensure_hermes_home()`.
- **Why**: Dolshoi desktop isolates `HERMES_HOME` to `~/.dolshoi/hermes-profile`. Upstream Hermes treated SOUL.md as personalization; product voice must not depend on a mutable profile file or a personal `~/.hermes` install.
- Upstream PRs are not opened for this product lock.

## Browser automation

### CloakBrowser is the default local browser path
- Default `browser_*` sessions start local **CloakBrowser** (`cloakserve` CDP) instead of stock Playwright Chromium / agent-browser alone.
- Runtime helper: `tools/cloakbrowser_runtime.py`.
- Default launch is **headed** (visible window); opt into headless with `CLOAKBROWSER_HEADLESS=1`.

### CDP override is NOT CloakBrowser fallback
- `/browser connect`, `BROWSER_CDP_URL`, and `browser.cdp_url` attach browser tools to a **user-supplied Chromium DevTools endpoint**.
- Typical uses: drive a real Chrome/Brave window you already opened, reuse a debug profile/cookies/login, or attach to a remote Chromium CDP port.
- When set, this **wins over** CloakBrowser auto-launch for that process (disconnect / unset reverts to CloakBrowser default).

### Removed browser backends
- **Browser Use** cloud plugin removed (`plugins/browser/browser_use/`).
- **Camofox** local Firefox/REST path removed (`tools/browser_camofox*.py`).
- **Browserbase** cloud browser plugin removed (`plugins/browser/browserbase/`).
- **Firecrawl cloud browser** plugin removed (`plugins/browser/firecrawl/`).
  - Note: Firecrawl **web search/extract** (`plugins/web/firecrawl/`, `FIRECRAWL_API_KEY`) remains for `web_search` / `web_extract`. Only the browser-session backend is gone.

### Remaining browser backends
| Backend | Status in k-hermes |
|---------|--------------------|
| CloakBrowser (default local) | **Primary** |
| CDP override (`/browser connect`) | Supported (manual attach) |
| Third-party browser plugins | Optional (`~/.hermes/plugins/browser/`) |
| Browserbase / Firecrawl browser / Browser Use / Camofox | **Removed** |

## Direct desktop
- Encrypted media plane + no-strip route work under `agent/direct_desktop_*` and related session/state paths (see recent commits on `main`).

## Policy notes
- Do **not** open upstream PRs for these browser backend product decisions.
- Portal JWT coverage category names such as `browser-use` / `firecrawl` may still appear as entitlement enums; k-hermes does not route `browser_*` sessions through removed vendors.

## Skills surface (k-hermes pruning)

### Removed as out-of-scope for k-hermes
- **`skills/yuanbao`** — Yuanbao group-ops skill removed from the bundled skill library.
  - Note: the **Yuanbao messaging platform adapter** (`gateway/platforms/yuanbao*`) is unchanged; only the skill package is gone.
- **`skills/data-science/jupyter-live-kernel`** — Jupyter live-kernel skill removed (entire `skills/data-science/` category).
- **All mlops skills** — both bundled (`skills/mlops/`) and optional (`optional-skills/mlops/`) trees deleted.
  - Bundled removed: huggingface-hub, llama-cpp, vllm, lm-evaluation-harness, weights-and-biases, audiocraft, segment-anything.
  - Optional removed: axolotl, trl/unsloth, flash-attention, peft, outlines, dspy, whisper, vector DBs, GPU cloud skills, etc.

### Docs / catalog updates
- Website skill docs, sidebars, and EN/zh-Hans skill catalogs no longer list the removed skills.
- Skill-authoring category list updated accordingly.
