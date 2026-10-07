/**
 * Where a GOG account is signed in to connect it (#128).
 *
 * GOG Galaxy's own sign-in, as Heroic uses it: GOG redirects to a nearly empty
 * page whose address carries the code, and the user pastes back that address
 * or the code. The client id is Galaxy's, public in every install, and has to
 * match `CLIENT_ID` in `backend/src/ludarium/providers/gog.py`, which spends it.
 */
export const GOG_LOGIN_URL =
  'https://auth.gog.com/auth?client_id=46899977096215655' +
  '&redirect_uri=https%3A%2F%2Fembed.gog.com%2Fon_login_success%3Forigin%3Dclient' +
  '&response_type=code&layout=client2'
