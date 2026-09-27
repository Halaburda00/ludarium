/**
 * Where an Epic account is signed in to connect it (#64).
 *
 * The Epic Games Launcher's own sign-in, as legendary uses it: the page Epic
 * redirects to shows an authorization code, which the user pastes back. The
 * client id is the launcher's, public in every install, and has to match
 * `CLIENT_ID` in `backend/src/ludarium/providers/epic.py`, which spends the code.
 */
export const EPIC_LOGIN_URL =
  'https://www.epicgames.com/id/login?redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi' +
  '%2Fredirect%3FclientId%3D34a02cf8f4414e29b15921876da36f9a%26responseType%3Dcode'
