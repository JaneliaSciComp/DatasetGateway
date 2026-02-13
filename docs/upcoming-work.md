# DatasetGate — Upcoming Work

Improvements needed before DatasetGate is ready for production use and
integration with external apps like celltyping-light.

## 1. Landing Page UX

**Problem:** After OAuth login, the callback hardcodes a redirect to
`/web/datasets`. This breaks the Neuroglancer popup flow, which expects the
user to land on `/login` (the "you may close this window" page). Meanwhile,
browser-based logins from the root page need to reach the dashboard.

**Fix — `next` parameter on OAuth flow:**

- `AuthLoginView.get()` reads `?next=` from the query string and stores it
  in `request.session["oauth_next"]`.
- `AuthCallbackView.get()` redirects to
  `request.session.pop("oauth_next", "/login")` instead of hardcoding
  `/web/datasets`.
- The root page (`ngauth/index.html`) login link becomes
  `/auth/login?next=/web/datasets`, so browser users land on the dashboard.
- Neuroglancer's popup flow continues to call `/auth/login` with no `next`
  param, so it falls through to `/login` as before.

**Files:**
- `ngauth/views.py` — `AuthLoginView`, `AuthCallbackView`
- `ngauth/templates/ngauth/index.html` — login link

## 2. `make_admin` Management Command

**Problem:** Bootstrapping the first admin requires creating a Django
superuser, navigating to `/admin/`, finding the `core.User` record, and
checking the `admin` checkbox. This is error-prone and undocumented.

**Fix:** A one-liner management command:

```
python manage.py make_admin user@example.com
```

Looks up the `core.User` by email, sets `admin=True`, prints confirmation.
The user must have logged in via OAuth first (to create the record).

**File:** `core/management/commands/make_admin.py`

## 3. Cross-Subdomain Auth Cookie

**Problem:** When DatasetGate and an external app (e.g., celltyping-light)
live on sibling subdomains (`auth.example.org` / `app.example.org`), the
auth cookie is scoped to the issuing host by default and won't be sent to
the sibling.

**Fix:** Add `AUTH_COOKIE_DOMAIN` setting (env var, default empty). When
set (e.g., `.example.org`), the ngauth login cookie and CAVE token cookie
are issued with that domain, making them visible to all subdomains.

When both services share a single host via path-based routing (e.g.,
`host/auth/` and `host/app/`), no configuration is needed — cookies are
shared naturally.

**Files:**
- `datasetgate/settings.py` — new `AUTH_COOKIE_DOMAIN` setting
- `ngauth/views.py` — pass `domain=` when setting cookies

## 4. Revert Hardcoded `/web/datasets` Redirect

The current `AuthCallbackView` (line 135 of `ngauth/views.py`) has:

```python
response = HttpResponseRedirect("/web/datasets")
```

This was a premature edit — it works for browser users but breaks the
Neuroglancer popup flow. The `next` parameter fix (item 1 above) replaces
this with proper session-based redirect handling.
