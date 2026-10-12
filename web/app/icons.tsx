// Presentational orb icons, keyed off the call state.

export function MicIcon() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z" fill="currentColor" />
      <path
        d="M19 11a1 1 0 1 0-2 0 5 5 0 0 1-10 0 1 1 0 1 0-2 0 7 7 0 0 0 6 6.92V21a1 1 0 1 0 2 0v-3.08A7 7 0 0 0 19 11Z"
        fill="currentColor"
      />
    </svg>
  );
}

export function StopIcon() {
  return (
    <svg width="30" height="30" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor" />
    </svg>
  );
}

export function WaveIcon() {
  return (
    <svg width="40" height="40" viewBox="0 0 40 40" fill="none" aria-hidden="true" className="wave">
      {[0, 1, 2, 3, 4].map((i) => (
        <rect key={i} x={6 + i * 6} y="14" width="3" height="12" rx="1.5" fill="currentColor" />
      ))}
    </svg>
  );
}

export function Spinner() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" className="spin" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

export function MutedMicIcon({ muted = false }: { muted?: boolean }) {
  return (
    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z" fill="currentColor" />
      <path d="M19 11a1 1 0 1 0-2 0 5 5 0 0 1-10 0 1 1 0 1 0-2 0 7 7 0 0 0 6 6.92V21a1 1 0 1 0 2 0v-3.08A7 7 0 0 0 19 11Z" fill="currentColor" />
      {muted && <path d="M4 4 20 20" stroke="#071120" strokeWidth="2.4" strokeLinecap="round" />}
    </svg>
  );
}

export function PhoneOffIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M5.1 15.4c4.4-3.2 9.4-3.2 13.8 0l-2.1 3.2-3.2-1.4v-2.1a11.2 11.2 0 0 0-3.2 0v2.1l-3.2 1.4-2.1-3.2Z"
        fill="currentColor"
      />
    </svg>
  );
}
