/* The checkmark drawn inside a checkbox. Shared so the priority list, the
   briefing and the calendar all animate the same stroke.

   `spark` adds three shards of pyrite thrown outward, once, on the tick. It is
   the only reward gesture in the app and it lives here so every surface that
   ticks something off gets the same one -- a list, a briefing row and a
   calendar item celebrating differently would read as three apps.

   Mounted only while `on` is true and removed when the animation is done, so
   nothing is left animating in the DOM behind a ticked row. */
import { useEffect, useState } from 'react';

export function Tick({ spark = false, on = false }) {
  const [burst, setBurst] = useState(false);

  useEffect(() => {
    if (!spark || !on) return undefined;
    setBurst(true);
    const timer = setTimeout(() => setBurst(false), 520);
    return () => clearTimeout(timer);
  }, [spark, on]);

  return (
    <>
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="M2.5 8.5 6.2 12 13.5 4" />
      </svg>
      {burst && (
        <span className="tick-spark" aria-hidden="true">
          <i /><i /><i />
        </span>
      )}
    </>
  );
}

export default Tick;
