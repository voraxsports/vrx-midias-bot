# vrx-midias-bot

Automação do canal **#midias-vrx** (Discord).
Posta embeds quando sair:
- **Vídeo novo no YouTube** (RSS do canal)
- **Post novo no Instagram** (Graph API — opcional)

## Como funciona
- Roda via **GitHub Actions** (cron + manual).
- Usa `state.json` versionado pra não duplicar post.
- Por padrão, no **primeiro run** ele só sincroniza estado (não posta histórico).

## Secrets (GitHub)
Repo → Settings → Secrets and variables → Actions

Obrigatórios:
- `DISCORD_WEBHOOK_URL` → webhook do canal `#midias-vrx`
- `YT_FEED_URL` → `https://www.youtube.com/feeds/videos.xml?channel_id=UCk3OtKKa0EeQmWjsTzosaEg`

Opcionais (Instagram Graph API):
- `IG_USER_ID`
- `IG_ACCESS_TOKEN`
- `IG_GRAPH_VERSION` (ex: `v20.0`)

## Teste rápido
1) Configure os Secrets
2) Actions → **VRX Midias Bot** → **Run workflow**
3) Se quiser forçar post no primeiro run: ajuste `ALLOW_INITIAL_POST=1` no workflow (temporário).

## Links
- YouTube: https://youtube.com/@voraxsports
- Instagram: https://www.instagram.com/voraxsportss/
