# Vanguarr → Huntarr book/game workflow

Set the existing catalogue credentials: `CATALOGUE_LAZYLIBRARIAN_URL`, `CATALOGUE_LAZYLIBRARIAN_KEY`, `CATALOGUE_RETROARR_URL`, `CATALOGUE_RETROARR_KEY`, `CATALOGUE_IGDB_CLIENT_ID`, and `CATALOGUE_IGDB_CLIENT_SECRET`.

Set:
- `CATALOGUE_HUNTARR_URL=http://huntarr-address:9706`
- `CATALOGUE_HUNTARR_TOKEN=<same value as Huntarr MEDIA_HUNT_TOKEN>`
- `CATALOGUE_WRITE_TOKEN=<separate long random token>`
- `CATALOGUE_ENABLE_GAME_IMPORT=true` once tested against an isolated RetroArr instance.
- `CATALOGUE_AUTO_HUNT=false` for manual testing, then true for scheduled processing of approved requests.
- `CATALOGUE_HUNT_INTERVAL_MINUTES=360` for approved-request processing (not search scheduling).

Vanguarr discovery → review → explicit approval → process approved:
- books: mark known, missing formats Wanted in LazyLibrarian, then enqueue search in Huntarr;
- games: resolve IGDB metadata and platform ID via RetroArr, create/monitor if absent, then enqueue search in Huntarr;
- author requests: add author only. LazyLibrarian metadata must populate books before they can be requested. Bulk-author book-status automation is **not** implied by author creation.

Huntarr handles later searches and cooldowns. Vanguarr does not trigger duplicate searches. Game search dispatch is off unless `MEDIA_HUNT_GAME_AUTO_DISPATCH=true` in Huntarr. Confirm paths/authentication and test one book and one game before activating a full catalogue.
