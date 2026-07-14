# k-hermes changelist (vs upstream Hermes / NousResearch hermes-agent)

This fork tracks changes that diverge from upstream Hermes Agent.
Upstream PRs are intentionally not opened for these items.

## Browser automation

### CloakBrowser is the default local browser path
- Default `browser_*` sessions start local **CloakBrowser** (`cloakserve` CDP) instead of stock Playwright Chromium / agent-browser alone.
- Explicit CDP override still wins: `BROWSER_CDP_URL`, `browser.cdp_url`, `/browser connect`.
- Runtime helper: `tools/cloakbrowser_runtime.py`.
- Default launch is **headed** (visible window) so operators can watch sessions; opt into headless with `CLOAKBROWSER_HEADLESS=1`.

### Browser Use cloud provider removed
- Deleted in-tree plugin: `plugins/browser/browser_use/`.
- Removed auto-detect preference for Browser Use (`BROWSER_USE_API_KEY` / Nous managed browser-use gateway no longer selects a cloud browser).
- Removed setup/picker UX for “Nous Subscription (Browser Use cloud)”.
- Removed `BROWSER_USE_API_KEY` from optional env catalog / status key list.
- Legacy registry walk is now **browserbase-only** (Firecrawl remains explicit-config only).
- Explicit `browser.cloud_provider: browser-use` is treated as **unavailable** (not remapped silently to another cloud vendor).
- Local CloakBrowser remains the recommended path for bot-detection / CAPTCHA-avoidance work.

### Remaining browser backends (after this change)
| Backend | Status in k-hermes |
|---------|--------------------|
| CloakBrowser (default local) | **Primary** |
| CDP override (`/browser connect`) | Supported |
| Camofox (local Firefox/Camoufox REST) | Supported optional |
| Browserbase cloud | Supported optional |
| Firecrawl cloud browser | Supported explicit-only |
| Browser Use cloud | **Removed** |

## Direct desktop
- Encrypted media plane + no-strip route work under `agent/direct_desktop_*` and related session/state paths (see recent commits on `main`).

## Policy notes
- Do **not** open upstream PRs for CloakBrowser-defaulting or Browser Use removal; these are fork product decisions.
- Portal entitlement category name `browser-use` may still appear in JWT/tool-coverage enums (upstream Nous Portal schema). k-hermes no longer routes runtime browser sessions through that vendor.
