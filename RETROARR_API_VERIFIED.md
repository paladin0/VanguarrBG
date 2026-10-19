# RetroArr API integration (from uploaded RetroArr-main.zip)

Routes used by the Vanguarr catalogue worker:

- `GET /api/v3/platform/by-igdb/{id}` maps IGDB platform ID to the distinct RetroArr platform ID.
- `GET /api/v3/game/lookup/igdb/{id}` obtains a Game payload from RetroArr's metadata provider.
- `GET /api/v3/game` checks existing games for a duplicate IGDB ID + platform.
- `POST /api/v3/game` creates a game; expects a JSON Game object (title, platformId, igdbId, etc). Returns HTTP 201 with created ID; HTTP 409 on duplicate.
- `PUT /api/v3/games/{id}/monitored` sets `{"monitored":true}`.
- `POST /api/v3/games/{id}/search-now?autoDispatch=false` performs a search without automatically sending a release. Not called by this patch.

All are protected with `X-Api-Key`. Configure `CATALOGUE_RETROARR_URL` and `CATALOGUE_RETROARR_KEY` and explicitly set `CATALOGUE_ENABLE_GAME_IMPORT=true` only after verifying the connected test instance. Approved requests are queued, not automatically submitted until a worker runs. A 409 duplicate title needs operator review instead of blindly overwriting data. A partial failure after creation may leave an unmonitored game; the next run checks existing game IDs before adding.

**Important:** this patch implements approval-to-game-registration and monitoring in Vanguarr. It does **not** add a native Huntarr2 scheduler, nor automatically trigger game downloads. There is no claim of end-to-end testing with your live RetroArr build.
