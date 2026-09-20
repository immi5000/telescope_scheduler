/**
 * The figures the form starts from, in one place because more than one file
 * has to agree about them.
 *
 * These are the SERVER's defaults as well (`SessionRequest` and
 * `TonightRequest` in `api/schemas.py`, `SiteRequest.bortle`), so a night
 * planned without touching either field is the night the API would have
 * planned from the same request. They live here rather than in `Setup.tsx`
 * because the target picker needs the SNR goal too, and importing it back
 * out of its own parent would be a cycle.
 */

/** Per star-sized patch of the target's surface, at its mean brightness. */
export const DEFAULT_SNR = 15

/** What a sky nobody has measured is assumed to be: an ordinary suburb. */
export const DEFAULT_BORTLE = 5
