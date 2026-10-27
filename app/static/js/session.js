// Who is signed in. app.js fills it before the first page renders.

export const session = {
  me: null,
};

// A page that changes the account's name or picture calls meChanged(), so
// the header shows the new one.
const meListeners = new Set();

export function onMeChanged(fn) {
  meListeners.add(fn);
}

export function meChanged() {
  for (const fn of meListeners) fn(session.me);
}

export function myTimezone() {
  return session.me?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
