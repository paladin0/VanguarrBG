# Books & Games extension: approval-gated beta

This extends Vanguarr's existing `/catalogue` page and keeps the original TV/movie routines unchanged. It uses the original Vanguarr LLM client for *advisory* recommendations and an approval queue at `/data/catalogue-review.json`.

## Configuration (set in TrueNAS-managed environment, never Git)

| Variable | Example | Purpose |
| --- | --- | --- |
| `CATALOGUE_LAZYLIBRARIAN_URL` | `http://192.168.4.40:31089` | LazyLibrarian |
| `CATALOGUE_LAZYLIBRARIAN_KEY` | full-access key | LL API |
| `CATALOGUE_RETROARR_URL` | `http://192.168.4.40:30073` | RetroArr |
| `CATALOGUE_RETROARR_KEY` | key | RetroArr API |
| `CATALOGUE_IGDB_CLIENT_ID` | Twitch client ID | IGDB discovery |
| `CATALOGUE_IGDB_CLIENT_SECRET` | Twitch client secret | IGDB discovery |
| `CATALOGUE_WRITE_TOKEN` | long random secret | **Required** to submit, approve, process and request LLM recommendations; browser asks once per tab |
| `CATALOGUE_REVIEW_FILE` | `/data/catalogue-review.json` | queue persistence |
| `CATALOGUE_AUTO_HUNT` | `false` | opt-in background processing of already approved entries |
| `CATALOGUE_HUNT_INTERVAL_MINUTES` | `360` | background polling interval, minimum 15 |

**Permissions:** This extends the existing unauthenticated Vanguarr interface; read/discovery routes must remain on trusted LAN or behind your reverse-proxy authentication. Write operations require `X-Catalogue-Token`. Protect the token in TrueNAS and never publish the dashboard to the open internet without authentication/TLS.

## Processing rules

- Discover authors/books or IGDB games, add items to queue, explicitly approve, then process up to 10 per manual run. Background processing is off by default.
- Books **already present** in LazyLibrarian can be marked Wanted for eBook and/or AudioBook through the documented `getBook` / `queueBook` API. Existing `Have`, `Wanted`, `Snatched` and `Ignored` statuses are untouched. LazyLibrarian's own search schedule handles downloads.
- Authors can be submitted via `addAuthor`. Their bibliography still needs to be retrieved by LazyLibrarian before book queues can be created. Full-library bulk Wanted handling remains dependent on reliable book metadata, not merely author count.
- **Game write operations are disabled**. RetroArr's exact create/monitor API must be verified on the target installed version. Approved game requests show a failed status with an explanatory error, not a silently successful import.
- Recommendations use Vanguarr's existing LLM configuration, return advisory text only and never auto-add titles.
- Queue entries are deduplicated; failed items are not automatically retried. No automatic bulk hunts of 1,000+ authors are initiated.

## Safety / deployment

Test with a separate Vanguarr deployment and a **copy** of `/data`, not the live SQLite database. Configure a new port and restrict to the LAN. Test current TV/movie screens, then test an author, a known LazyLibrarian book, and a game review. Do not replace the live app until results and database backup/rollback are verified. The Huntarr companion integration remains read-only at this stage and is **not** connected to Vanguarr's worker. Do not claim this is a fully operational cross-app scheduling feature yet.

## RetroArr next verification

Use `GET /api/catalogue/games/capabilities` to inspect the installed API documentation (if exposed). If it reports no OpenAPI schema, obtain exact request/response examples from the running RetroArr build before enabling game writes. Do not guess endpoints or bypass the API with database modifications.
