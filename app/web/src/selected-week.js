export const DEMO_MONDAYS = ["2026-04-27", "2026-05-04"];
export const DEFAULT_WEEK = DEMO_MONDAYS[0];

const STORAGE_KEY = "petfolk.selectedMonday";

function isDemoMonday(value) {
  return DEMO_MONDAYS.includes(value);
}

export function selectedWeek(requested) {
  if (isDemoMonday(requested)) return requested;
  try {
    const remembered = window.localStorage.getItem(STORAGE_KEY);
    if (isDemoMonday(remembered)) return remembered;
  } catch {
    // Storage can be unavailable in a private or sandboxed browser.
  }
  return DEFAULT_WEEK;
}

export function rememberWeek(week) {
  if (!isDemoMonday(week)) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, week);
  } catch {
    // The URL remains the source of truth when storage is unavailable.
  }
}
