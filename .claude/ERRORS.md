# ERRORS — Issues Encountered and Fixes Applied

---

## E-001: google-auth-oauthlib not installed (Apr 14 2026)
**Error**: `ModuleNotFoundError: No module named 'google_auth_oauthlib'`
**Root cause**: requirements.txt only had google-api-python-client. OAuth libraries were not listed.
**Fix**: `pip3 install google-auth-oauthlib google-auth-httplib2`
**Prevention**: Updated requirements.txt to include both packages.

---

## E-002: attrs version conflict during mcp install (Apr 10 2026)
**Error**: `lib50 3.1.4 requires attrs<21,>=18.1, but you have attrs 26.1.0 which is incompatible`
**Root cause**: lib50 (an unrelated CS50 library) has a strict attrs<21 pin. mcp installs attrs 26.1.0.
**Fix**: Ignored — lib50 is an unrelated course tool, not part of this project. attrs 26.1.0 works correctly for our packages.
**Status**: Non-blocking, ignored.
