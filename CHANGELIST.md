# k-hermes changelist (vs upstream Hermes / NousResearch hermes-agent)

This fork tracks changes that diverge from upstream Hermes Agent.
Upstream PRs are intentionally not opened for these items.

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

## System prompt (돌쇠 / Dolshoi identity)

### Product identity rewrite
- Default agent identity is **돌쇠 (Dolshoi)**, not Hermes Agent / Nous Research.
- Purpose: reduce everyday friction for people in Korea; concise, action-first; pursue user requests until done or a real blocker is reported.
- `DEFAULT_AGENT_IDENTITY` in `agent/prompt_builder.py`; user `SOUL.md` should match when present.

### Hermes self-help surface removed
- Removed Hermes docs / `hermes-agent` skill steering from the system prompt.
- Added `DOLSHOI_PRODUCT_BOUNDARY`: do not answer Hermes/hermes-agent/Nous product questions; do not load or recommend `hermes-agent` skills.
- Skills index blocks `hermes-agent`, `hermes-agent-dev`, `hermes-agent-operations` via `_BLOCKED_SYSTEM_PROMPT_SKILLS`.

### CloakBrowser mandatory in prompt
- When any `browser_*` tool is loaded, inject `CLOAKBROWSER_GUIDANCE` requiring CloakBrowser-backed `browser_*` tools (no silent Chrome/cloud/CDP substitution unless the user explicitly `/browser connect`).

### Kanban prompt guidance disabled
- `KANBAN_GUIDANCE` is empty and is **not** injected into the system prompt (normal chat or kanban worker).

### Platform hints for desktop IPC
- Removed platform hints: `tui`, `sms`, `email`, `api_server`.
- Added/updated `desktop` (and mapped historical `platform="tui"` → desktop hint): absolute filesystem paths as plain text for file/link rendering; do not emit `MEDIA:` tags on desktop.
- `cli` / `webui` also steer absolute plain-text paths for file handoff.

### Single-profile product
- Multi-profile system-prompt warnings (`Active Hermes profile…`, cross-profile write hints) removed. One profile surface only.
