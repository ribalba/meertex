// Who is signed in. app.js fills it before the first page renders.

export const session = {
  me: null,
};

export function myTimezone() {
  return session.me?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
