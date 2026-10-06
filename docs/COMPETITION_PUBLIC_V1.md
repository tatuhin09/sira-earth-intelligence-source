# SIRA Competition Public Site

This is a static export, separate from the private owner desktop and the local read-only demo. The export is a captured snapshot. It does not update while the computer is off, and the page labels the capture time and release evidence as historical.

## Private build and audit

From a clean, committed SIRA revision with a passing release acceptance artifact for that same revision:

```bash
cd "$HOME/sira"
source .venv/bin/activate
PYTHONPATH="$PWD/src" python3 -B -P -m sira.competition_export \
  --root "$PWD" \
  --out "$HOME/Downloads/sira-competition-public-site" \
  --archive "$HOME/Downloads/sira-competition-public-site.zip"
```

The exporter checks Git cleanliness and current-revision release evidence, reconstructs a strict field allowlist, scans for private values, and writes exactly five public files: `index.html`, `docs/index.html`, `assets/site.css`, `snapshot.json`, and `_headers`. The ZIP contains exactly those files at its root. It contains no private Python modules, databases, runtime artifacts, credentials, tasks or goal text.

If SIRA changes, repeat release acceptance for the new revision before producing a new export. Review and replace the public deployment with the new static ZIP; the site has no live connection to SIRA.

## Cloudflare Pages Direct Upload

1. Sign in to [Cloudflare](https://dash.cloudflare.com/) and open **Workers & Pages**.
2. Choose **Create application → Get started → Drag and drop your files**. Give the project a public name such as `sira-labs-demo`.
3. Upload **only** `sira-competition-public-site.zip`, after checking its five-file manifest. Do not upload the repository, `.env`, the local demo server, or an entire Downloads folder.
4. Select **Deploy site**. The deployed site receives a `*.pages.dev` HTTPS address. Test its `/`, `/docs/`, and `/snapshot.json` URLs on a second device or network.
5. Confirm the public snapshot's capture timestamp and release count. Share the real URL only after checking the deployed output for private values.

Official guide: https://developers.cloudflare.com/pages/get-started/direct-upload/

Cloudflare Pages serves the uploaded static files independently of the owner's computer. The private owner desktop remains bound to loopback and is never part of the upload. No Git integration or private repository publication is needed. A new deployment requires a fresh manual export and upload.

## Boundaries

- Public content is static and read-only. No runtime control, research/chat mutation endpoint, shell, credential, package installation, promotion or paid-spending authority is exported.
- The capability map preserves current evidence states; it does not convert partial or unverified capability into mastery.
- The public snapshot carries aggregate counts and narrowly scoped descriptions only. Private topics, knowledge text, evidence IDs and filesystem paths remain local.
- Keep the owner desktop and local competition demo on loopback only. Do not tunnel either service to the internet.
