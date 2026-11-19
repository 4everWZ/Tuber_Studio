"""Enter a private Studio login without including the credential in browser errors."""

from playwright.async_api import Error


async def fill_private_login(page, token):
    field = page.locator("#token")
    await field.wait_for(state="visible")
    try:
        # Playwright's fill action includes its argument in timeout call logs.
        await field.evaluate("""(input, value) => {
            input.value = value;
            input.dispatchEvent(new Event("input", {bubbles: true}));
        }""", token)
    except Error:
        raise RuntimeError("无法在工坊登录表单中填写凭据") from None
