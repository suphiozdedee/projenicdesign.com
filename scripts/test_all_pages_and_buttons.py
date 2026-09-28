import sys
sys.stdout.reconfigure(encoding='utf-8')
from playwright.sync_api import sync_playwright

def run_comprehensive_audit():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        errors = []
        page.on("pageerror", lambda err: errors.append(f"Console error: {err}"))
        
        pages_to_test = [
            ("Home", "http://localhost:3000/"),
            ("Projeler", "http://localhost:3000/projeler.html"),
            ("Yetkinlikler", "http://localhost:3000/yetkinlikler.html"),
            ("Hakkimizda", "http://localhost:3000/hakkimizda.html"),
            ("Iletisim", "http://localhost:3000/iletisim.html"),
            ("ULAK Project Detail", "http://localhost:3000/proje-aurora.html")
        ]

        print("================================================================")
        print("  PROJENIC DESIGN — COMPREHENSIVE INTERACTIVE & UI AUDIT       ")
        print("================================================================")

        for name, url in pages_to_test:
            print(f"\n[PAGE AUDIT] Loading {name}: {url}")
            resp = page.goto(url)
            assert resp.status == 200, f"Page {url} returned status {resp.status}"
            page.wait_for_load_state("networkidle")

            # 1. Theme toggle test
            theme_btn = page.locator("[data-theme-toggle]").first
            if theme_btn.count() > 0:
                t1 = page.locator("html").get_attribute("data-theme")
                theme_btn.click()
                t2 = page.locator("html").get_attribute("data-theme")
                assert t1 != t2, f"{name}: Theme failed to switch!"
                theme_btn.click() # restore
                print(f"  ✓ Theme toggle functional (light <-> dark)")

            # 2. Check public email addresses
            mailtos = page.locator("a[href^='mailto:']").all()
            for m in mailtos:
                href = m.get_attribute("href")
                assert "hello@projenic.com" not in href, f"{name}: Found deprecated hello@projenic.com in {href}"
                assert href == "mailto:info@projenicdesign.com", f"{name}: Unexpected public mailto {href}"
            print(f"  ✓ Public mailto links verified ({len(mailtos)} found, all info@projenicdesign.com)")

        # Deep test: Home Page interactive elements
        print("\n--- Testing Home Page Interactive Systems ---")
        page.goto("http://localhost:3000/")
        page.wait_for_load_state("networkidle")

        # Hero slider
        slider_counter = page.locator("#hero-slider-counter")
        c_init = slider_counter.inner_text()
        page.locator("#hero-slider-next").click()
        page.wait_for_timeout(200)
        c_next = slider_counter.inner_text()
        assert c_init != c_next, "Hero slider next button failed!"
        page.locator("#hero-slider-prev").click()
        page.wait_for_timeout(200)
        print("  ✓ Hero slider (next/prev) fully functional")

        # Filter buttons
        filters = ["all", "exhibition", "spatial", "events", "production"]
        for f in filters:
            btn = page.locator(f".portfolio-filter-btn[data-filter='{f}']").first
            btn.click()
            page.wait_for_timeout(100)
            vis = page.locator(".portfolio-project-card:visible").count()
            print(f"  ✓ Filter '{f}' active -> {vis} cards visible")

        # Project detail modal test with authentic projects
        projects_to_check = ["ulak", "yamaha", "dardanel", "stm", "mercedes", "perotti"]
        modal = page.locator("#project-detail-modal")
        for p_slug in projects_to_check:
            card = page.locator(f"[data-open-project='{p_slug}']").first
            assert card.count() > 0, f"Card for {p_slug} not found!"
            card.click()
            page.wait_for_timeout(250)
            assert "is-open" in (modal.get_attribute("class") or ""), f"Modal failed to open for {p_slug}"
            title_text = page.locator("#modal-project-title").inner_text()
            print(f"  ✓ Project modal opened for: {title_text}")
            page.locator("[data-close-project-modal]").first.click()
            page.wait_for_timeout(200)
            assert "is-open" not in (modal.get_attribute("class") or ""), f"Modal failed to close for {p_slug}"

        # Contact Drawer
        page.locator("[data-open-drawer]").first.click()
        page.wait_for_timeout(250)
        drawer = page.locator("#contact-drawer")
        assert "is-open" in (drawer.get_attribute("class") or ""), "Drawer failed to open"
        print("  ✓ Contact drawer opened")
        page.locator("[data-close-drawer]").first.click()
        page.wait_for_timeout(200)
        assert "is-open" not in (drawer.get_attribute("class") or ""), "Drawer failed to close"
        print("  ✓ Contact drawer closed")

        # Interactive Brief Form on Home page
        print("\n--- Testing Brief Form Submission & Confirmation Modal ---")
        brief_form = page.locator("form[data-fair-brief-form]").first
        brief_form.locator("[name='name']").fill("Suphi Test")
        brief_form.locator("[name='company']").fill("Projenic Mimarlık")
        brief_form.locator("[name='email']").fill("info@projenicdesign.com")
        brief_form.locator("[name='phone']").fill("+90 551 204 78 51")
        brief_form.locator("[name='notes']").fill("Otomasyon test brief iletimi.")
        brief_form.locator("button[type='submit']").click()
        page.wait_for_timeout(500)

        confirm_modal = page.locator("#brief-confirmation-modal")
        assert confirm_modal.count() > 0, "Confirmation modal element not created!"
        assert "pointer-events-none" not in (confirm_modal.get_attribute("class") or ""), "Confirmation modal failed to show!"
        print("  ✓ Form submission successfully triggered Projenic Confirmation Modal")
        wa_href = page.locator("#brief-confirm-wa").get_attribute("href")
        assert "wa.me/905512047851" in wa_href, f"Invalid WhatsApp URL: {wa_href}"
        print(f"  ✓ WhatsApp direct link verified: {wa_href[:50]}...")
        page.locator("#brief-confirm-ok").click()
        page.wait_for_timeout(200)
        assert "pointer-events-none" in (confirm_modal.get_attribute("class") or ""), "Confirmation modal failed to close"
        print("  ✓ Confirmation modal closed cleanly")

        # Deep test: Projeler Page
        print("\n--- Testing Projeler Page Project Cards ---")
        page.goto("http://localhost:3000/projeler.html")
        page.wait_for_load_state("networkidle")
        card_count = page.locator(".portfolio-project-card").count()
        assert card_count == 6, f"Expected 6 cards on projeler.html, got {card_count}"
        print(f"  ✓ Found all 6 authentic project cards on projeler.html")

        # Deep test: ULAK Project Detail & Lightbox
        print("\n--- Testing ULAK Project Detail & Lightbox ---")
        page.goto("http://localhost:3000/proje-aurora.html")
        page.wait_for_load_state("networkidle")
        h1 = page.locator("h1").inner_text()
        print(f"  ✓ Project detail page H1: '{h1}'")
        assert "ULAK Haberleşme" in h1, f"Expected ULAK Haberleşme, got {h1}"
        
        # Test Lightbox
        first_gallery = page.locator("[data-lightbox]").first
        first_gallery.click()
        page.wait_for_timeout(250)
        lightbox = page.locator("#image-lightbox-modal")
        assert "hidden" not in (lightbox.get_attribute("class") or ""), "Lightbox failed to open!"
        print("  ✓ Lightbox opened successfully")
        page.locator("#lightbox-close").click()
        page.wait_for_timeout(200)
        assert "hidden" in (lightbox.get_attribute("class") or ""), "Lightbox failed to close!"
        print("  ✓ Lightbox closed successfully")

        # Deep test: Contact Page form
        print("\n--- Testing Contact Page (iletisim.html) Form ---")
        page.goto("http://localhost:3000/iletisim.html")
        page.wait_for_load_state("networkidle")
        page.fill("#c-name", "Hakan Bey")
        page.fill("#c-company", "TÜYAP Katılımcı A.Ş.")
        page.fill("#c-email", "info@projenicdesign.com")
        page.fill("#c-phone", "+90 551 204 78 51")
        page.fill("#c-event", "IDEF 2025")
        page.fill("#c-notes", "200 m² Ada Stand Talebi")
        page.locator("#contact-full-form button[type='submit']").click()
        page.wait_for_timeout(500)
        
        confirm_modal = page.locator("#brief-confirmation-modal")
        assert "pointer-events-none" not in (confirm_modal.get_attribute("class") or ""), "Contact page modal failed to show!"
        print("  ✓ Contact page form triggered Projenic Confirmation Modal")
        page.locator("#brief-confirm-ok").click()

        browser.close()

        if errors:
            print("\nERRORS ENCOUNTERED:")
            for err in errors:
                print(f"  ✗ {err}")
            sys.exit(1)

        print("\n================================================================")
        print("  ALL 6 PAGES & INTERACTIVE SYSTEMS VERIFIED (100% PASS)       ")
        print("================================================================")

if __name__ == "__main__":
    run_comprehensive_audit()
