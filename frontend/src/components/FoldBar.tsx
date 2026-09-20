/**
 * The two places a fold is watched from.
 *
 * `FoldBar` goes where the click was -- under Create Schedule, under Add to
 * schedule -- because that is where the answer is being waited for.
 *
 * `FoldBanner` goes over the sky, and exists because the card the add was
 * started from can be closed, the selection changed, or the panels hidden,
 * while the re-plan runs for several more seconds. A wait whose only sign
 * disappears when you look away is a wait that reads as nothing happening.
 *
 * Both subscribe to the store themselves rather than take a fraction as a
 * prop. They tick four times a second, and neither the setup sheet nor the
 * object card nor the app shell should re-render for that.
 */

import { useFoldProgress } from '../plan/foldProgress'

/**
 * A meter, with the stage under it.
 *
 * `progressbar` rather than a bare div: a sighted reader gets two facts from
 * this -- how far, and what of -- and a screen reader should get the same two,
 * which is what `aria-valuetext` carries.
 */
export function FoldBar({ heading, label }: { heading?: string; label?: string }) {
  const job = useFoldProgress()
  if (job.kind === null) return null
  const percent = Math.round(job.fraction * 100)
  return (
    <div className="foldbar">
      {heading && <div className="foldbar__heading">{heading}</div>}
      <div
        className="meter"
        role="progressbar"
        aria-label={label ?? heading ?? 'Planning the night'}
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={`${percent}% — ${job.message}`}
      >
        <div className="meter__fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="foldbar__stage">
        <span className="foldbar__msg">{job.message}</span>
        <span className="foldbar__pct num">{percent}%</span>
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
    <div className="banner foldbanner" role="status" aria-live="polite">
      <FoldBar heading={heading} />
    </div>
  )
}
