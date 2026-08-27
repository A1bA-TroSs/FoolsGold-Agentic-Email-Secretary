// Inline strokes so icons inherit --text / --on-accent from the rail state.
const base = {
  width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none',
  stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', strokeLinejoin: 'round',
};

export const InboxIcon = () => (
  <svg {...base}><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></svg>
);
export const StarIcon = () => (
  <svg {...base}><path d="m12 3 2.6 5.6 6 .8-4.4 4.2 1.1 6.1L12 16.9 6.7 19.7l1.1-6.1L3.4 9.4l6-.8z" /></svg>
);
export const SunIcon = () => (
  <svg {...base}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
);
export const GearIcon = () => (
  <svg {...base}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
);
export const RefreshIcon = ({ spinning }) => (
  <svg {...base} width="15" height="15" style={spinning ? { animation: 'spin .9s linear infinite' } : undefined}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" />
  </svg>
);
export const PaperclipIcon = () => (
  <svg {...base} width="12" height="12"><path d="M21.44 11.05 12.25 20.24a5 5 0 0 1-7.07-7.07l8.49-8.49a3.5 3.5 0 0 1 4.95 4.95l-8.49 8.49a2 2 0 0 1-2.83-2.83l7.78-7.78" /></svg>
);

const sm = { ...base, width: 14, height: 14 };

export const PinIcon = () => (
  <svg {...sm}><path d="M12 17v5" /><path d="M9 10.8V4h6v6.8l2.6 3.2a1 1 0 0 1-.8 1.6H7.2a1 1 0 0 1-.8-1.6z" /></svg>
);
export const CheckIcon = () => (
  <svg {...sm}><path d="M20 6 9 17l-5-5" /></svg>
);
export const MuteIcon = () => (
  <svg {...sm}><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M5 11a7 7 0 0 0 14 0" /><path d="m3 3 18 18" /></svg>
);
export const ClockIcon = () => (
  <svg {...sm}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>
);
export const UndoIcon = () => (
  <svg {...sm}><path d="M3 7v6h6" /><path d="M3.5 13a9 9 0 1 0 2.1-6.4L3 9" /></svg>
);
export const MailSyncIcon = () => (
  <span className="icon-sync">
    <svg {...base} width="16" height="16">
      <rect x="2" y="5" width="20" height="14" rx="2" />
      <path d="m2.5 6.5 9.5 7 9.5-7" />
    </svg>
  </span>
);

/* Three pulsing dots. Reads as "thinking", which is what an LLM call is --
   a spinning arrow reads as "fetching", which it is not. */
export const ThinkingIcon = () => (
  <span className="icon-think"><i /><i /><i /></span>
);

/* The briefing view is a checklist, so it gets a checklist. A sun read as a
   brightness control, which is not a thing this app has. */
export const AgendaIcon = () => (
  <svg {...base}>
    <rect x="4" y="3.5" width="16" height="17" rx="2.5" />
    <path d="M8.2 9.2 9.6 10.6 12 8.2" />
    <path d="M8.2 15.2 9.6 16.6 12 14.2" />
    <path d="M14.6 9.4h2.6M14.6 15.4h2.6" />
  </svg>
);

/* Priority = a flag you plant on what matters, clearer than a star (which
   usually means "favourite" in a mail client). */
export const FlagIcon = () => (
  <svg {...base}>
    <path d="M5 21V4.5" />
    <path d="M5 5.2h10.5l-1.6 3.2 1.6 3.2H5z" />
  </svg>
);
