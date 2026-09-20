/**
 * The two places a fold is watched from.
 *
 * `FoldBar` goes where the click was, which for creating a night is the foot
 * of the setup sheet, next to the button that started it.
 *
 * `FoldBanner` goes over the sky, for the two re-plans that happen with the
 * night already on screen -- adding an object, and folding again against the
 * current forecast. It is not in the object card that starts the add, because
 * that card can be closed, the selection changed or the panels hidden while
 * the re-plan runs on, and a wait whose only sign disappears when you look
 * away is a wait that reads as nothing happening.
 *
 * Both subscribe to the store themselves rather than take a fraction as a
 * prop. They tick four times a second, and neither the setup sheet nor the
 * app shell should re-render for that.
 */

import { useFoldProgress } from '../plan/foldProgress'

/**
 * A meter, with the stage under it.
 *
 * `progressbar` rather than a bare div: a sighted reader gets two facts here
 * -- how far, and what of -- and a screen reader should get the same two.
 *
 * WHAT IS AND IS NOT ANNOUNCED. The whole bar is a polite live region, so a
 * new stage is read out as it lands: those arrive a handful of times per fold
 * and each one is worth hearing. The PERCENTAGE is `aria-hidden`, and that is
 * load-bearing rather than tidy-minded -- it changes four times a second, and
 * inside a live region that is not a progress report, it is a screen reader
 * talking over itself for the length of the wait. The number is still exposed,
 * on `aria-valuenow`, where assistive technology reads it on demand instead of
 * being told. `aria-valuetext` carries the stage for the same reason: it
 * changes when the stage does, not when the bar moves.
 */
export function FoldBar({ heading, label }: { heading?: string; label?: string }) {
  const job = useFoldProgress()
  if (job.kind === null) return null
  const percent = Math.round(job.fraction * 100)
  return (
    <div className="foldbar" role="status" aria-live="polite">
      {heading && <div className="foldbar__heading">{heading}</div>}
      <div
        className="meter"
        role="progressbar"
        aria-label={label ?? heading ?? 'Planning the night'}
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={job.message}
      >
        <div className="meter__fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="foldbar__stage">
        <span className="foldbar__msg">{job.message}</span>
        <span className="foldbar__pct num" aria-hidden>
          {percent}%
        </span>
      </div>
    </div>
  )
}

/**
 * The same wait, over the sky, for an add only.
 *
 * Creating a night has the setup sheet in front of it and needs no second
 * copy; an add has nothing guaranteed to still be on screen. The heading is
 * carried here because the first half of an add is the ordinary fold -- the
 * night is planned WITHOUT the object before the object is added to it -- so
 * those stages say "solved 2/4" and nothing about what was asked for.
 */
export function FoldBanner() {
  const job = useFoldProgress()
  // Not `create`: that one happens behind the setup sheet, which has its own
  // bar and is the only thing on screen.
  if (job.kind === null || job.kind === 'create') return null
  const heading = job.subject
    ? `Re-planning the night around ${job.subject}`
    : 'Re-planning the night against the current forecast'
  return (
    <div className="banner foldbanner">
      {/* The live region is the bar's own -- nesting a second one here would
          announce every stage twice. */}
      <FoldBar heading={heading} />
    </div>
  )
}
