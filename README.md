<div align="center">

# Marketing Studio

**Ads, Email, SEO and social in one Mac app - with a draft you review before anything goes live.**

![platform](https://img.shields.io/badge/platform-macOS%2013%2B-111827)
![engine](https://img.shields.io/badge/engine-Python%203-3776AB)
![app](https://img.shields.io/badge/app-Swift%20%2F%20WKWebView-F05138)
![network](https://img.shields.io/badge/binds-127.0.0.1%20only-16a34a)

</div>

Marketing Studio works like Google Ads Editor, for Google Ads **and** Meta: every edit you make lands in a
**draft** on its own, and nothing changes in your accounts until you press **Push**, review the tally
("Ad sets edited 2, Ads added 1") and press **Push live**. Every push is logged with an **Undo**.
It also has Email (ML Suite), SEO (WordPress + Search Console) and SoMe (Facebook, Instagram, LinkedIn) areas.

It is a small native Mac app around a local Python engine that only listens on `127.0.0.1`. Your keys
stay on your Mac, in the macOS Keychain.

## Install

1. Download `MarketingStudio-macOS-arm64.zip` from the [latest release](https://github.com/Sivertsen0007/MarketingStudio/releases/latest).
2. Unzip it and move **Marketing Studio.app** to your Applications folder.
3. The app is signed with our own certificate, not Apple's, so the first time macOS will say it can't check it.
   Right-click the app, choose **Open**, then **Open** again. (Or: System Settings > Privacy & Security > Open Anyway.)

Needs macOS 13 or newer on Apple Silicon, and `python3` (it comes with the Xcode Command Line Tools:
`xcode-select --install`).

## Updates

The app checks GitHub once a day. When there is a newer release it shows **Update available - Update now**.
It downloads the release, checks it is exactly the file GitHub published (SHA-256) and that it is signed with
the same certificate as the app you have, swaps it in and restarts. If anything fails, your old version stays.

## First run: Setup

Setup opens on its own the first time (later: the **Setup** button at the top). Fill in what you use:

| Area | What you need |
|---|---|
| **Meta** | An access token with `ads_management` (a system user token is best), the ad account id (`act_...`), and optionally the Page id. |
| **Google Ads** | The developer token and the manager (MCC) id from your admin, plus the OAuth client id + secret (a Google Cloud OAuth client of type *Desktop app*). Save, then press **Sign in with Google**. Your Google user needs access to the Ads accounts. |
| **Email** | ML Suite URL and tokens (optional). |
| **SEO** | A WordPress application password (optional). Search Console + GA4 use your Google sign-in. |
| **LinkedIn** | Optional. |

**Have your keys in a .env file?** Use *Import from a .env file* in Setup: Studio saves every key it knows
(`META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `GADS_DEVELOPER_TOKEN`, `GADS_LOGIN_CUSTOMER_ID`, `GADS_OPERATING_CUSTOMER_ID`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_FILE=<path to json>`,
`MLSUITE_*`, `WP_STUDIO_*`, `LINKEDIN_*`) and ignores the rest.

Secrets go into the macOS Keychain (service "Marketing Studio"). Everything else Studio keeps - drafts, push
history, caches, settings - lives in `~/Library/Application Support/MarketingStudio/`.

The Google Ads Editor parts (Send to Editor, reading Editor's data) need Google Ads Editor installed on the same
Mac. Everything else talks to the Google Ads and Meta APIs directly.

## Use with Claude Code (MCP)

Studio ships an MCP server, so Claude Code in your terminal works on the same accounts and the same draft as the
app. Connect it once (Setup shows the exact line):

```sh
claude mcp add marketing-studio -s user -- python3 "/Applications/Marketing Studio.app/Contents/Resources/engine/mcp_server.py"
```

Claude can then read campaigns, ad sets, ads, keywords and stats, add and remove draft changes (they appear live in
the app), check the draft, preview a live push, read push history, work on Email/SEO/SoMe drafts, and set Studio up
from a .env file (`setup_import_env_file`) without the values ever passing through the chat. Claude cannot push
live, send, publish or undo: those stay your buttons in the app.

## How it works

```
Marketing Studio.app
  Contents/MacOS/MarketingStudio      Swift: window (WKWebView), menus, starts/stops the engine
  Contents/Resources/engine/          Python (stdlib only): server.py on 127.0.0.1:4830
      server.py        HTTP API + the page
      core.py, gae.py  drafts, Google Ads Editor database
      live.py, gapi.py Google Ads API (preview with validateOnly, push, undo)
      meta_ads.py      Meta Marketing API (preview, push one change at a time, undo)
      areas/           Email, SEO, SoMe, Activity
      appconfig.py     where data and keys live (Keychain), Sign in with Google
      updater.py       in-app updates from GitHub Releases
```

Safety rails: every POST needs a per-process secret that only the page gets; the engine refuses other hosts and
origins; new campaigns, ad sets and ads are always created **paused**; removals and spend increases need an extra
tick before Push live.

## Develop

```sh
cd engine && python3 server.py            # run from source on http://localhost:4830
python3 tests/test_email.py               # tests: plain python3, no pytest needed (each file runs on its own)
cd macapp && ./build.sh --install         # build the app and put it in ~/Applications
./release.sh patch --notes notes.md       # cut a release (bumps VERSION, builds, signs, tags, publishes)
```

Releases must be signed on the Mac that holds the signing certificate: installed copies only accept updates
signed by the same certificate as themselves.
