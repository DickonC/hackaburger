# web

Serves the frontend at `https://hackaburger.cch-uk.tech/`.

The host nginx proxies `/` to the container `hackaburger-web` on `127.0.0.1:8095` and `/ingest/` to the backend. The container serves only `web/public/`, which `./publish` fills with copies of `index.html` and `backend.js`. The rest of the repo, including `.env`, is never served.

To update the live page after changing `index.html` or `backend.js`:

```bash
cd ~/hackaburger && git pull && web/publish
```

There's no restart or cache to clear, because the files are served with `Cache-Control: no-cache`.

First-time setup: `cd web && ./publish && docker compose up -d`.
