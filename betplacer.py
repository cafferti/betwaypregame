from playwright.sync_api import sync_playwright
import os
import json
import time


def find_betway_bet_placement_api():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(script_dir, "betway_profile")

    captured_count = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_path,
            headless=False,
            channel="chrome",
            no_viewport=True,
        )

        page = context.new_page()

        def handle_request(request):
            # Bet placement is virtually always a POST/PUT with a JSON body
            if request.method not in ("POST", "PUT"):
                return
            if "betway" not in request.url and "betwayafrica" not in request.url:
                return

            print(f"\n{'#' * 80}")
            print(f"➡️  {request.method} REQUEST: {request.url}")
            post_data = request.post_data
            if post_data:
                # Try to pretty-print if it's JSON
                try:
                    parsed = json.loads(post_data)
                    print(f"Body:\n{json.dumps(parsed, indent=2)}")
                except Exception:
                    print(f"Body (raw): {post_data}")
            print(f"{'#' * 80}")

        def handle_response(response):
            nonlocal captured_count
            request = response.request
            if request.method not in ("POST", "PUT"):
                return
            if "betway" not in response.url and "betwayafrica" not in response.url:
                return

            try:
                content_type = response.headers.get("content-type", "")
            except Exception:
                return
            if "application/json" not in content_type:
                return

            try:
                body_text = response.text()
                body = json.loads(body_text)
                preview = json.dumps(body, indent=2)[:3000]
            except Exception as e:
                print(f"⚠️ Could not read response body: {e}")
                return

            captured_count += 1
            print(f"\n{'=' * 80}")
            print(f"📡 [{captured_count}] RESPONSE for {response.url}")
            print(f"Status: {response.status}")
            print(f"Body:\n{preview}")
            print(f"{'=' * 80}")

        page.on("request", handle_request)
        page.on("response", handle_response)

        print("🌐 Opening betway.com.ng...")
        page.goto("https://www.betway.com.ng/", wait_until="domcontentloaded")

        print("\n⚠️  IMPORTANT — MANUAL STEPS:")
        print("1. Find any pregame football match with low odds (favorite).")
        print("2. Add it to your bet slip.")
        print("3. Enter the SMALLEST possible stake (e.g. ₦100).")
        print("4. Click 'Place Bet' and confirm.")
        print("5. Wait for the bet confirmation to appear on screen.")
        print(
            "👉 Every POST/PUT request+response involving betway will print here automatically."
        )

        input("\n⏸️  Press ENTER once your bet is placed and confirmed on screen...")

        print(f"\n✅ Captured {captured_count} responses. Closing in 3 seconds...")
        time.sleep(3)
        context.close()


if __name__ == "__main__":
    find_betway_bet_placement_api()
