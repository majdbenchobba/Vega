# Vega

Vega is a local Windows companion with a private-by-default workspace. This
source release contains no personal projects, provider credentials, local
settings, account IDs, or downloaded model runtimes.

## First run

1. Install Python 3.13 or newer and Node.js 24.12 or newer.
2. Extract Vega to a folder your Windows account can write to. Avoid
   `C:\Program Files`, since Vega keeps its local configuration beside the app.
3. Open PowerShell in the extracted folder and run `.\Initialize-Vega.ps1`.
   It creates private configuration from the examples, a Vega-only Python
   environment, and installs the pinned Python and Node dependencies. Existing
   settings are never overwritten.
4. Install and configure OmniRoute separately with your own provider accounts.
   The example routes through `auto/best-free` at `127.0.0.1:20128`.
5. Run `.\Install-OmniRoute-Startup.ps1`, then `.\Install-Startup.ps1`.
   Open `http://127.0.0.1:8769/`.

The default model route is dynamic. Provider and free-model availability can
change; neither a free response nor an always-on fallback is guaranteed. Vega
does not include provider credentials or personal project routes.

## Privacy and data

- New installs start with an empty project registry. Connect only projects you
  choose.
- The default writable workspace is `%LOCALAPPDATA%\Vega\workspace`.
- `settings.json`, `tasks.json`, local catalogs, reviewed document
  authorizations, reports, media, model downloads, and runtime state are
  user-local files and should not be shared.
- Optional Windows app aliases and Mini App brand image names live in
  `settings.json`; both settings default to empty. Brand images are local
  files under `public\miniapp\brands` and are excluded from this release.
- Cloud speech services require your own credentials and may incur charges.
- Voice-note decoding uses an FFmpeg executable available on `PATH`. Vega does
  not bundle FFmpeg; install it separately and review its license.
- Optional speaker analysis requires explicitly running
  `.\setup_speaker_runtime.py --provision`. Model and dependency attributions
  are in `speaker-models.json` and `third-party\speaker-models`.

## Development

Run the focused Python regression tests with:

```powershell
python -m unittest test_conversation
```

Rebuild the UI after installing dependencies with `npm ci`:

```powershell
npm run build
```

See `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_SBOM.cdx.json` for dependency
attributions and the resolved Node dependency inventory.
