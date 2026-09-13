from playwright.sync_api import sync_playwright
import os


def launch_and_save_session():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(script_dir, "betway_profile")

    print(f"📁 Session Vault Path: {profile_path}")
    print("🚀 Launching browser configuration...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_path,
            headless=False,
            channel="chrome",  # use real installed Chrome, not bundled Chromium
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
            no_viewport=True,
        )

        # Extra stealth: patch navigator.webdriver before any page scripts run
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined })"
        )

        page = context.new_page()

        try:
            print("\n🌐 Opening Betway...")
            page.goto("https://www.betway.com.ng/")

            print("\n⚠️  ACTION REQUIRED ⚠️")
            print("1. Please MANUALLY click Login on the web page.")
            print("2. Enter your phone number / username and password.")
            print("3. Verify you can see your account balance.")
            print(
                "4. Leave this script running; DO NOT close the browser window manually."
            )

            input(
                "\n👉 Press ENTER once you're fully logged in and can see your balance..."
            )

            print("\n🔒 Session successfully secured in the vault folder!")

        except Exception as e:
            print(f"❌ Error during login session capture: {e}")
        finally:
            context.close()
            print("🚪 Browser closed. You can now run direct_request.py.")


if __name__ == "__main__":
    launch_and_save_session()
