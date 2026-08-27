/* The checkmark drawn inside a checkbox. Shared so the priority list, the
   briefing and the calendar all animate the same stroke. */
export function Tick() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.5 8.5 6.2 12 13.5 4" />
    </svg>
  );
}

export default Tick;
