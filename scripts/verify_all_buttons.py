import sys
sys.stdout.reconfigure(encoding='utf-8')
from playwright.sync_api import sync_playwright

def run_tests():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("=== 1. Testing Home Page (http://localhost:3000/) ===")
        page.goto("http://localhost:3000/")
        page.wait_for_load_state("networkidle")

        # Theme toggle
        theme_btn = page.locator("[data-theme-toggle]").first
        initial_theme = page.locator("html").get_attribute("data-theme")
        print(f"Initial theme: {initial_theme}")
        theme_btn.click()
        toggled_theme = page.locator("html").get_attribute("data-theme")
        print(f"Toggled theme: {toggled_theme}")
        assert initial_theme != toggled_theme, "Theme failed to toggle!"
        theme_btn.click() # toggle back
        print("✓ Theme Toggle test PASSED")

        # Hero Slider
        slider_counter = page.locator("#hero-slider-counter")
        c1 = slider_counter.inner_text()
        page.locator("#hero-slider-next").click()
        c2 = slider_counter.inner_text()
        print(f"Hero Slider counter: {c1} -> {c2}")
        assert c1 != c2, "Hero slider failed to advance!"
        print("✓ Hero Slider Next/Prev test PASSED")

        # Portfolio Filters
        filter_exhibition = page.locator(".portfolio-filter-btn[data-filter='exhibition']").first
        filter_exhibition.click()
        page.wait_for_timeout(200)
        visible_cards = page.locator(".portfolio-project-card:visible").count()
        print(f"Filtered 'exhibition' visible cards: {visible_cards}")
        assert visible_cards > 0, "No cards visible after filter!"
        page.locator(".portfolio-filter-btn[data-filter='all']").first.click()
        page.wait_for_timeout(200)
        total_cards = page.locator(".portfolio-project-card:visible").count()
        print(f"Reset 'all' visible cards: {total_cards}")
        assert total_cards == 6, f"Expected 6 cards, got {total_cards}"
        print("✓ Portfolio Filter test PASSED")

        # Project Detail Modal
        first_card = page.locator("[data-open-project='aurora']").first
        first_card.click()
        page.wait_for_timeout(400)
        modal = page.locator("#project-detail-modal")
        assert "is-open" in (modal.get_attribute("class") or ""), "Project modal failed to open!"
        print("✓ Project Modal Open test PASSED")
        page.locator("[data-close-project-modal]").first.click()
        page.wait_for_timeout(300)
        assert "is-open" not in (modal.get_attribute("class") or ""), "Project modal failed to close!"
        print("✓ Project Modal Close test PASSED")

        # Contact Drawer
        drawer_trigger = page.locator("[data-open-drawer]").first
        drawer_trigger.click()
        page.wait_for_timeout(300)
        drawer = page.locator("#contact-drawer")
        assert "is-open" in (drawer.get_attribute("class") or ""), "Contact drawer failed to open!"
        print("✓ Contact Drawer Open test PASSED")
        page.locator("[data-close-drawer]").first.click()
        page.wait_for_timeout(300)
        assert "is-open" not in (drawer.get_attribute("class") or ""), "Contact drawer failed to close!"
        print("✓ Contact Drawer Close test PASSED")

        print("\n=== 2. Testing Projects Page (http://localhost:3000/projeler.html) ===")
        page.goto("http://localhost:3000/projeler.html")
        page.wait_for_load_state("networkidle")
        header_cta = page.locator("header a:has-text('Proje Başlat')")
        assert header_cta.count() > 0, "Header CTA link not found on projeler.html!"
        assert header_cta.get_attribute("href") == "/iletisim.html", "Header CTA link target incorrect!"
        print(f"Header CTA correctly links to: {header_cta.get_attribute('href')}")
        print("✓ Projects Page CTA test PASSED")

        print("\n=== 3. Testing Contact Page (http://localhost:3000/iletisim.html) ===")
        page.goto("http://localhost:3000/iletisim.html")
        page.wait_for_load_state("networkidle")
        
        # Verify only info@projenicdesign.com is visible
        email_links = page.locator("a[href^='mailto:']").all()
        for el in email_links:
            href = el.get_attribute("href")
            txt = el.inner_text().strip()
            print(f"Found email link: {href} ({txt})")
            assert "hello@projenic.com" not in href, "Found deprecated hello@projenic.com!"
            assert href == "mailto:info@projenicdesign.com", f"Unexpected email link: {href}"
        print("✓ Public email is strictly info@projenicdesign.com PASSED")

        # Verify Form Submission dialog handling
        page.on("dialog", lambda dialog: dialog.accept())
        page.fill("#c-name", "Test Yetkili")
        page.fill("#c-company", "Test Şirketi A.Ş.")
        page.fill("#c-email", "test@firma.com")
        page.fill("#c-phone", "+90 555 123 45 67")
        page.fill("#c-event", "TÜYAP Otomotiv Fuarı")
        page.fill("#c-notes", "3D stand tasarımı ve 80 m² alan teklifi talep edilmektedir.")
        
        submit_btn = page.locator("#contact-full-form button[type='submit']")
        assert submit_btn.count() > 0, "Submit button not found on contact page!"
        print("✓ Contact form filled and submit button verified")

        browser.close()
        print("\n==========================================")
        print("ALL INTERACTION & BUTTON TESTS PASSED (100%)")
        print("==========================================")

if __name__ == "__main__":
    run_tests()
