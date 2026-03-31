"""OAuth callback route for handling OIDC provider redirects.

Mounted as a custom route on the FastMCP server. Handles the second leg
of the "OAuth-over-OAuth" flow: receives the OIDC provider's auth code,
exchanges it for an ID token, validates the user, and issues an MCP
authorization code back to the MCP client.
"""

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .oauth_provider import AUTH_CODE_TTL, OIDCDelegatingProvider

logger = logging.getLogger(__name__)


def _error_redirect(redirect_uri: str, state: str | None, error: str, description: str) -> Response:
    """Build an OAuth error redirect response."""
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    return RedirectResponse(url=f"{redirect_uri}?{urlencode(params)}", status_code=302)


async def handle_oauth_callback(
    request: Request,
    provider: OIDCDelegatingProvider,
) -> Response:
    """Handle the OIDC provider's redirect after user authentication.

    Flow:
    1. Validate the state parameter against our ephemeral store
    2. Check for errors from the OIDC provider
    3. Exchange the OIDC auth code for an ID token
    4. Validate the ID token (signature, issuer, audience, expiry)
    5. Check user authorization (email match)
    6. Generate an MCP authorization code
    7. Redirect to the MCP client with the code
    """
    # Extract query parameters
    error = request.query_params.get("error")
    state = request.query_params.get("state")
    code = request.query_params.get("code")

    # Load state mapping
    auth_state = provider.ephemeral.pop(f"state:{state}") if state else None
    original_state = auth_state.get("original_state") if auth_state else None
    redirect_uri = auth_state.get("redirect_uri") if auth_state else None

    if not auth_state or not redirect_uri:
        logger.warning("OAuth callback with invalid or expired state")
        return Response("Invalid or expired OAuth state", status_code=400)

    # Forward OIDC provider errors to the MCP client
    if error:
        error_desc = request.query_params.get("error_description", "upstream authentication failed")
        return _error_redirect(redirect_uri, original_state, error, error_desc)

    if not code:
        return _error_redirect(
            redirect_uri, original_state, "server_error", "missing code parameter"
        )

    # Exchange OIDC code for tokens
    try:
        token_response = await provider.http_client.post(
            provider._token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": provider._callback_url,
                "client_id": provider.config.oauth_client_id,
                "client_secret": provider.config.oauth_client_secret,
            },
        )
        if token_response.status_code != 200:
            logger.error(
                "OIDC token exchange failed: %d %s",
                token_response.status_code,
                token_response.text,
            )
            return _error_redirect(
                redirect_uri, original_state, "server_error", "upstream token exchange failed"
            )
        token_data = token_response.json()
    except httpx.HTTPError:
        logger.exception("OIDC token exchange HTTP error")
        return _error_redirect(
            redirect_uri, original_state, "server_error", "upstream authentication failed"
        )

    # Validate ID token
    id_token_str = token_data.get("id_token")
    if not id_token_str:
        return _error_redirect(
            redirect_uri, original_state, "server_error", "no id_token in upstream response"
        )

    claims = await _validate_id_token(id_token_str, provider)
    if claims is None:
        return _error_redirect(
            redirect_uri, original_state, "access_denied", "ID token validation failed"
        )

    # Check user authorization
    email = claims.get("email")
    if not claims.get("email_verified", False):
        logger.warning("OAuth login rejected: email %s is not verified", email)
        return _error_redirect(redirect_uri, original_state, "access_denied", "email not verified")

    if provider.config.oauth_authorized_email:
        if email != provider.config.oauth_authorized_email:
            logger.warning(
                "Unauthorized OAuth user: %s (expected %s)",
                email,
                provider.config.oauth_authorized_email,
            )
            return _error_redirect(
                redirect_uri, original_state, "access_denied", "unauthorized user"
            )

    # Generate MCP authorization code
    mcp_code = secrets.token_urlsafe(32)
    provider.ephemeral.save(
        f"code:{mcp_code}",
        {
            "client_id": auth_state["client_id"],
            "code_challenge": auth_state["code_challenge"],
            "redirect_uri": auth_state["redirect_uri"],
            "redirect_uri_provided_explicitly": auth_state["redirect_uri_provided_explicitly"],
            "scopes": auth_state["scopes"],
            "resource": auth_state.get("resource"),
            "expires_at": time.time() + AUTH_CODE_TTL,
        },
    )

    # Redirect to MCP client with the code
    params = {"code": mcp_code}
    if original_state:
        params["state"] = original_state
    return RedirectResponse(url=f"{redirect_uri}?{urlencode(params)}", status_code=302)


async def _validate_id_token(
    id_token_str: str,
    provider: OIDCDelegatingProvider,
) -> dict | None:
    """Validate an OIDC ID token. Returns claims dict or None on failure.

    Retries once with refreshed JWKS on unknown key ID (handles key rotation).
    """
    for attempt in range(2):
        try:
            # Build signing keys from cached JWKS
            signing_keys = [jwt.PyJWK(key_data) for key_data in provider._jwks.get("keys", [])]

            # Decode the token header to find the key ID
            unverified_header = jwt.get_unverified_header(id_token_str)
            kid = unverified_header.get("kid")

            # Find matching key
            signing_key = None
            for key in signing_keys:
                if key.key_id == kid:
                    signing_key = key
                    break

            if signing_key is None:
                if attempt == 0:
                    logger.info("Unknown key ID %s, refreshing JWKS", kid)
                    await provider._refresh_jwks()
                    continue
                logger.error("Unknown key ID %s after JWKS refresh", kid)
                return None

            claims = jwt.decode(
                id_token_str,
                signing_key.key,
                algorithms=provider.jwks_algorithms,
                issuer=provider.config.oauth_issuer_url,
                audience=provider.config.oauth_client_id,
                options={"require": ["exp", "iss", "aud", "email"]},
            )
            return claims

        except jwt.ExpiredSignatureError:
            logger.warning("ID token has expired")
            return None
        except jwt.InvalidTokenError as e:
            if attempt == 0 and "kid" in str(e).lower():
                logger.info("Token validation failed, refreshing JWKS: %s", e)
                await provider._refresh_jwks()
                continue
            logger.warning("ID token validation failed: %s", e)
            return None

    return None
